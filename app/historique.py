"""Contenu « juridique » d'un dossier pour les documents officiels : charges, mutations, lignée.

Ces fonctions ne font que LIRE ce que l'application a déjà enregistré (mutations validées et signées, liens de
filiation). Elles servent à l'extrait imprimé et à la signature du QR : un extrait imprimé devient « non à jour »
dès qu'une hypothèque, une mutation ou une correction est validée après son impression.
"""

import json

from . import arbre as Ab
from . import constants as C
from . import mutations as M
from .extensions import db
from .models import Filiation, Mutation


def superficie(d):
    """(m², origine) de la parcelle : mesurée sur le tracé en vigueur, sinon déclarée ; (None, None) si inconnue."""
    g = d.geometrie_courante
    if g is not None:
        return g.superficie_m2, "tracé"
    if d.parcelle.superficie_m2:
        return d.parcelle.superficie_m2, "déclarée"
    return None, None


def formater_m2(valeur):
    """« 1 200 m² », « 171,43 m² » ; « - » si la superficie est inconnue."""
    if valeur is None:
        return "-"
    texte = f"{valeur:,.2f}".replace(",", " ").replace(".", ",")
    if texte.endswith(",00"):
        texte = texte[:-3]
    elif texte.endswith("0"):
        texte = texte[:-1]                                  # « 450,50 » -> « 450,5 »
    return texte + " m²"


def metres_carres(part, aire):
    """Superficie (m²) correspondant à une quote-part (Fraction) d'une parcelle de `aire` m², arrondie au centième."""
    return round(float(part) * aire, 2) if (aire and part) else None


def _parts(m, role):
    return [
        (p.proprietaire.nom_complet, M.formater_part(M._part_de(p)) if p.part_num else None)
        for p in m.parties
        if p.role == role
    ]


def _noms(items):
    return ", ".join(f"{nom} ({part})" if part else nom for nom, part in items) or "—"


def _montant(valeur):
    """12 500 000 KMF, avec des espaces insécables : un montant ne se coupe jamais en fin de ligne."""
    return f"{valeur:,} KMF".replace(",", "\u00a0").replace(" KMF", "\u00a0KMF") if valeur else None


# --------------------------------------------------------------------------
# Charges
# --------------------------------------------------------------------------
def charges_en_vigueur(d):
    """Hypothèques actives, de la plus ancienne à la plus récente."""
    lignes = []
    for c in sorted(M.etat(d).charges, key=lambda c: (c.date_acte, c.id)):
        creancier = next((p.proprietaire.nom_complet for p in c.parties if p.role == C.ROLE_CREANCIER), "—")
        lignes.append(
            {
                "creancier": creancier,
                "montant": c.montant,
                "date_acte": c.date_acte,
                "reference": c.reference_acte,
                "reportee": bool(c.details and '"reportee_de"' in c.details),
            }
        )
    return lignes


# --------------------------------------------------------------------------
# Mutations enregistrées sur CE dossier
# --------------------------------------------------------------------------
def _decrire(m):
    """(nature, détail) en texte simple."""
    if m.type in C.TRANSFERTS:
        detail = f"{_noms(_parts(m, C.ROLE_CEDANT))} cède à {_noms(_parts(m, C.ROLE_BENEFICIAIRE))}"
        suite = Filiation.query.filter_by(mutation_id=m.id).first()
        if suite is not None:
            detail += f" ; nouveau dossier {suite.resultat.numero}"
        return Ab.LIBELLES[m.type], detail
    if m.type == C.MUT_HYPOTHEQUE:
        creancier = _noms(_parts(m, C.ROLE_CREANCIER))
        if m.details and '"reportee_de"' in m.details:
            origine = json.loads(m.details).get("origine", "")
            return "Hypothèque reportée", f"au profit de {creancier} ; reportée depuis le dossier {origine}"
        return "Hypothèque inscrite", f"au profit de {creancier}"
    if m.type == C.MUT_MAINLEVEE:
        cible = m.lie_a
        return "Mainlevée d'hypothèque", f"levée de l'hypothèque du {cible.date_acte:%d/%m/%Y}" if cible else "levée d'une hypothèque"
    if m.type == C.MUT_RECTIFICATION:
        if m.details:
            from . import corrections

            # « devient » plutôt qu'une flèche : la police des PDF ne sait pas écrire « → »
            return "Correction des données", corrections.texte_resume(corrections.charger(m), fleche=" devient ")
        return "Rectification", m.notes or ""
    if m.type == C.MUT_ANNULATION:
        cible = m.lie_a
        return "Annulation", (f"de la mutation n° {cible.seq}" if cible and cible.seq else "d'une mutation") + (f" : {m.notes}" if m.notes else "")
    if m.type in C.PARTAGES:
        suites = Filiation.query.filter_by(mutation_id=m.id).all()
        return Ab.LIBELLES[m.type], ", ".join(f.resultat.numero for f in suites)
    return C.MUTATIONS.get(m.type, m.type), ""


def mutations_du_dossier(d):
    """Mutations VALIDÉES de ce dossier, dans l'ordre de l'historique signé (les rejets et les attentes n'en font pas partie)."""
    valides = M.valides(d)
    annulees = {m.lie_a_id: m for m in valides if m.type == C.MUT_ANNULATION}
    lignes = []
    for m in valides:
        nature, detail = _decrire(m)
        lignes.append(
            {
                "seq": m.seq,
                "date": m.date_acte,
                "nature": nature,
                "detail": detail,
                "reference": m.reference_acte,
                "montant": _montant(m.montant),
                "validee": m.decision_at,
                "annulee_par": annulees[m.id].seq if m.id in annulees else None,
            }
        )
    return lignes


def tete_de_chaine(d):
    """Empreinte (courte) de la dernière mutation validée : change à chaque nouvelle mutation, correction ou charge."""
    valides = M.valides(d)
    return valides[-1].hash[:12] if valides else None


# --------------------------------------------------------------------------
# Lignée (origine de propriété)
# --------------------------------------------------------------------------
def _evenement(f):
    m = db.session.get(Mutation, f.mutation_id) if f.mutation_id else None
    texte = Ab.LIBELLES[f.type] + (f" ({f.libelle})" if f.libelle else "")
    if m is not None:
        texte += f" du {m.date_acte:%d/%m/%Y}"
        if m.reference_acte:
            texte += f", acte : {m.reference_acte}"
        if f.type == C.MUT_VENTE and m.montant:
            texte += f", {_montant(m.montant)}"
    return f"{texte} (dossier {f.resultat.numero})"


def lignee(d):
    """Dossiers dont celui-ci descend, du plus ancien au plus récent, puis le dossier lui-même.

    Chaque ligne indique le propriétaire à l'époque et la mutation qui a donné le dossier suivant.
    Vide si le dossier n'a pas d'ascendance (parcelle d'origine)."""
    ancetres, liens, a_voir = {}, {}, [d]
    while a_voir:
        x = a_voir.pop()
        for f in x.origines:
            liens[f.id] = f
            if f.source_id not in ancetres:
                ancetres[f.source_id] = f.source
                a_voir.append(f.source)
    if not ancetres:
        return []
    lignes = []
    for x in sorted(ancetres.values(), key=lambda x: x.id) + [d]:
        sortants = [f for f in sorted(liens.values(), key=lambda f: f.id) if f.source_id == x.id]
        lignes.append(
            {
                "numero": x.numero,
                "proprietaires": x.proprietaire_label,
                "statut": x.statut_label,
                "courant": x.id == d.id,
                "evenements": [_evenement(f) for f in sortants],
            }
        )
    return lignes
