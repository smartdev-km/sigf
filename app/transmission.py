"""Transfert de propriété : un NOUVEAU dossier est créé au nom des nouveaux propriétaires.

Vente, donation, héritage et expropriation ne modifient plus le dossier : à leur validation, le système
crée un dossier SUCCESSEUR et clôture l'ancien, qui reste consultable comme historique.

Le successeur décrit la parcelle telle qu'elle est APRÈS le transfert :
- il contient TOUS les propriétaires actuels (même pour la vente d'une partie seulement : le vendeur y
  figure pour sa part restante), donc un seul dossier à jour décrit toujours la parcelle ;
- il est certifié d'emblée (le Directeur vient de valider la mutation) et reprend les numéros RTC/RTS,
  puisque la parcelle est la même : son certificat peut être imprimé tout de suite ;
- il reprend le tracé (copie du fichier) et les hypothèques encore actives, qui suivent la parcelle ;
- un lien de filiation relie l'ancien dossier au nouveau (affiché dans l'arbre de traçabilité).

Les hypothèques, mainlevées, rectifications et annulations, elles, restent dans le dossier.
Tout se fait dans la transaction de la validation : tout ou rien.
"""

import json
import os
import uuid
from datetime import date

from . import constants as C
from . import mutations as M
from . import partages
from .audit import audit
from .extensions import db
from .models import Dossier, EtapeLog, Filiation, Geometrie, Mutation, MutationPartie, Parcelle, PartInitiale, maintenant
from .storage import get_storage

LIBELLES = {
    C.MUT_VENTE: "vente",
    C.MUT_DONATION: "donation",
    C.MUT_HERITAGE: "héritage",
    C.MUT_EXPROPRIATION: "expropriation",
}


def creer_successeur(m, user, d):
    """Appelé par mutations.valider, après le scellement de `m`. Retourne le nouveau dossier."""
    et = M.etat(d)  # `m` est scellée mais pas encore « terminale » : cet état inclut donc le transfert
    parts = sorted(((p.id, f.numerator, f.denominator) for p, f in et.parts), key=lambda t: (-t[1] / t[2], t[0]))
    if not parts:
        raise M.MutationError("Le transfert ne laisse aucun propriétaire : validation impossible.")

    quoi = LIBELLES[m.type]
    p = d.parcelle
    maintenant_ = maintenant().replace(microsecond=0)
    s = Dossier(
        origine=C.ORIGINE_NOUVEAU,
        filiere=C.FILIERE_PUBLIC,
        statut=C.CERTIFIE,
        parcelle=Parcelle(
            reference=p.reference, ile_id=p.ile_id, commune=p.commune, village=p.village,
            superficie_m2=p.superficie_m2, type_titre=p.type_titre,
        ),
        proprietaire_id=parts[0][0],
        cree_par_id=user.id,
        rsp=d.rsp,
        rcp=d.rcp,
        valide_par_id=user.id,
        valide_at=maintenant_,
        notes=f"Issu d'une {quoi} du dossier {d.numero} (acte du {m.date_acte:%d/%m/%Y}, {m.reference_acte or 'sans référence'})",
    )
    db.session.add(s)
    db.session.flush()
    s.numero = f"SIGF-{date.today().year}-{s.id:05d}"
    from . import titrage

    titrage.entrer_en_titrage(s, user)                 # un dossier « Certificat émis » est suivi dans « Titrage »
    for pid, num, den in parts:
        s.parts_initiales.append(PartInitiale(proprietaire_id=pid, part_num=num, part_den=den))
    # type « derive » : ce n'est ni un certificat émis par le circuit, ni une création ordinaire (statistiques)
    db.session.add(
        EtapeLog(
            dossier=s, de_statut=None, vers_statut=C.CERTIFIE, type="derive", user_id=user.id,
            commentaire=f"Créé par {quoi} du dossier {d.numero} (mutation n° {m.seq})",
        )
    )

    # --- le tracé suit la parcelle ---
    g = d.geometrie_courante
    if g is not None:
        extension = os.path.splitext(g.cle)[1]
        cle = f"dossiers/{s.id}/trace/{uuid.uuid4().hex}{extension}"
        get_storage().copier(g.cle, cle)
        db.session.add(
            Geometrie(
                dossier_id=s.id, version=1, geojson=g.geojson, empreinte=g.empreinte, superficie_m2=g.superficie_m2,
                perimetre_m=g.perimetre_m, centre_lon=g.centre_lon, centre_lat=g.centre_lat,
                min_lon=g.min_lon, min_lat=g.min_lat, max_lon=g.max_lon, max_lat=g.max_lat,
                cle=cle, nom_original=g.nom_original, mime=g.mime, taille=g.taille, sha256=g.sha256,
                uploaded_by_id=g.uploaded_by_id, elements_json=g.elements_json, motif_remplacement=f"Reprise du tracé du dossier {d.numero}",
            )
        )
    db.session.flush()

    # --- les hypothèques actives suivent la parcelle ---
    for charge in et.charges:
        copie = Mutation(
            dossier_id=s.id, type=C.MUT_HYPOTHEQUE, statut=C.MS_ATTENTE, date_acte=charge.date_acte,
            reference_acte=charge.reference_acte, montant=charge.montant,
            notes=f"Hypothèque reportée depuis le dossier {d.numero}",
            saisi_par_id=charge.saisi_par_id, saisi_at=charge.saisi_at,
            details=json.dumps({"origine": d.numero, "reportee_de": charge.id}, sort_keys=True, separators=(",", ":")),
        )
        for partie in charge.parties:
            copie.parties.append(MutationPartie(role=partie.role, proprietaire=partie.proprietaire, part_num=partie.part_num, part_den=partie.part_den))
        db.session.add(copie)
        db.session.flush()
        M.sceller(s, copie, user)

    # --- filiation, puis clôture de l'ancien dossier ---
    db.session.add(Filiation(source_id=d.id, resultat_id=s.id, type=m.type, mutation_id=m.id))
    db.session.flush()
    db.session.expire(d, ["descendances"])
    partages._fermer(d, user, m, f"propriété transférée ({quoi}) : dossier à jour {s.numero}")
    audit("dossier_cree_par_transfert", "dossier", s.id, nouveau=f"{s.numero} <- {d.numero} ({quoi})", user=user)
    from . import generation

    db.session.expire(s, ["geometries", "mutations"])
    generation.automatiques(s, user)                      # le nouveau dossier est certifié d'emblée : ses documents sont générés
    return s
