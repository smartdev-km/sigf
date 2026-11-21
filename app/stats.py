"""Statistiques du tableau de bord.

Toutes les fonctions prennent une île (ou None) et une date de référence `ref`, pour que les résultats
soient déterministes et testables. Le regroupement par mois se fait en Python (identique sur SQLite
et PostgreSQL). Aucun chiffre ne dépend des droits : les écrans qui les affichent excluent le
topographe privé, qui n'a qu'une vue limitée à ses propres dossiers.
"""

from datetime import datetime, timedelta

from sqlalchemy import func

from . import constants as C
from .extensions import db
from .models import Dossier, EtapeLog, Filiation, Mutation, Parcelle

# Étapes « en cours », dans l'ordre du circuit
ETAPES = [C.PLAN_CROQUIS, C.ATTENTE_AVIS, C.SAISIE_RSP, C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR]
# Qui doit agir à chaque étape
RESPONSABLE = {
    C.PLAN_CROQUIS: "Topographie",
    C.ATTENTE_AVIS: "Topographe privé",
    C.SAISIE_RSP: "Topographie publique",
    C.ENREGISTREMENT_RCP: "Domaine",
    C.VALIDATION_DIRECTEUR: "Directeur",
}
# Noms courts pour les graphiques (les libellés complets restent dans les tableaux et les fiches)
NOMS_COURTS = {
    C.PLAN_CROQUIS: "Plan croquis",
    C.ATTENTE_AVIS: "Attente d'avis",
    C.SAISIE_RSP: "Saisie du RTC",
    C.ENREGISTREMENT_RCP: "Enregistrement RTS",
    C.VALIDATION_DIRECTEUR: "Validation Directeur",
}
# Ce que l'utilisateur a FAIT à chaque transition, à la forme verbale (fil d'activité)
VERBES = {
    (C.PLAN_CROQUIS, C.ATTENTE_AVIS): "a terminé le plan croquis",
    (C.PLAN_CROQUIS, C.SAISIE_RSP): "a terminé le plan croquis",
    (C.ATTENTE_AVIS, C.SAISIE_RSP): "a envoyé son avis",
    (C.SAISIE_RSP, C.ENREGISTREMENT_RCP): "a saisi le numéro RTC",
    (C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR): "a saisi le numéro RTS",
    (C.VALIDATION_DIRECTEUR, C.CERTIFIE): "a validé et émis le certificat",
}
MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
PERIODES = (3, 6, 12)
SEUIL_RETARD_JOURS = 30


# --------------------------------------------------------------------------
# Calendrier
# --------------------------------------------------------------------------
def decaler_mois(annee, mois, delta):
    n = annee * 12 + (mois - 1) + delta
    return n // 12, n % 12 + 1


def fenetre_mois(ref, nb):
    """[(année, mois)] des `nb` derniers mois, le plus ancien d'abord, le mois de `ref` en dernier."""
    return [decaler_mois(ref.year, ref.month, -i) for i in range(nb - 1, -1, -1)]


def etiquette(annee, mois, avec_annee=False):
    return f"{MOIS_COURTS[mois - 1]} {str(annee)[2:]}" if avec_annee else MOIS_COURTS[mois - 1]


def etiquettes(fenetre):
    """Le mois seul, avec l'année quand elle change (et sur le premier point)."""
    sorties = []
    for i, (a, m) in enumerate(fenetre):
        sorties.append(etiquette(a, m, avec_annee=(i == 0 or m == 1)))
    return sorties


def borne_debut(ref, nb):
    a, m = decaler_mois(ref.year, ref.month, -(nb - 1))
    return datetime(a, m, 1)


def borne_fin(ref):
    a, m = decaler_mois(ref.year, ref.month, 1)
    return datetime(a, m, 1)


# --------------------------------------------------------------------------
# Requêtes de base
# --------------------------------------------------------------------------
def _avec_ile(q, ile):
    """Restreint une requête qui contient déjà Dossier à une île."""
    if ile:
        q = q.join(Parcelle, Parcelle.id == Dossier.parcelle_id).filter(Parcelle.ile_id == ile)
    return q


def _dates(q, colonne):
    return [x for (x,) in q.with_entities(colonne).all() if x]


def _par_mois(dates, fenetre):
    compte = {cle: 0 for cle in fenetre}
    for d in dates:
        cle = (d.year, d.month)
        if cle in compte:
            compte[cle] += 1
    return [compte[cle] for cle in fenetre]


def _mutations_validees(ile):
    q = Mutation.query.join(Dossier, Dossier.id == Mutation.dossier_id).filter(
        Mutation.statut == C.MS_VALIDEE,
        # une fusion crée une ligne « système » par parcelle source, un transfert reporte les hypothèques sur
        # le nouveau dossier : ce ne sont pas de nouvelles opérations
        Mutation.details.is_(None) | (~Mutation.details.like('%"role":"source"%') & ~Mutation.details.like('%"reportee_de"%')),
    )
    return _avec_ile(q, ile)


def _certifications(ile):
    q = EtapeLog.query.join(Dossier, Dossier.id == EtapeLog.dossier_id).filter(
        EtapeLog.vers_statut == C.CERTIFIE, EtapeLog.type == "avance"
    )
    return _avec_ile(q, ile)


# --------------------------------------------------------------------------
# Indicateurs
# --------------------------------------------------------------------------
def activite_mensuelle(ile, nb_mois, ref):
    fenetre = fenetre_mois(ref, nb_mois)
    debut, fin = borne_debut(ref, nb_mois), borne_fin(ref)
    successeurs = db.session.query(Filiation.resultat_id).filter(Filiation.type.in_(C.TRANSFERTS))   # créés par une mutation, pas « ouverts »
    crees = _avec_ile(Dossier.query.filter(Dossier.origine == C.ORIGINE_NOUVEAU, Dossier.created_at >= debut, Dossier.created_at < fin, ~Dossier.id.in_(successeurs)), ile)
    certifies = _certifications(ile).filter(EtapeLog.created_at >= debut, EtapeLog.created_at < fin)
    mutations = _mutations_validees(ile).filter(Mutation.decision_at >= debut, Mutation.decision_at < fin)
    return {
        "mois": etiquettes(fenetre),
        "series": [
            {"nom": "Dossiers créés", "valeurs": _par_mois(_dates(crees, Dossier.created_at), fenetre)},
            {"nom": "Certificats émis", "valeurs": _par_mois(_dates(certifies, EtapeLog.created_at), fenetre)},
            {"nom": "Mutations validées", "valeurs": _par_mois(_dates(mutations, Mutation.decision_at), fenetre)},
        ],
    }


def mutations_en_attente(ile):
    q = Mutation.query.join(Dossier, Dossier.id == Mutation.dossier_id).filter(Mutation.statut == C.MS_ATTENTE)
    return _avec_ile(q, ile).count()


def pipeline(ile):
    """Dossiers en cours, par étape du circuit (état actuel)."""
    q = _avec_ile(Dossier.query.filter(Dossier.statut.in_(ETAPES)), ile)
    lignes = dict(q.with_entities(Dossier.statut, func.count(Dossier.id)).group_by(Dossier.statut).all())
    return [{"nom": NOMS_COURTS[e], "valeur": lignes.get(e, 0), "statut": e} for e in ETAPES]


def par_ile():
    """Parcelles existantes (non clôturées) par île."""
    from .models import Ile

    lignes = dict(
        db.session.query(Parcelle.ile_id, func.count(Dossier.id))
        .join(Dossier, Dossier.parcelle_id == Parcelle.id)
        .filter(Dossier.statut != C.CLOTURE)
        .group_by(Parcelle.ile_id)
        .all()
    )
    return [{"nom": i.nom, "valeur": lignes.get(i.id, 0), "ile": i.id} for i in Ile.query.order_by(Ile.id)]


def mutations_par_type(ile, nb_mois, ref):
    debut, fin = borne_debut(ref, nb_mois), borne_fin(ref)
    q = _mutations_validees(ile).filter(Mutation.decision_at >= debut, Mutation.decision_at < fin)
    compte = dict(q.with_entities(Mutation.type, func.count(Mutation.id)).group_by(Mutation.type).all())
    lignes = [{"nom": C.MUTATIONS[t], "valeur": n, "type": t} for t, n in compte.items()]
    return sorted(lignes, key=lambda x: (-x["valeur"], x["nom"]))


def _parcours_certifies(ile, debut, fin):
    """{dossier_id: (créé le, [logs triés])} des dossiers certifiés dans [debut, fin)."""
    certs = _certifications(ile).filter(EtapeLog.created_at >= debut, EtapeLog.created_at < fin)
    ids = {dossier_id for (dossier_id,) in certs.with_entities(EtapeLog.dossier_id).all()}
    if not ids:
        return {}
    crees = dict(db.session.query(Dossier.id, Dossier.created_at).filter(Dossier.id.in_(ids)).all())
    logs = EtapeLog.query.filter(EtapeLog.dossier_id.in_(ids), EtapeLog.type != "correction").order_by(EtapeLog.dossier_id, EtapeLog.id).all()
    parcours = {i: (crees[i], []) for i in ids}
    for log in logs:
        parcours[log.dossier_id][1].append(log)
    return parcours


def delais(ile, debut, fin):
    """Délai de traitement (jours) des dossiers certifiés dans la fenêtre : création -> certificat."""
    jours = []
    for cree, logs in _parcours_certifies(ile, debut, fin).values():
        fin_cert = next((l.created_at for l in reversed(logs) if l.vers_statut == C.CERTIFIE), None)
        if fin_cert and cree:
            jours.append(max(0.0, (fin_cert - cree).total_seconds() / 86400))
    return jours


def delais_par_etape(ile, debut, fin):
    """Temps moyen passé à chaque étape (jours), sur les dossiers certifiés dans la fenêtre.

    Le temps à une étape va de l'entrée dans l'étape à l'entrée dans la suivante ; un retour en
    arrière (rejet) s'ajoute au temps de l'étape revisitée."""
    cumul = {e: [] for e in ETAPES}
    for cree, logs in _parcours_certifies(ile, debut, fin).values():
        par_etape = {}
        for courant, suivant in zip(logs, logs[1:]):
            if courant.vers_statut in cumul:
                par_etape[courant.vers_statut] = par_etape.get(courant.vers_statut, 0.0) + (suivant.created_at - courant.created_at).total_seconds() / 86400
        for etape, jours in par_etape.items():
            cumul[etape].append(jours)
    return [
        {"nom": NOMS_COURTS[e], "valeur": round(sum(v) / len(v), 1) if v else 0.0, "n": len(v), "statut": e}
        for e, v in cumul.items()
        if e != C.ATTENTE_AVIS or v  # l'attente d'avis n'existe que pour la filière privée
    ]


def en_retard(ile, ref, seuil=SEUIL_RETARD_JOURS, limite=8):
    """Dossiers en cours sans avancement depuis plus de `seuil` jours, les plus anciens d'abord."""
    dernier = (
        db.session.query(EtapeLog.dossier_id.label("dossier_id"), func.max(EtapeLog.created_at).label("dernier"))
        .filter(EtapeLog.type != "correction")
        .group_by(EtapeLog.dossier_id)
        .subquery()
    )
    q = (
        db.session.query(Dossier, dernier.c.dernier)
        .join(dernier, dernier.c.dossier_id == Dossier.id)
        .filter(Dossier.statut.in_(ETAPES), dernier.c.dernier < ref - timedelta(days=seuil))
    )
    q = _avec_ile(q, ile)
    lignes = q.order_by(dernier.c.dernier).all()
    return {
        "total": len(lignes),
        "lignes": [
            {"dossier": d, "etape": C.STATUTS[d.statut], "responsable": RESPONSABLE[d.statut], "jours": (ref - dt).days}
            for d, dt in lignes[:limite]
        ],
    }


# --------------------------------------------------------------------------
# Tuiles
# --------------------------------------------------------------------------
def compact(n):
    """1 284 -> « 1 284 », 12 900 -> « 12,9 k »."""
    if n >= 10000:
        return f"{n / 1000:.1f}".replace(".", ",").replace(",0", "") + " k"
    return f"{n:,}".replace(",", " ")


def _delta(courant, precedent, nom_periode, haut_est_bon=True, unite=""):
    """Variation signée vs une période nommée. `bon` : True/False/None selon le sens et ce qui est souhaitable."""
    diff = courant - precedent
    if round(diff, 1) == 0:
        return {"texte": f"stable vs {nom_periode}", "sens": "stable", "bon": None}
    sens = "hausse" if diff > 0 else "baisse"
    if precedent:
        texte = f"{diff / precedent * 100:+.0f} %".replace("-", "−")
    else:
        texte = f"{diff:+g}{unite}".replace("-", "−")
    return {"texte": f"{texte} vs {nom_periode}", "sens": sens, "bon": (diff > 0) == haut_est_bon}


def tuiles(ile, nb_mois, ref):
    sparkline = activite_mensuelle(ile, 12, ref)  # 12 mois fixes pour les mini-courbes, quel que soit le filtre
    certs, muts = sparkline["series"][1]["valeurs"], sparkline["series"][2]["valeurs"]
    en_cours = sum(l["valeur"] for l in pipeline(ile))
    prive = _avec_ile(Dossier.query.filter(Dossier.statut.in_(ETAPES), Dossier.filiere == C.FILIERE_PRIVE), ile).count()

    fin = borne_fin(ref)
    debut = borne_debut(ref, nb_mois)
    debut_prec = borne_debut(decaler_mois_date(ref, -nb_mois), nb_mois)
    actuels, precedents = delais(ile, debut, fin), delais(ile, debut_prec, debut)
    moy = lambda v: round(sum(v) / len(v), 1) if v else None  # noqa: E731

    tuile_delai = {"libelle": f"Délai moyen de traitement ({nb_mois} mois)", "valeur": "—", "brut": None, "precision": f"{len(actuels)} dossier(s) certifié(s)", "delta": None, "trace": None}
    if moy(actuels) is not None:
        tuile_delai.update(valeur=f"{moy(actuels):.1f}".replace(".", ",") + " j", brut=moy(actuels))
        if moy(precedents) is not None:
            tuile_delai["delta"] = _delta(moy(actuels), moy(precedents), f"les {nb_mois} mois précédents", haut_est_bon=False, unite=" j")
    return [
        {"libelle": "Dossiers en cours", "valeur": compact(en_cours), "brut": en_cours, "precision": f"dont {prive} en filière privée" if prive else "dans le circuit de traitement", "delta": None, "trace": None},
        {"libelle": "Certificats émis ce mois", "valeur": compact(certs[-1]), "brut": certs[-1], "precision": None, "delta": _delta(certs[-1], certs[-2], "le mois précédent"), "trace": certs},
        {"libelle": "Mutations validées ce mois", "valeur": compact(muts[-1]), "brut": muts[-1], "precision": None, "delta": _delta(muts[-1], muts[-2], "le mois précédent"), "trace": muts},
        tuile_delai,
    ]


def decaler_mois_date(ref, delta):
    a, m = decaler_mois(ref.year, ref.month, delta)
    return datetime(a, m, 1)


# --------------------------------------------------------------------------
# Fil d'activité
# --------------------------------------------------------------------------
def _verbe(log):
    if log.type == "rejet":
        return f"a rejeté le dossier ({log.commentaire})" if log.commentaire else "a rejeté le dossier"
    if log.de_statut is None:
        return "a numérisé un ancien dossier" if log.vers_statut == C.ARCHIVE else "a ouvert le dossier"
    if log.vers_statut == C.CLOTURE:
        return "a clôturé le dossier"
    return VERBES.get((log.de_statut, log.vers_statut), "a fait avancer le dossier")


def activite_recente(ile, limite=8):
    logs = _avec_ile(EtapeLog.query.join(Dossier, Dossier.id == EtapeLog.dossier_id).filter(EtapeLog.type != "correction"), ile)
    evenements = []
    for l in logs.order_by(EtapeLog.id.desc()).limit(limite).all():
        evenements.append({"quand": l.created_at, "qui": l.user.nom, "texte": _verbe(l), "dossier": l.dossier})
    for m in _mutations_validees(ile).order_by(Mutation.decision_at.desc()).limit(limite).all():
        evenements.append({"quand": m.decision_at, "qui": m.decision_par.nom, "texte": f"a validé : {C.MUTATIONS[m.type].lower()}", "dossier": m.dossier})
    evenements.sort(key=lambda e: e["quand"], reverse=True)
    return evenements[:limite]
