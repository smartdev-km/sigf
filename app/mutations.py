"""Mutations : historique juridique du terrain.

Règles :
- Le Domaine saisit, le Directeur valide ; personne ne valide sa propre saisie.
- Seules les mutations VALIDÉES comptent. Elles sont numérotées (seq) et chaînées par HMAC :
  chaque empreinte couvre le contenu de la mutation et l'empreinte de la précédente.
- Rien n'est jamais modifié ni supprimé. Une erreur se corrige par une mutation d'annulation,
  qui ne peut viser que la DERNIÈRE mutation active (pile) : l'état se recalcule alors en
  rejouant l'historique sans elle, sans jamais devenir incohérent.
- Le propriétaire actuel n'est pas stocké : il est CALCULÉ en rejouant les mutations validées
  à partir du propriétaire initial du dossier (reprise de l'existant ou fin du circuit).
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from itertools import zip_longest

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from . import constants as C
from . import documents as Doc
from .audit import audit
from .extensions import db
from .models import Dossier, Filiation, Mutation, MutationPartie, Proprietaire, critere_nom, maintenant
from .storage import StorageError

bp = Blueprint("mutations", __name__)

GENESE = "0" * 64  # « empreinte précédente » de la première mutation


class MutationError(Exception):
    """Mutation refusée (message affichable à l'utilisateur)."""


# --------------------------------------------------------------------------
# Droits
# --------------------------------------------------------------------------
def dossier_mutable(d):
    """Seuls les dossiers terminés (certifiés) ou repris de l'existant ont des mutations."""
    return d.statut in (C.CERTIFIE, C.ARCHIVE) and not d.est_bloque   # un problème signalé fige le dossier


def peut_saisir(user, d):
    return user.role == C.DOMAINE and dossier_mutable(d)


def peut_decider(user):
    return user.role == C.DIRECTEUR


def peut_consulter(user):
    return user.role != C.TOPO_PRIVE


# --------------------------------------------------------------------------
# Quotes-parts
# --------------------------------------------------------------------------
def parse_part(texte, nom="La quote-part"):
    brut = (texte or "").strip().replace(",", ".").replace(" ", "")
    if not brut:
        raise MutationError(f"{nom} est obligatoire.")
    try:
        f = Fraction(brut[:-1]) / 100 if brut.endswith("%") else Fraction(brut)
    except (ValueError, ZeroDivisionError):
        raise MutationError(f"{nom} « {texte.strip()} » est invalide (exemples : 1, 1/2, 25%).") from None
    if not 0 < f <= 1:
        raise MutationError(f"{nom} doit être supérieure à 0 et ne pas dépasser 1 (la parcelle entière).")
    if f.denominator > 100000:
        raise MutationError(f"{nom} « {texte.strip()} » est trop précise.")
    return f


def formater_part(f):
    return "totalité" if f == 1 else f"{f.numerator}/{f.denominator}"


def _part_de(partie):
    return Fraction(partie.part_num, partie.part_den)


# --------------------------------------------------------------------------
# État du terrain (calculé)
# --------------------------------------------------------------------------
@dataclass
class Etat:
    parts: list = field(default_factory=list)  # [(Proprietaire, Fraction)] propriétaires actuels
    charges: list = field(default_factory=list)  # hypothèques actives (Mutation)
    derniere: Mutation | None = None  # dernière mutation active (cible possible d'une annulation)
    connus: dict = field(default_factory=dict)  # id -> Proprietaire (toute personne ayant figuré au dossier)


def valides(d):
    return sorted((m for m in d.mutations if m.statut == C.MS_VALIDEE), key=lambda m: m.seq)


def annulees_ids(valid):
    return {m.lie_a_id for m in valid if m.type == C.MUT_ANNULATION}


def etat(d):
    """Rejoue les mutations validées, hors annulations et mutations annulées."""
    valid = valides(d)
    annulees = annulees_ids(valid)
    if d.parts_initiales:  # parcelle issue d'une division ou d'une fusion
        parts = {p.proprietaire_id: Fraction(p.part_num, p.part_den) for p in d.parts_initiales}
        connus = {p.proprietaire_id: p.proprietaire for p in d.parts_initiales}
    else:
        parts = {d.proprietaire_id: Fraction(1)}
        connus = {d.proprietaire_id: d.proprietaire}
    charges = {}
    derniere = None
    # Un transfert qui a donné naissance à un nouveau dossier est TERMINAL pour celui-ci : l'ancien dossier
    # garde les propriétaires d'avant (historique), le dossier successeur porte ceux d'après.
    transferts_clotures = {f.mutation_id for f in d.descendances if f.type in C.TRANSFERTS}
    for m in valid:
        for p in m.parties:
            connus[p.proprietaire_id] = p.proprietaire
        if m.type == C.MUT_ANNULATION or m.id in annulees:
            continue
        if m.id in transferts_clotures:
            continue
        if not (m.details and '"reportee_de"' in m.details):   # une charge reportée n'est pas « la dernière mutation »
            derniere = m
        if m.type in C.TRANSFERTS:
            for p in m.parties:
                if p.role == C.ROLE_CEDANT:
                    parts[p.proprietaire_id] = parts.get(p.proprietaire_id, Fraction(0)) - _part_de(p)
                elif p.role == C.ROLE_BENEFICIAIRE:
                    parts[p.proprietaire_id] = parts.get(p.proprietaire_id, Fraction(0)) + _part_de(p)
        elif m.type == C.MUT_HYPOTHEQUE:
            charges[m.id] = m
        elif m.type == C.MUT_MAINLEVEE:
            charges.pop(m.lie_a_id, None)
    actuels = [(connus[pid], f) for pid, f in parts.items() if f != 0]
    actuels.sort(key=lambda x: (-x[1], x[0].nom_complet))
    return Etat(parts=actuels, charges=list(charges.values()), derniere=derniere, connus=connus)


def libelle_proprietaires(d):
    parts = etat(d).parts
    if len(parts) == 1 and parts[0][1] == 1:
        return parts[0][0].nom_complet
    return " ; ".join(f"{p.nom_complet} ({formater_part(f)})" for p, f in parts) or "-"


# --------------------------------------------------------------------------
# Règles métier (appliquées à la saisie ET de nouveau à la validation)
# --------------------------------------------------------------------------
def _controler_regles(m, et):
    ced = [p for p in m.parties if p.role == C.ROLE_CEDANT]
    ben = [p for p in m.parties if p.role == C.ROLE_BENEFICIAIRE]
    detenues = {p.id: f for p, f in et.parts}

    if m.type in C.TRANSFERTS:
        if not ced or not ben:
            raise MutationError("Un transfert exige au moins un cédant et un bénéficiaire.")
        for p in ced:
            if _part_de(p) > detenues.get(p.proprietaire.id, Fraction(0)):
                nom = p.proprietaire.nom_complet
                raise MutationError(f"{nom} ne détient pas la part qu'il/elle cède ({formater_part(_part_de(p))}).")
        if sum(_part_de(p) for p in ced) != sum(_part_de(p) for p in ben):
            raise MutationError("Le total des parts cédées doit être égal au total des parts reçues.")
        # (une personne tout juste créée n'a pas encore d'identifiant : elle ne peut pas être cédante)
        if {p.proprietaire.id for p in ced} & {p.proprietaire.id for p in ben if p.proprietaire.id}:
            raise MutationError("Une même personne ne peut pas être à la fois cédant et bénéficiaire.")
    elif m.type == C.MUT_MAINLEVEE:
        if m.lie_a_id not in {c.id for c in et.charges}:
            raise MutationError("Cette hypothèque n'est plus active : mainlevée impossible.")
    elif m.type == C.MUT_ANNULATION:
        if et.derniere is None or m.lie_a_id != et.derniere.id:
            raise MutationError("Seule la dernière mutation active peut être annulée.")
    elif m.type in C.PARTAGES:
        from . import partages

        partages.controler(m, et)
    elif m.type == C.MUT_RECTIFICATION and m.details:
        from . import corrections

        corrections.controler(m, et, m.dossier)


# --------------------------------------------------------------------------
# Saisie
# --------------------------------------------------------------------------
def _texte(valeur, nom, maxlen):
    valeur = (valeur or "").strip()
    if len(valeur) > maxlen:
        raise MutationError(f"{nom} est trop long (maximum {maxlen} caractères).")
    return valeur


def _date_acte(valeur):
    try:
        jour = date.fromisoformat((valeur or "").strip())
    except ValueError:
        raise MutationError("La date de l'acte est obligatoire (format valide).") from None
    if jour > date.today():
        raise MutationError("La date de l'acte ne peut pas être dans le futur.")
    if jour.year < 1800:
        raise MutationError("La date de l'acte est invalide.")
    return jour


def _montant(valeur, nom):
    brut = (valeur or "").strip().replace(" ", "")
    if not brut:
        return None
    if not brut.isdigit() or int(brut) <= 0 or int(brut) > 10**13:
        raise MutationError(f"{nom} doit être un nombre entier positif (en KMF).")
    return int(brut)


TOLERANCE_M2 = 0.01  # écart d'arrondi accepté entre les m² saisis (au centième) et la part cédée


def parse_m2(texte, nom="La superficie"):
    """« 225,25 », « 225.25 m² » -> Fraction (m²) strictement positive."""
    brut = (texte or "").strip().lower().replace("m²", "").replace("m2", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not brut:
        raise MutationError(f"{nom} est obligatoire.")
    try:
        v = Fraction(brut)
    except (ValueError, ZeroDivisionError):
        raise MutationError(f"{nom} « {texte.strip()} » est invalide : saisissez un nombre de m² (exemple : 225,25).") from None
    if v <= 0:
        raise MutationError(f"{nom} doit être supérieure à 0.")
    return v


def _part_depuis_m2(brut_m2, aire):
    """Quote-part (Fraction de la parcelle) correspondant à une superficie en m²."""
    surface = Fraction(str(round(aire, 2)))
    m2 = parse_m2(brut_m2, "La superficie reçue par un bénéficiaire")
    if m2 > surface + Fraction(str(TOLERANCE_M2)):
        raise MutationError(f"La superficie reçue ({brut_m2.strip()} m²) dépasse celle de la parcelle ({formater_m2(float(surface))}).")
    return min(Fraction(1), (m2 / surface)).limit_denominator(100000)


def formater_m2(valeur):
    from .historique import formater_m2 as f

    return f(valeur)


def _parties_transfert(m, form, et, d=None):
    for proprietaire, detenue in et.parts:
        brut = (form.get(f"cede_{proprietaire.id}") or "").strip()
        if not brut:
            continue
        part = detenue if brut.lower() in ("tout", "totalité", "totalite") else parse_part(brut, f"La part cédée par {proprietaire.nom_complet}")
        m.parties.append(MutationPartie(role=C.ROLE_CEDANT, proprietaire=proprietaire, part_num=part.numerator, part_den=part.denominator))

    champs = [form.getlist(k) for k in ("b_existant", "b_nom", "b_prenom", "b_identite", "b_telephone", "b_part", "b_m2")]
    deja, en_m2 = set(), []
    aire = None
    for existant, nom, prenom, identite, tel, brut_part, brut_m2 in zip_longest(*champs, fillvalue=""):
        if not any((existant.strip(), nom.strip(), brut_part.strip(), brut_m2.strip())):
            continue  # ligne vide
        if brut_m2.strip():                                  # la part reçue est saisie en m² : on en tire la quote-part exacte
            if aire is None:
                from . import historique as H

                aire = H.superficie(d)[0] if d is not None else None
            if not aire:
                raise MutationError("La superficie de la parcelle est inconnue : indiquez la part du bénéficiaire en fraction (1/2, 25 %…).")
            part = _part_depuis_m2(brut_m2, aire)
        else:
            part = parse_part(brut_part, "La superficie d'un bénéficiaire" if aire else "La part d'un bénéficiaire")
        if existant.strip():
            try:
                pid = int(existant)
            except ValueError:
                raise MutationError("Bénéficiaire existant invalide.") from None
            if pid not in et.connus:
                raise MutationError("Ce bénéficiaire n'est pas connu de ce dossier.")
            if pid in deja:
                raise MutationError("Un même bénéficiaire apparaît deux fois.")
            deja.add(pid)
            personne = et.connus[pid]
        else:
            if not nom.strip():
                raise MutationError("Le nom d'un bénéficiaire est obligatoire.")
            personne = Proprietaire(
                nom=_texte(nom, "Le nom", 120),
                prenom=_texte(prenom, "Le prénom", 120) or None,
                identite=_texte(identite, "La pièce d'identité", 80) or None,
                telephone=_texte(tel, "Le téléphone", 40) or None,
            )
        ligne = MutationPartie(role=C.ROLE_BENEFICIAIRE, proprietaire=personne, part_num=part.numerator, part_den=part.denominator)
        m.parties.append(ligne)
        if brut_m2.strip():
            en_m2.append(ligne)

    # Les m² sont saisis au centième : un écart d'arrondi avec la part cédée (ex. 1/3 d'une parcelle) est absorbé par le dernier bénéficiaire.
    if en_m2 and aire:
        cede = sum((Fraction(p.part_num, p.part_den) for p in m.parties if p.role == C.ROLE_CEDANT), Fraction(0))
        recu = sum((Fraction(p.part_num, p.part_den) for p in m.parties if p.role == C.ROLE_BENEFICIAIRE), Fraction(0))
        ecart = cede - recu
        if ecart != 0 and abs(float(ecart)) * aire <= TOLERANCE_M2:
            derniere = en_m2[-1]
            corrigee = Fraction(derniere.part_num, derniere.part_den) + ecart
            if 0 < corrigee <= 1 and corrigee.denominator <= 100000:
                derniere.part_num, derniere.part_den = corrigee.numerator, corrigee.denominator


def construire(d, user, form):
    """Valide le formulaire et retourne une Mutation en attente (non enregistrée)."""
    if not peut_saisir(user, d):
        raise MutationError("Vous ne pouvez pas saisir de mutation sur ce dossier.")
    type_ = form.get("type")
    if type_ not in C.MUTATIONS:
        raise MutationError("Type de mutation invalide.")
    if type_ in C.PARTAGES:
        raise MutationError("La division et la fusion ont leur propre formulaire (boutons « Diviser » et « Fusionner »).")
    et = etat(d)
    m = Mutation(
        dossier_id=d.id,
        type=type_,
        statut=C.MS_ATTENTE,
        date_acte=_date_acte(form.get("date_acte")),
        reference_acte=_texte(form.get("reference_acte"), "La référence de l'acte", 200) or None,
        notes=_texte(form.get("notes"), "Les notes", 4000) or None,
        saisi_par_id=user.id,
        saisi_at=maintenant(),
    )

    if type_ in C.TRANSFERTS:
        if not m.reference_acte:
            raise MutationError("La référence de l'acte (notaire, jugement, n° d'acte…) est obligatoire.")
        # Un prix n'existe que pour une vente : donation, héritage et expropriation n'en ont pas (un montant envoyé est ignoré)
        m.montant = _montant(form.get("montant"), "Le prix") if type_ == C.MUT_VENTE else None
        _parties_transfert(m, form, et, d)
    elif type_ == C.MUT_HYPOTHEQUE:
        if not m.reference_acte:
            raise MutationError("La référence de l'acte est obligatoire.")
        m.montant = _montant(form.get("montant"), "Le montant garanti")
        if not m.montant:
            raise MutationError("Le montant garanti est obligatoire.")
        nom = _texte(form.get("creancier_nom"), "Le nom du créancier", 120)
        if not nom:
            raise MutationError("Le créancier (banque, particulier…) est obligatoire.")
        m.parties.append(
            MutationPartie(role=C.ROLE_CREANCIER, proprietaire=Proprietaire(nom=nom, identite=_texte(form.get("creancier_identite"), "L'identifiant", 80) or None))
        )
    elif type_ == C.MUT_MAINLEVEE:
        if not m.reference_acte:
            raise MutationError("La référence de l'acte de mainlevée est obligatoire.")
        try:
            m.lie_a_id = int(form.get("lie_a_id", ""))
        except ValueError:
            raise MutationError("Choisissez l'hypothèque à lever.") from None
    elif type_ == C.MUT_ANNULATION:
        if not m.notes:
            raise MutationError("Le motif de l'annulation est obligatoire (champ notes).")
        if et.derniere is None:
            raise MutationError("Il n'y a aucune mutation à annuler.")
        m.lie_a_id = et.derniere.id
    elif type_ == C.MUT_RECTIFICATION:
        if not m.notes:
            raise MutationError("Décrivez la rectification (champ notes).")

    _controler_regles(m, et)
    return m


# --------------------------------------------------------------------------
# Chaîne d'empreintes
# --------------------------------------------------------------------------
def _calculer_hash(m, hash_precedent):
    corps = {
            "dossier": m.dossier_id,
            "seq": m.seq,
            "type": m.type,
            "date_acte": m.date_acte.isoformat(),
            "reference": m.reference_acte,
            "montant": m.montant,
            "notes": m.notes,
            "lie_a": m.lie_a_id,
            "saisi_par": m.saisi_par_id,
            "saisi_at": m.saisi_at.replace(microsecond=0).isoformat(),
            "valide_par": m.decision_par_id,
            "valide_at": m.decision_at.replace(microsecond=0).isoformat() if m.decision_at else None,
            "parties": sorted([p.role, p.proprietaire_id, p.part_num, p.part_den] for p in m.parties),
    }
    if m.details:
        corps["details"] = m.details
    contenu = json.dumps(corps, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    cle = current_app.config["QR_SECRET"].encode()
    return hmac.new(cle, b"mutation:" + contenu.encode() + b"|" + hash_precedent.encode(), hashlib.sha256).hexdigest()


def verifier_chaine(d):
    """(True, None) si l'historique est intact, sinon (False, n° de la première mutation douteuse)."""
    precedent = GENESE
    for attendu, m in enumerate(valides(d), start=1):
        if m.seq != attendu or m.hash_precedent != precedent or not m.hash:
            return False, attendu
        if not hmac.compare_digest(m.hash, _calculer_hash(m, precedent)):
            return False, attendu
        precedent = m.hash
    return True, None


# --------------------------------------------------------------------------
# Décision du Directeur
# --------------------------------------------------------------------------
def sceller(d, m, user):
    """Numérote la mutation dans la chaîne du dossier et la signe (HMAC), sans commit."""
    precedent = (
        Mutation.query.filter(Mutation.dossier_id == d.id, Mutation.statut == C.MS_VALIDEE, Mutation.id != m.id)
        .order_by(Mutation.seq.desc())
        .first()
    )
    m.seq = (precedent.seq + 1) if precedent else 1
    m.hash_precedent = precedent.hash if precedent else GENESE
    m.statut = C.MS_VALIDEE
    m.decision_par_id = user.id
    m.decision_at = maintenant().replace(microsecond=0)
    m.hash = _calculer_hash(m, m.hash_precedent)


def valider(user, m):
    if not peut_decider(user):
        raise MutationError("Seul le Directeur peut valider une mutation.")
    if m.statut != C.MS_ATTENTE:
        raise MutationError("Cette mutation a déjà été traitée.")
    if m.saisi_par_id == user.id:
        raise MutationError("Vous ne pouvez pas valider une mutation que vous avez saisie.")
    # Verrou sur les dossiers concernés (toujours dans l'ordre des id : pas d'interblocage) :
    # deux validations simultanées ne peuvent pas prendre le même numéro.
    ids = {m.dossier_id}
    if m.type == C.MUT_FUSION:
        from . import partages

        ids |= set(partages.ids_sources(m))
    verrouilles = {x.id: x for x in db.session.query(Dossier).filter(Dossier.id.in_(sorted(ids))).order_by(Dossier.id).with_for_update().all()}
    for x in verrouilles.values():
        db.session.refresh(x)
        if not dossier_mutable(x):
            raise MutationError(f"Le dossier {x.numero} est bloqué (problème signalé) : le Directeur doit d'abord lever le blocage." if x.est_bloque else f"Le dossier {x.numero} est clôturé ou n'est plus modifiable.")
        ok, _ = verifier_chaine(x)
        if not ok:
            raise MutationError(f"L'historique du dossier {x.numero} est altéré : validation impossible, contactez l'administrateur.")
    d = verrouilles[m.dossier_id]
    _controler_regles(m, etat(d))  # l'état a pu changer depuis la saisie

    sceller(d, m, user)
    audit("mutation_validee", "mutation", m.id, nouveau=f"{m.type} n°{m.seq} dossier {d.numero}", user=user)
    if m.type in C.PARTAGES:
        from . import partages

        partages.appliquer(m, user, verrouilles)
    elif m.type in C.TRANSFERTS:
        from . import transmission

        transmission.creer_successeur(m, user, d)
    elif m.type == C.MUT_RECTIFICATION and m.details:
        from . import corrections

        corrections.appliquer_mutation(m, user, d)
    # Les parts sont cédées ou la parcelle est hypothéquée : le système génère l'acte correspondant (imprimable)
    from . import generation

    db.session.flush()
    generation.pour_mutation(m, user)


def rejeter(user, m, motif):
    if not peut_decider(user):
        raise MutationError("Seul le Directeur peut rejeter une mutation.")
    if m.statut != C.MS_ATTENTE:
        raise MutationError("Cette mutation a déjà été traitée.")
    motif = (motif or "").strip()
    if not motif:
        raise MutationError("Le motif du rejet est obligatoire.")
    m.statut = C.MS_REJETEE
    m.decision_par_id = user.id
    m.decision_at = maintenant().replace(microsecond=0)
    m.motif_rejet = motif
    audit("mutation_rejetee", "mutation", m.id, nouveau=motif, user=user)


# --------------------------------------------------------------------------
# Présentation
# --------------------------------------------------------------------------
def _corrections_de(m):
    if m.type != C.MUT_RECTIFICATION or not m.details:
        return []
    from . import corrections

    return corrections.resume(corrections.charger(m))


def vue_historique(d):
    """Mutations prêtes à afficher (plus récentes d'abord), avec leurs parties et leur état."""
    annulees = annulees_ids(valides(d))
    lignes = []
    for m in sorted(d.mutations, key=lambda x: x.id, reverse=True):
        def noms(role):
            return [(p.proprietaire.nom_complet, formater_part(_part_de(p)) if p.part_num else None) for p in m.parties if p.role == role]

        lignes.append(
            {
                "m": m,
                "libelle": "Correction des données" if (m.type == C.MUT_RECTIFICATION and m.details) else C.MUTATIONS[m.type],
                "cedants": noms(C.ROLE_CEDANT),
                "beneficiaires": noms(C.ROLE_BENEFICIAIRE),
                "creanciers": noms(C.ROLE_CREANCIER),
                "annulee": m.id in annulees,
                "cible": m.lie_a,
                "corrections": _corrections_de(m),
                # parcelles créées par cette mutation (division / fusion) : [(libellé, dossier)]
                "issues": [(f.libelle, f.resultat) for f in Filiation.query.filter_by(mutation_id=m.id).order_by(Filiation.id)] if (m.type in C.PARTAGES or m.type in C.TRANSFERTS) else [],
            }
        )
    return lignes


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _dossier_ou_404(dossier_id):
    from . import workflow as W

    d = db.session.get(Dossier, dossier_id)
    if d is None or not peut_consulter(current_user) or not W.peut_voir(d, current_user):
        abort(404)
    return d


@bp.route("/dossiers/<int:dossier_id>/mutations/nouvelle", methods=["GET", "POST"])
@login_required
def nouvelle(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_saisir(current_user, d):
        abort(403)
    et = etat(d)
    from . import historique as H

    aire, source_aire = H.superficie(d)

    def page(code=200):
        return (
            render_template(
                "dossiers/mutation_form.html",
                d=d,
                et=et,
                aire=aire,
                source_aire=source_aire,
                fmt_m2=H.formater_m2,
                f=request.form if request.method == "POST" else request.args,  # ?type=vente présélectionne le type
                types={k: v for k, v in C.MUTATIONS.items() if k not in C.PARTAGES and k != C.MUT_RECTIFICATION},
                transferts=C.TRANSFERTS,
                formater_part=formater_part,
                today=date.today().isoformat(),
            ),
            code,
        )

    if request.method == "GET":
        return page()

    cles = []
    try:
        m = construire(d, current_user, request.form)
        fichiers = []
        if any(f and f.filename for f in request.files.getlist("fichiers")):
            fichiers = Doc.preparer(request.files.getlist("fichiers"), request.form.getlist("type_document"), request.form.getlist("libelle_document"))
        db.session.add(m)
        db.session.flush()
        if fichiers:
            for doc in Doc.enregistrer(d, fichiers, None, current_user, cles):
                doc.mutation_id = m.id
        audit("mutation_saisie", "mutation", m.id, nouveau=f"{m.type} dossier {d.numero}")
        db.session.commit()
    except (MutationError, Doc.DocumentError, StorageError) as e:
        db.session.rollback()
        if cles:
            Doc.nettoyer(cles)
        flash(str(e), "danger")
        return page(400)
    except Exception:
        db.session.rollback()
        if cles:
            Doc.nettoyer(cles)
        raise
    flash("Mutation enregistrée : elle attend la validation du Directeur.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#tracabilite")


def _mutation_ou_404(mutation_id):
    from . import workflow as W

    m = db.session.get(Mutation, mutation_id)
    if m is None or not peut_consulter(current_user) or not W.peut_voir(m.dossier, current_user):
        abort(404)
    return m


def nb_en_attente(user):
    """Mutations en attente de validation, dans le périmètre (île) du compte."""
    from . import workflow as W

    q = Mutation.query.join(Dossier, Dossier.id == Mutation.dossier_id).filter(Mutation.statut == C.MS_ATTENTE)
    return W.restreindre_ile(q, user).count()


@bp.route("/mutations/<int:mutation_id>/valider", methods=["POST"])
@login_required
def valider_route(mutation_id):
    m = _mutation_ou_404(mutation_id)
    try:
        valider(current_user, m)
        db.session.commit()
        message = f"Mutation validée (n° {m.seq} de l'historique)."
        nouveau = Filiation.query.filter_by(mutation_id=m.id).first() if m.type in C.TRANSFERTS else None
        if nouveau is not None:
            message += f" Un nouveau dossier a été créé : {nouveau.resultat.numero}."
        flash(message, "success")
    except MutationError as e:
        db.session.rollback()
        flash(str(e), "danger")
    except IntegrityError:
        db.session.rollback()
        flash("Une autre validation vient d'avoir lieu sur ce dossier : réessayez.", "warning")
    except StorageError as e:
        db.session.rollback()  # tout ou rien : aucune parcelle créée ni clôturée
        flash(f"{e} La mutation reste en attente : réessayez.", "danger")
    return redirect(url_for("main.detail_dossier", dossier_id=m.dossier_id) + "#tracabilite")


@bp.route("/mutations/<int:mutation_id>/acte")
@login_required
def acte(mutation_id):
    """Acte imprimable d'une mutation validée (vente, donation, héritage, expropriation, hypothèque, mainlevée) :
    ouvre l'exemplaire déjà généré, ou le génère s'il n'existe pas encore (mutations validées avant la génération automatique)."""
    from flask import Response

    from . import generation

    m = _mutation_ou_404(mutation_id)
    if m.statut != C.MS_VALIDEE or generation.code_acte(m) is None:
        abort(404)
    if current_user.role not in (C.ADMIN, C.DIRECTEUR, C.DOMAINE) or m.dossier.est_bloque:
        abort(403)
    try:
        emis, contenu = generation.acte_de(m, current_user)
        if contenu is not None:
            audit("impression", "dossier", m.dossier_id, nouveau=f"{emis.type} copie {emis.copie_n} (mutation n° {m.seq})")
            db.session.commit()
    except StorageError:
        abort(503)
    if contenu is None:                                   # exemplaire déjà généré : on le rouvre à l'identique
        return redirect(url_for("main.exemplaire_pdf", rec_id=emis.id))
    return Response(
        contenu,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{emis.type}-{m.dossier.numero}-n{m.seq}.pdf"', "Cache-Control": "no-store"},
    )


@bp.route("/mutations/<int:mutation_id>/rejeter", methods=["POST"])
@login_required
def rejeter_route(mutation_id):
    m = _mutation_ou_404(mutation_id)
    try:
        rejeter(current_user, m, request.form.get("motif"))
        db.session.commit()
        flash("Mutation rejetée.", "success")
    except MutationError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("main.detail_dossier", dossier_id=m.dossier_id) + "#tracabilite")


# --------------------------------------------------------------------------
# Page « Mutations » : vue d'ensemble, et point de départ d'une vente, donation, héritage…
# --------------------------------------------------------------------------
CARTES = [
    # (type, titre, description, icône)
    (C.MUT_VENTE, "Vente", "Un propriétaire vend tout ou partie de sa part.", "cash-coin"),
    (C.MUT_DONATION, "Donation", "Un propriétaire donne tout ou partie de sa part.", "gift"),
    (C.MUT_HERITAGE, "Héritage", "Succession : le défunt transmet à ses héritiers (indivision possible).", "people"),
    (C.MUT_EXPROPRIATION, "Expropriation", "Transfert à l'État ou à une collectivité.", "bank"),
    (C.MUT_HYPOTHEQUE, "Hypothèque", "Inscription d'une garantie au profit d'un créancier.", "lock"),
    (C.MUT_MAINLEVEE, "Mainlevée", "Levée d'une hypothèque devenue sans objet.", "unlock"),
    (C.MUT_DIVISION, "Division", "Partage d'une parcelle en plusieurs parcelles filles.", "diagram-2"),
    (C.MUT_FUSION, "Fusion", "Regroupement de parcelles contiguës en une seule.", "intersect"),
    (C.MUT_RECTIFICATION, "Correction", "Corriger la commune, la superficie, le nom d'un propriétaire… (validée par le Directeur).", "pencil-square"),
    (C.MUT_ANNULATION, "Annulation", "Annule la dernière mutation d'un dossier.", "arrow-counterclockwise"),
]


def _resume_parties(m):
    def noms(role):
        return [(p.proprietaire.nom_complet, formater_part(_part_de(p)) if p.part_num else None) for p in m.parties if p.role == role]

    return {"cedants": noms(C.ROLE_CEDANT), "beneficiaires": noms(C.ROLE_BENEFICIAIRE), "creanciers": noms(C.ROLE_CREANCIER)}


@bp.route("/mutations")
@login_required
def liste():
    if not peut_consulter(current_user):
        abort(404)
    from sqlalchemy.orm import aliased

    from . import workflow as W

    q = Mutation.query.join(Dossier, Dossier.id == Mutation.dossier_id).filter(
        Mutation.details.is_(None) | ~Mutation.details.like('%"reportee_de"%')   # les hypothèques reportées sur un nouveau dossier ne sont pas de nouvelles mutations
    )
    q = W.restreindre_ile(q, current_user)
    type_ = request.args.get("type", "")
    statut = request.args.get("statut", "")
    texte = (request.args.get("q") or "").strip()
    du, au = request.args.get("du", ""), request.args.get("au", "")

    if type_ in C.MUTATIONS:
        q = q.filter(Mutation.type == type_)
    if statut in C.MUTATION_STATUTS:
        q = q.filter(Mutation.statut == statut)
    for valeur, sens in ((du, ">="), (au, "<=")):
        try:
            jour = date.fromisoformat(valeur) if valeur else None
        except ValueError:
            jour = None
        if jour:
            q = q.filter(Mutation.date_acte >= jour if sens == ">=" else Mutation.date_acte <= jour)
    if texte:
        motif = f"%{texte}%"
        personne = aliased(Proprietaire)
        via_parties = (
            db.session.query(MutationPartie.mutation_id)
            .join(personne, personne.id == MutationPartie.proprietaire_id)
            .filter(critere_nom(personne, motif))
        )
        q = q.filter(Dossier.numero.ilike(motif) | Mutation.reference_acte.ilike(motif) | Mutation.id.in_(via_parties))

    page = request.args.get("page", 1, type=int)
    pagination = q.order_by(Mutation.id.desc()).paginate(page=page, per_page=20, error_out=False)
    lignes = [{"m": m, "libelle": C.MUTATIONS[m.type], "corrections": _corrections_de(m), **_resume_parties(m)} for m in pagination.items]
    return render_template(
        "mutations/liste.html",
        pagination=pagination,
        lignes=lignes,
        cartes=CARTES if current_user.role == C.DOMAINE else [],
        types=C.MUTATIONS,
        statuts=C.MUTATION_STATUTS,
        filtres={"type": type_, "statut": statut, "q": texte, "du": du, "au": au},
        nb_attente=nb_en_attente(current_user),
    )


@bp.route("/mutations/nouvelle")
@login_required
def choisir():
    """Étape 1 d'une mutation lancée depuis le menu : choisir le dossier concerné."""
    if current_user.role != C.DOMAINE:
        abort(403)
    type_ = request.args.get("type", "")
    if type_ not in C.MUTATIONS:
        abort(404)
    from . import workflow as W
    from .models import Parcelle

    texte = (request.args.get("q") or "").strip()
    q = Dossier.query.join(Proprietaire, Proprietaire.id == Dossier.proprietaire_id).join(Parcelle, Parcelle.id == Dossier.parcelle_id)
    q = W.restreindre_ile(q.filter(Dossier.statut.in_([C.CERTIFIE, C.ARCHIVE])), current_user)
    if texte:
        q = W.appliquer_recherche(q, texte)

    lignes = []
    for d in q.order_by(Dossier.id.desc()).limit(150).all():
        et = etat(d)
        if type_ == C.MUT_MAINLEVEE and not et.charges:
            continue
        if type_ == C.MUT_ANNULATION and et.derniere is None:
            continue
        raison = None
        if type_ in C.PARTAGES:
            from . import partages

            if et.charges:
                raison = "Hypothèque active : faites d'abord une mainlevée."
            elif partages._autres_en_attente(d):
                raison = "Une mutation est en attente de validation sur ce dossier."
            elif type_ == C.MUT_FUSION:
                raison = None
        lien = (
            url_for("partages.division", dossier_id=d.id) if type_ == C.MUT_DIVISION
            else url_for("partages.fusion", dossier_id=d.id) if type_ == C.MUT_FUSION
            else url_for("corrections.corriger", dossier_id=d.id) if type_ == C.MUT_RECTIFICATION
            else url_for("mutations.nouvelle", dossier_id=d.id, type=type_)
        )
        lignes.append({"d": d, "lien": lien, "raison": raison, "charges": len(et.charges)})
        if len(lignes) >= 20:
            break
    titre = next(t for k, t, _, _ in CARTES if k == type_)
    return render_template("mutations/choisir.html", type_=type_, titre=titre, lignes=lignes, q=texte)
