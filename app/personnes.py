"""Personnes : regroupement des fiches « propriétaire » et patrimoine d'une personne.

Chaque dossier crée sa propre fiche de personne ; la même personne existe donc en plusieurs exemplaires. Deux
fiches désignent la MÊME personne si elles portent le même numéro de pièce d'identité (espaces et casse ignorés).
Sans numéro, une fiche reste seule.

Les fiches ne sont JAMAIS fusionnées en base : leurs identifiants sont signés dans la chaîne des mutations, les
réécrire casserait les empreintes. Pour rapprocher deux fiches, on corrige la pièce d'identité (correction validée
par le Directeur) et le regroupement se fait tout seul.
"""

import json
from fractions import Fraction

from sqlalchemy import case, cast, func, literal, or_, select, String, union

from . import constants as C
from . import mutations as M
from . import workflow as W
from .extensions import db
from .audit import audit
from .models import Dossier, Filiation, ModificationPersonne, Mutation, MutationPartie, Parcelle, PartInitiale, Proprietaire, maintenant

PREFIXE_FICHE = "~fiche~"


# --------------------------------------------------------------------------
# Clé de regroupement (identique en Python et en SQL : espaces retirés, minuscules)
# --------------------------------------------------------------------------
def cle(p):
    identite = (p.identite or "").replace(" ", "").lower()
    return identite if identite else f"{PREFIXE_FICHE}{p.id}"


def _cle_sql():
    identite = func.lower(func.replace(func.coalesce(Proprietaire.identite, ""), " ", ""))
    return case((identite != "", identite), else_=literal(PREFIXE_FICHE) + cast(Proprietaire.id, String))


def visibles(user):
    """Fiches liées à au moins un dossier du périmètre du compte (sous-requête) ; None : aucune restriction.
    Un compte limité à une île ne voit pas les personnes des autres îles ; un topographe privé ne voit que les propriétaires
    des dossiers qu'il a ajoutés."""
    dans = W.condition_perimetre(user) if user is not None else None
    if dans is None:
        return None
    return union(
        select(Dossier.proprietaire_id).where(dans),
        select(PartInitiale.proprietaire_id).join(Dossier, Dossier.id == PartInitiale.dossier_id).where(dans),
        select(MutationPartie.proprietaire_id)
        .join(Mutation, Mutation.id == MutationPartie.mutation_id)
        .join(Dossier, Dossier.id == Mutation.dossier_id)
        .where(Mutation.statut == C.MS_VALIDEE, dans),
    )


def groupe(p, user=None):
    """Toutes les fiches qui désignent la même personne que `p` (p comprise), limitées à celles que `user` peut voir."""
    q = Proprietaire.query.filter(_cle_sql() == cle(p))
    vis = visibles(user)
    if vis is not None:
        q = q.filter(Proprietaire.id.in_(vis))
    return q.order_by(Proprietaire.id).all()


def representants(recherche=None, user=None):
    """Requête de UNE fiche par personne, filtrée sur un texte libre (nom, prénom, pièce d'identité, téléphone)."""
    cle_sql = _cle_sql()
    regroupement = select(func.min(Proprietaire.id)).group_by(cle_sql)
    vis = visibles(user)
    if vis is not None:
        regroupement = regroupement.where(Proprietaire.id.in_(vis))
    if recherche:
        motif = f"%{recherche}%"
        complet = func.coalesce(Proprietaire.prenom, "") + literal(" ") + Proprietaire.nom
        inverse = Proprietaire.nom + literal(" ") + func.coalesce(Proprietaire.prenom, "")
        correspondants = select(cle_sql).where(
            or_(
                Proprietaire.nom.ilike(motif), Proprietaire.prenom.ilike(motif), Proprietaire.identite.ilike(motif),
                Proprietaire.telephone.ilike(motif), complet.ilike(motif), inverse.ilike(motif),
            )
        )
        regroupement = regroupement.where(cle_sql.in_(correspondants))
    return Proprietaire.query.filter(Proprietaire.id.in_(regroupement))


def fiche_principale(fiches):
    """La fiche la plus complète du groupe (à égalité, la plus récente) : c'est elle qu'on affiche."""
    def remplissage(p):
        return sum(bool(x) for x in (p.nom, p.prenom, p.identite, p.telephone, p.adresse))

    return max(fiches, key=lambda p: (remplissage(p), p.id))


# --------------------------------------------------------------------------
# Patrimoine
# --------------------------------------------------------------------------
def _aire(d):
    g = d.geometrie_courante
    if g is not None:
        return g.superficie_m2, "tracé"
    if d.parcelle.superficie_m2:
        return d.parcelle.superficie_m2, "déclarée"
    return None, None


def _dossiers_concernes(ids):
    trouves = {i for (i,) in db.session.query(Dossier.id).filter(Dossier.proprietaire_id.in_(ids))}
    trouves |= {i for (i,) in db.session.query(PartInitiale.dossier_id).filter(PartInitiale.proprietaire_id.in_(ids))}
    trouves |= {
        i
        for (i,) in db.session.query(Mutation.dossier_id)
        .join(MutationPartie, MutationPartie.mutation_id == Mutation.id)
        .filter(MutationPartie.proprietaire_id.in_(ids), Mutation.statut == C.MS_VALIDEE)
    }
    return sorted(trouves)


def _fin_de_propriete(d):
    """Pour un dossier clôturé : (date de clôture, verbe, dossier suivant)."""
    cloture = next((e for e in reversed(d.etapes) if e.vers_statut == C.CLOTURE), None)
    quand = cloture.created_at if cloture else None
    suite = d.descendances[0] if d.descendances else None
    verbe = {"division": "Parcelle divisée", "fusion": "Parcelle fusionnée"}.get(suite.type if suite else "", None)
    if verbe is None and suite is not None:
        verbe = f"Propriété transférée ({C.MUTATIONS[suite.type].lower()})" if suite.type in C.TRANSFERTS else "Clôturé"
    return quand, verbe or "Clôturé", [f.resultat for f in d.descendances]


def biens(fiches, user=None):
    """Patrimoine d'une personne (toutes ses fiches) : biens actuels, anciens biens, créances, parcours.
    Avec `user`, seuls les dossiers qu'il peut voir (son île) sont comptés."""
    ids = {p.id for p in fiches}
    actuels, anciens, creances = [], [], []
    total, sans_aire = 0.0, 0

    q_dossiers = Dossier.query.filter(Dossier.id.in_(_dossiers_concernes(ids)))
    if user is not None:
        q_dossiers = W.perimetre(q_dossiers, user)
    for d in q_dossiers.order_by(Dossier.id).all():
        et = M.etat(d)
        part = sum((f for p, f in et.parts if p.id in ids), Fraction(0))
        if part > 0:
            aire, source = _aire(d)
            ligne = {"dossier": d, "part": part, "aire": aire, "source_aire": source, "charges": len(et.charges)}
            if d.statut == C.CLOTURE:
                ligne["cloture_le"], ligne["fin"], ligne["suite"] = _fin_de_propriete(d)
                anciens.append(ligne)
            else:
                actuels.append(ligne)
                if aire:
                    total += float(part) * aire
                else:
                    sans_aire += 1
        # créancier : hypothèques inscrites à son profit
        for m in sorted(d.mutations, key=lambda m: m.id):
            if m.statut == C.MS_VALIDEE and m.type == C.MUT_HYPOTHEQUE and any(p.role == C.ROLE_CREANCIER and p.proprietaire_id in ids for p in m.parties):
                if m.details and '"reportee_de"' in m.details:
                    continue  # copie technique sur un dossier successeur : on compte l'hypothèque d'origine
                creances.append({"dossier": d, "mutation": m, "active": m in et.charges})

    return {
        "actuels": actuels,
        "anciens": sorted(anciens, key=lambda l: l["cloture_le"] or l["dossier"].created_at, reverse=True),
        "creances": creances,
        "parcours": parcours(ids, user),
        "superficie_totale": total,
        "sans_superficie": sans_aire,
    }


def parcours(ids, user=None):
    """Mutations validées où la personne intervient (cédant, bénéficiaire, créancier), les plus récentes d'abord."""
    q = (
        Mutation.query.join(MutationPartie, MutationPartie.mutation_id == Mutation.id)
        .join(Dossier, Dossier.id == Mutation.dossier_id)
        .filter(MutationPartie.proprietaire_id.in_(ids), Mutation.statut == C.MS_VALIDEE)
    )
    if user is not None:
        q = W.perimetre(q, user)
    mutations = q.order_by(Mutation.date_acte.desc(), Mutation.id.desc()).distinct().all()
    evenements = []
    for m in mutations:
        if m.details and '"reportee_de"' in m.details:
            continue
        mes_roles = {p.role for p in m.parties if p.proprietaire_id in ids}

        def autres(role):
            return ", ".join(
                f"{p.proprietaire.nom_complet}" + (f" ({M.formater_part(M._part_de(p))})" if p.part_num else "")
                for p in m.parties
                if p.role == role and p.proprietaire_id not in ids
            )

        if m.type in C.TRANSFERTS:
            if C.ROLE_BENEFICIAIRE in mes_roles:
                role, texte = "Acquiert", f"de {autres(C.ROLE_CEDANT) or '—'}"
            else:
                role, texte = "Cède", f"à {autres(C.ROLE_BENEFICIAIRE) or '—'}"
            nature = f"{role} par {C.MUTATIONS[m.type].lower().split(' ')[0]}"
        elif m.type == C.MUT_HYPOTHEQUE:
            nature, texte = "Créancier hypothécaire", "au titre d'une hypothèque"
        else:
            continue
        suite = Filiation.query.filter_by(mutation_id=m.id).first()
        evenements.append(
            {
                "date": m.date_acte,
                "nature": nature,
                "detail": texte,
                "reference": m.reference_acte,
                "montant": m.montant,
                "dossier": m.dossier,
                "suite": suite.resultat if suite else None,
            }
        )
    return evenements


def similaires(p, fiches, limite=10, user=None):
    """Fiches d'AUTRES personnes qui y ressemblent (même nom et prénom, ou même téléphone) : doublons possibles."""
    ids = {x.id for x in fiches}
    conditions = [
        (func.lower(Proprietaire.nom) == p.nom.lower()) & (func.lower(func.coalesce(Proprietaire.prenom, "")) == (p.prenom or "").lower())
    ]
    if p.telephone:
        conditions.append(Proprietaire.telephone == p.telephone)
    q = Proprietaire.query.filter(or_(*conditions), ~Proprietaire.id.in_(ids))
    vis = visibles(user)
    if vis is not None:
        q = q.filter(Proprietaire.id.in_(vis))
    candidates = q.order_by(Proprietaire.id).all()
    vus, resultat = set(), []
    for c in candidates:
        if cle(c) in vus or cle(c) == cle(p):
            continue
        vus.add(cle(c))
        resultat.append(c)
    return resultat[:limite]


# --------------------------------------------------------------------------
# Modification de l'état civil : le Domaine propose, le Directeur valide
# --------------------------------------------------------------------------
class ModificationError(Exception):
    """Refus affichable à l'utilisateur."""


def _co():
    from . import corrections  # import tardif : corrections dépend de mutations

    return corrections


def peut_proposer(user):
    return user.role == C.DOMAINE


def peut_decider(user):
    return user.role == C.DIRECTEUR


def valeurs(p):
    return _co()._valeurs_proprietaire(p)


def en_attente(fiches):
    ids = [p.id for p in fiches]
    return ModificationPersonne.query.filter(ModificationPersonne.proprietaire_id.in_(ids), ModificationPersonne.statut == "en_attente").order_by(ModificationPersonne.id).all()


def lire_formulaire(principale, form):
    """(changements, motif) : seulement ce qui change réellement. Le nom et la pièce d'identité restent obligatoires."""
    co = _co()
    try:
        motif = co._texte(form.get("motif"), "Le motif de la modification", 500, obligatoire=True)
        courant, changements = valeurs(principale), {}
        for champ, _ in co.CHAMPS_PROPRIETAIRE:
            if champ not in form:
                continue
            nouveau = co._nettoyer_proprietaire(champ, form.get(champ))
            if champ == "identite" and not nouveau:
                raise co.CorrectionError("La pièce d'identité est obligatoire : elle permet de reconnaître la personne d'un dossier à l'autre.")
            if not co._meme(courant[champ], nouveau):
                changements[champ] = [courant[champ], nouveau]
    except co.CorrectionError as e:
        raise ModificationError(str(e)) from None
    if not changements:
        raise ModificationError("Aucune modification : changez au moins une valeur.")
    return changements, motif


def charger(m):
    return json.loads(m.changements)


def lignes(m):
    """Lignes affichables : (libellé, avant, après)."""
    libelle = _co().LIBELLE
    return [(libelle[c], av or "—", ap or "—") for c, (av, ap) in charger(m).items()]


def rapprochements(m, user=None):
    """Autres personnes déjà enregistrées avec la NOUVELLE pièce d'identité : la validation les regrouperait avec celle-ci."""
    nouvelle = charger(m).get("identite")
    if not nouvelle:
        return []
    fiches = groupe(m.proprietaire)
    cle_nouvelle = nouvelle[1].replace(" ", "").lower()
    ids = {f.id for f in fiches}
    q = Proprietaire.query.filter(_cle_sql() == cle_nouvelle)
    vis = visibles(user)
    if vis is not None:
        q = q.filter(Proprietaire.id.in_(vis))
    return [p for p in q.order_by(Proprietaire.id).all() if p.id not in ids]


def _refuser_si_dossier_bloque(fiches):
    """Modifier une personne modifie tous ses dossiers : un dossier bloqué (problème signalé) l'interdit."""
    from .blocages import ids_bloques

    bloques = ids_bloques(_dossiers_concernes({p.id for p in fiches}))
    if bloques:
        numeros = ", ".join(d.numero for d in Dossier.query.filter(Dossier.id.in_(bloques)).order_by(Dossier.id))
        raise ModificationError(f"Cette personne a un dossier bloqué (problème signalé) : {numeros}. Le Directeur doit d'abord lever le blocage.")


def proposer(user, principale, fiches, changements, motif):
    if not peut_proposer(user):
        raise ModificationError("Seul le Domaine peut proposer une modification.")
    _refuser_si_dossier_bloque(fiches)
    if en_attente(fiches):
        raise ModificationError("Une demande de modification est déjà en attente pour cette personne : attendez la décision du Directeur.")
    m = ModificationPersonne(
        proprietaire_id=principale.id,
        changements=json.dumps(changements, sort_keys=True, ensure_ascii=False),
        motif=motif,
        saisi_par_id=user.id,
    )
    db.session.add(m)
    db.session.flush()
    audit("personne_modification_demandee", "proprietaire", principale.id, nouveau=f"demande n°{m.id} : " + " ; ".join(f"{a} : {b} → {c}" for a, b, c in lignes(m)) + f" | motif : {motif}", user=user)
    return m


def _a_traiter(user, m, attendu_directeur=True):
    if not peut_decider(user):
        raise ModificationError("Seul le Directeur peut décider d'une modification.")
    if m.statut != "en_attente":
        raise ModificationError("Cette demande a déjà été traitée.")


def valider(user, m):
    _a_traiter(user, m)
    if m.saisi_par_id == user.id:
        raise ModificationError("Vous ne pouvez pas valider une demande que vous avez saisie.")
    fiches = groupe(m.proprietaire)
    _refuser_si_dossier_bloque(fiches)
    actuel = valeurs(fiche_principale(fiches))
    co = _co()
    for champ, (avant, _) in charger(m).items():
        if not co._meme(actuel[champ], avant):
            raise ModificationError(f"« {co.LIBELLE[champ]} » a changé depuis la saisie de cette demande : elle n'est plus applicable telle quelle. Rejetez-la et refaites-la.")
    changements = charger(m)
    for fiche in fiches:
        for champ, (_, apres) in changements.items():
            setattr(fiche, champ, apres)
    m.statut = "validee"
    m.decision_par_id = user.id
    m.decision_at = maintenant().replace(microsecond=0)
    audit("personne_modification_validee", "proprietaire", m.proprietaire_id, ancien=m.changements, nouveau=f"demande n°{m.id} appliquée à {len(fiches)} fiche(s)", user=user)


def rejeter(user, m, motif):
    _a_traiter(user, m)
    motif = (motif or "").strip()
    if not motif:
        raise ModificationError("Le motif du rejet est obligatoire.")
    m.statut = "rejetee"
    m.decision_par_id = user.id
    m.decision_at = maintenant().replace(microsecond=0)
    m.motif_rejet = motif
    audit("personne_modification_rejetee", "proprietaire", m.proprietaire_id, nouveau=f"demande n°{m.id} : {motif}", user=user)


def demandes_en_attente(user):
    """Demandes de modification en attente portant sur une personne visible par `user`."""
    q = ModificationPersonne.query.filter(ModificationPersonne.statut == "en_attente")
    vis = visibles(user)
    if vis is not None:
        q = q.filter(ModificationPersonne.proprietaire_id.in_(vis))
    return q


def carte(patrimoine, avec_anciens=True):
    """Carte des biens d'une personne : biens actuels (couleur du statut) et anciens biens (gris pointillé).
    Retourne {"features": [...], "sans_trace": nombre de biens sans tracé, "nb_actuels": ..., "nb_anciens": ...} ; seuls les biens du
    patrimoine déjà limité à l'île du compte sont concernés."""
    import json

    from flask import url_for

    from .carte import COULEURS_CARTE

    features, sans_trace = [], 0
    for ancien, lignes in ((False, patrimoine["actuels"]), (True, patrimoine["anciens"] if avec_anciens else [])):
        for l in lignes:
            d = l["dossier"]
            g = d.geometrie_courante
            if g is None:
                sans_trace += 1
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": json.loads(g.geojson),
                    "properties": {
                        "numero": d.numero,
                        "url": url_for("main.detail_dossier", dossier_id=d.id),
                        "part": M.formater_part(l["part"]),
                        "superficie_m2": round(g.superficie_m2),
                        "statut": "Ancien bien : " + l["fin"] if ancien else d.statut_label,
                        "couleur": "#8a94a0" if ancien else COULEURS_CARTE.get(d.statut, "#2a78d6"),
                        "ancien": ancien,
                        "localisation": f"{d.parcelle.commune}" + (f" · {d.parcelle.village}" if d.parcelle.village else ""),
                    },
                }
            )
    legende, vus = [], set()
    for f in features:                                          # une entrée par statut réellement présent sur la carte
        pr = f["properties"]
        cle_leg = ("Ancien bien (vendu, divisé ou fusionné)", pr["couleur"]) if pr["ancien"] else (pr["statut"], pr["couleur"])
        if cle_leg not in vus:
            vus.add(cle_leg)
            legende.append({"libelle": cle_leg[0], "couleur": cle_leg[1]})
    return {
        "type": "FeatureCollection",
        "features": features,
        "legende": legende,
        "sans_trace": sans_trace,
        "nb_actuels": sum(1 for f in features if not f["properties"]["ancien"]),
        "nb_anciens": sum(1 for f in features if f["properties"]["ancien"]),
    }
