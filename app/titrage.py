"""Titrage : les étapes qui suivent la certification.

Journal officiel (Fonction publique) → Plan de bornage (Domaine) → Journal de clôture (Fonction publique) → Titré (Domaine).
Le statut du circuit reste « Certificat émis » (les mutations restent possibles) : l'avancement du titrage est porté par
`Dossier.etape_titre` (l'étape À FAIRE). Chaque étape enregistre sa preuve (référence, date, scans) et reste dans l'historique ;
l'acteur d'une étape peut renvoyer le dossier à l'étape précédente, avec un motif. Seuls les dossiers certifiés après la mise en
place du titrage y entrent.
"""

from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import documents as Doc
from . import geo
from . import workflow as W
from .audit import audit
from .extensions import db
from sqlalchemy import func

from .models import Blocage, Dossier, EtapeTitrage, Parcelle, Proprietaire, maintenant
from .storage import StorageError

bp = Blueprint("titrage", __name__)


class TitrageError(Exception):
    """Saisie refusée (message affichable)."""


# --------------------------------------------------------------------------
# État et droits
# --------------------------------------------------------------------------
def en_titrage(d):
    return d.origine == C.ORIGINE_NOUVEAU and d.statut == C.CERTIFIE and d.etape_titre is not None


def etape_a_faire(d):
    """Étape à accomplir (une de C.ETAPES_TITRAGE) ou None (pas de titrage, ou titrage terminé)."""
    return d.etape_titre if en_titrage(d) and d.etape_titre in C.ETAPES_TITRAGE else None


def termine(d):
    return en_titrage(d) and d.etape_titre == C.TIT_FIN


def peut_agir(d, user, etape=None):
    """L'utilisateur peut-il accomplir l'étape À FAIRE ?"""
    etape = etape or etape_a_faire(d)
    if etape is None or d.est_bloque or not W.peut_voir(d, user):
        return False
    return user.role in (C.ADMIN, C.TITRAGE_ACTEUR[etape])


def peut_retourner(d, user):
    """Renvoyer le dossier à l'étape précédente : par l'acteur de l'étape à faire (ou l'administrateur), sauf à la première étape."""
    etape = etape_a_faire(d)
    return etape is not None and etape != C.ETAPES_TITRAGE[0] and peut_agir(d, user, etape)


def entrer_en_titrage(d, user):
    """Appelé à la certification d'un nouveau dossier : la première étape (Journal officiel) devient à faire."""
    if d.origine == C.ORIGINE_NOUVEAU and d.etape_titre is None:
        d.etape_titre = C.ETAPES_TITRAGE[0]
        audit("titrage_ouvert", "dossier", d.id, nouveau=C.TITRAGE_LIBELLES[d.etape_titre], user=user)


def libelle(d):
    """Libellé court de l'avancement du titrage (None si le dossier n'y est pas)."""
    if not en_titrage(d):
        return None
    return "Titré" if d.etape_titre == C.TIT_FIN else f"À faire : {C.TITRAGE_LIBELLES[d.etape_titre]}"


def historique(d):
    return list(d.etapes_titrage)


def etapes_suivi(d):
    """[(étape, libellé, état, dernière preuve)] pour la barre d'étapes du titrage."""
    a_faire = d.etape_titre
    idx = len(C.ETAPES_TITRAGE) if a_faire == C.TIT_FIN else (C.ETAPES_TITRAGE.index(a_faire) if a_faire in C.ETAPES_TITRAGE else -1)
    faites = {}
    for e in d.etapes_titrage:
        if e.type == "fait" and e.annule_at is None:
            faites[e.etape] = e
    sortie = []
    for i, etape in enumerate(C.ETAPES_TITRAGE):
        sortie.append((etape, C.TITRAGE_LIBELLES[etape], "done" if i < idx else "current" if i == idx else "todo", faites.get(etape)))
    return sortie


def contexte(d, user):
    """Données de la carte « Titrage » (étapes, formulaires, historique) ; None si le dossier n'est pas en titrage."""
    if not en_titrage(d):
        return None
    etape = etape_a_faire(d)
    return {
        "suivi": etapes_suivi(d),
        "etape": etape,
        "termine": termine(d),
        "peut_agir": peut_agir(d, user),
        "peut_retourner": peut_retourner(d, user),
        "champs": C.TITRAGE_CHAMPS.get(etape),
        "acteur": C.ROLES[C.TITRAGE_ACTEUR[etape]] if etape else None,
        "historique": historique(d),
        "libelles": C.TITRAGE_LIBELLES,
    }


def _derniere_date_active(d, avant_etape):
    """Date de l'étape active qui précède `avant_etape`."""
    i = C.ETAPES_TITRAGE.index(avant_etape)
    for etape in reversed(C.ETAPES_TITRAGE[:i]):
        for e in reversed(d.etapes_titrage):
            if e.etape == etape and e.type == "fait" and e.annule_at is None and e.date_etape:
                return etape, e.date_etape
    return None, None


# --------------------------------------------------------------------------
# Lecture et validation
# --------------------------------------------------------------------------
def lire(d, etape, form):
    """(référence, date, observations) validés."""
    libelle_ref, libelle_date, _ = C.TITRAGE_CHAMPS[etape]
    reference = " ".join((form.get("reference") or "").split())
    if not reference:
        raise TitrageError(f"« {libelle_ref} » est obligatoire.")
    if len(reference) > 120:
        raise TitrageError(f"« {libelle_ref} » est trop long (120 caractères maximum).")
    try:
        jour = date.fromisoformat((form.get("date_etape") or "").strip())
    except ValueError:
        raise TitrageError(f"« {libelle_date} » est obligatoire et doit être une date valide.") from None
    if jour > date.today():
        raise TitrageError(f"« {libelle_date} » ne peut pas être dans le futur.")
    precedente, date_precedente = _derniere_date_active(d, etape)
    if date_precedente and jour < date_precedente:
        raise TitrageError(f"« {libelle_date} » ne peut pas précéder celle de l'étape « {C.TITRAGE_LIBELLES[precedente]} » ({date_precedente:%d/%m/%Y}).")
    delai = current_app.config.get("TITRAGE_DELAI_BORNAGE_JOURS", 0)
    if etape == C.TIT_BORNAGE and delai and date_precedente and jour < date_precedente + timedelta(days=delai):
        raise TitrageError(f"Le bornage ne peut pas avoir lieu moins de {delai} jours après la parution au Journal officiel ({date_precedente:%d/%m/%Y}).")
    observations = (form.get("observations") or "").strip() or None
    if observations and len(observations) > 1000:
        raise TitrageError("Les observations sont limitées à 1000 caractères.")
    if etape == C.TIT_TITRAGE:
        existant = EtapeTitrage.query.filter(EtapeTitrage.etape == C.TIT_TITRAGE, EtapeTitrage.type == "fait", EtapeTitrage.annule_at.is_(None),
                                              EtapeTitrage.reference == reference, EtapeTitrage.dossier_id != d.id).first()
        if existant:
            raise TitrageError("Ce numéro de titre foncier est déjà attribué à un autre dossier.")
    return reference, jour, observations


def _fichiers(etape, files):
    """Scans joints : un champ de fichier par document attendu (tous facultatifs)."""
    attendus = C.TITRAGE_CHAMPS[etape][2]
    lignes, types = [], []
    for i, type_doc in enumerate(attendus):
        f = files.get(f"fichier_{i}")
        if f and f.filename:
            lignes.append(f)
            types.append(type_doc)
    if not lignes:
        return []
    return Doc.preparer(lignes, types, [""] * len(types))


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def avancer(d, user, form, files, cles_ecrites):
    """Accomplit l'étape à faire. Retourne l'étape accomplie (sans commit)."""
    etape = etape_a_faire(d)
    if etape is None or not peut_agir(d, user, etape):
        raise TitrageError("Cette étape n'est pas à accomplir par vous pour ce dossier.")
    reference, jour, observations = lire(d, etape, form)
    fichiers = _fichiers(etape, files)
    if fichiers:
        Doc.enregistrer(d, fichiers, None, user, cles_ecrites)
    if etape == C.TIT_BORNAGE:                       # tracé de bornage (KMZ/KML), facultatif à ce moment : il peut aussi être déposé ensuite
        fichier_bornage = files.get("fichier_bornage")
        if fichier_bornage and fichier_bornage.filename:
            from . import bornage as Bo

            Bo.deposer(d, user, fichier_bornage, form.get("motif_bornage"), cles_ecrites)
    db.session.add(EtapeTitrage(dossier=d, etape=etape, type="fait", reference=reference, date_etape=jour, observations=observations, user_id=user.id))
    suivante = C.ETAPES_TITRAGE.index(etape) + 1
    d.etape_titre = C.ETAPES_TITRAGE[suivante] if suivante < len(C.ETAPES_TITRAGE) else C.TIT_FIN
    audit("titrage_etape", "dossier", d.id, nouveau=f"{C.TITRAGE_LIBELLES[etape]} : {reference} ({jour:%d/%m/%Y})", user=user)
    return etape


def retourner(d, user, motif):
    """Renvoie le dossier à l'étape précédente (l'étape précédente est annulée et devra être refaite)."""
    if not peut_retourner(d, user):
        raise TitrageError("Vous ne pouvez pas renvoyer ce dossier à l'étape précédente.")
    motif = (motif or "").strip()
    if not motif:
        raise TitrageError("Le motif du retour est obligatoire.")
    if len(motif) > 1000:
        raise TitrageError("Le motif est limité à 1000 caractères.")
    courante = etape_a_faire(d)
    precedente = C.ETAPES_TITRAGE[C.ETAPES_TITRAGE.index(courante) - 1]
    for e in d.etapes_titrage:
        if e.etape == precedente and e.type == "fait" and e.annule_at is None:
            e.annule_at = maintenant()
    db.session.add(EtapeTitrage(dossier=d, etape=courante, type="retour", observations=motif, user_id=user.id))
    d.etape_titre = precedente
    audit("titrage_retour", "dossier", d.id, ancien=C.TITRAGE_LIBELLES[courante], nouveau=f"{C.TITRAGE_LIBELLES[precedente]} : {motif}", user=user)
    return precedente


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    return d


@bp.route("/dossiers/<int:dossier_id>/titrage/avancer", methods=["POST"])
@login_required
def route_avancer(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_agir(d, current_user):
        abort(403)
    cles = []
    try:
        etape = avancer(d, current_user, request.form, request.files, cles)
        db.session.commit()
        flash(f"Étape « {C.TITRAGE_LIBELLES[etape]} » enregistrée." + (" Le titrage est terminé : le dossier est titré." if d.etape_titre == C.TIT_FIN else ""), "success")
    except (TitrageError, Doc.DocumentError, StorageError, geo.GeoError) as e:
        db.session.rollback()
        Doc.nettoyer(cles)
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        Doc.nettoyer(cles)
        raise
    return redirect(url_for("titrage.fiche", dossier_id=d.id))


@bp.route("/dossiers/<int:dossier_id>/titrage/retour", methods=["POST"])
@login_required
def route_retour(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_retourner(d, current_user):
        abort(403)
    try:
        precedente = retourner(d, current_user, request.form.get("motif"))
        db.session.commit()
        flash(f"Dossier renvoyé à l'étape « {C.TITRAGE_LIBELLES[precedente]} ».", "success")
    except TitrageError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("titrage.fiche", dossier_id=d.id))


# --------------------------------------------------------------------------
# Page « Titrage » : suivi des quatre étapes
# --------------------------------------------------------------------------
def peut_consulter(user):
    """Accès à la page « Titrage » : administrateur, Directeur, Domaine et Fonction publique. Les topographes (public et privé) n'y ont pas accès."""
    return user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE, C.FONCTION_PUBLIQUE)


def _de_titrage(user):
    """Dossiers que ce compte voit et qui sont entrés en titrage."""
    return W.dossiers_visibles(user).filter(Dossier.origine == C.ORIGINE_NOUVEAU, Dossier.statut == C.CERTIFIE, Dossier.etape_titre.isnot(None))


def _non_bloques(q):
    return q.filter(~Dossier.blocages.any(Blocage.leve_at.is_(None)))


def nb_a_traiter(user):
    """Dossiers en titrage dont l'étape à faire revient à ce compte (hors dossiers bloqués) ; 0 pour les autres comptes."""
    etapes = [e for e in C.ETAPES_TITRAGE if C.TITRAGE_ACTEUR[e] == user.role]
    if not etapes or not peut_consulter(user):
        return 0
    return _non_bloques(_de_titrage(user).filter(Dossier.etape_titre.in_(etapes))).count()


def _depuis(d):
    """Date d'entrée du dossier dans son étape actuelle : dernière étape active accomplie, sinon la validation du Directeur."""
    faites = [e for e in d.etapes_titrage if e.type == "fait" and e.annule_at is None]
    retours = [e for e in d.etapes_titrage if e.type == "retour"]
    dates = [e.created_at for e in faites + retours] + [d.valide_at]
    return max(x for x in dates if x)


def _references(d):
    return [(C.TITRAGE_LIBELLES[e.etape], e.reference, e.date_etape) for e in d.etapes_titrage if e.type == "fait" and e.annule_at is None]


@bp.route("/titrage")
@login_required
def page():
    if not peut_consulter(current_user):
        abort(404)
    ile_id = request.args.get("ile", type=int)
    recherche = (request.args.get("q") or "").strip()
    q = _de_titrage(current_user).join(Proprietaire, Proprietaire.id == Dossier.proprietaire_id).join(Parcelle, Parcelle.id == Dossier.parcelle_id)
    if ile_id:
        q = q.filter(Parcelle.ile_id == ile_id)
    if recherche:
        q = W.appliquer_recherche(q, recherche)
    comptes = dict(q.with_entities(Dossier.etape_titre, func.count(Dossier.id)).group_by(Dossier.etape_titre).all())
    mes_etapes = {e for e in C.ETAPES_TITRAGE if C.TITRAGE_ACTEUR[e] == current_user.role}
    a_traiter = {e: _non_bloques(q.filter(Dossier.etape_titre == e)).count() for e in mes_etapes}

    etape = request.args.get("etape", "")
    if etape not in C.ONGLETS_TITRAGE:                       # sans onglet choisi : le premier où il y a du travail pour ce compte, sinon le premier qui contient des dossiers
        etape = next((e for e in C.ONGLETS_TITRAGE if a_traiter.get(e)), None) or next((e for e in C.ONGLETS_TITRAGE if comptes.get(e)), C.TIT_JO)
    liste = q.filter(Dossier.etape_titre == etape).order_by(Dossier.valide_at, Dossier.id).all()   # les plus anciens d'abord
    ids_bloques = {i for (i,) in db.session.query(Blocage.dossier_id).filter(Blocage.dossier_id.in_([d.id for d in liste] or [0]), Blocage.leve_at.is_(None))}
    maintenant_ = maintenant()
    lignes = [{"d": d, "jours": (maintenant_ - _depuis(d)).days, "references": _references(d), "bloque": d.id in ids_bloques,
               "a_moi": etape in mes_etapes and d.id not in ids_bloques} for d in liste]
    page_n = request.args.get("page", 1, type=int)
    par_page = 20
    return render_template(
        "titrage/page.html",
        onglets=[(e, libelle, comptes.get(e, 0), a_traiter.get(e, 0)) for e, libelle in C.ONGLETS_TITRAGE.items()],
        etape=etape,
        libelle_etape=C.ONGLETS_TITRAGE[etape],
        lignes=lignes[(page_n - 1) * par_page: page_n * par_page],
        total=len(lignes),
        page_n=page_n,
        pages=max(1, -(-len(lignes) // par_page)),
        acteur=C.ROLES[C.TITRAGE_ACTEUR[etape]] if etape in C.TITRAGE_ACTEUR else None,
        filtres={"q": recherche, "ile": ile_id},
        iles=W.iles_visibles(current_user),
        total_a_traiter=sum(a_traiter.values()),
    )


@bp.route("/dossiers/<int:dossier_id>/titrage/bloquer", methods=["POST"])
@login_required
def route_bloquer(dossier_id):
    """Signale un problème (bloque le dossier) depuis les étapes du titrage. Impossible une fois le dossier titré."""
    from . import blocages as Bl

    d = _dossier_ou_404(dossier_id)
    if not peut_consulter(current_user) or not en_titrage(d):
        abort(404)
    try:
        Bl.signaler(current_user, d, request.form.get("type"), request.form.get("description"))
        db.session.commit()
        flash("Dossier bloqué. Il reste en lecture seule jusqu'à ce que le Directeur lève le blocage.", "warning")
    except Bl.BlocageError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("titrage.fiche", dossier_id=d.id))


@bp.route("/titrage/<int:dossier_id>")
@login_required
def fiche(dossier_id):
    """Suivi d'UN dossier en titrage : étapes, saisie, historique, scans. Page indépendante de la fiche complète du dossier."""
    if not peut_consulter(current_user):
        abort(404)
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user) or not en_titrage(d):
        abort(404)
    from . import blocages as Bl
    from .models import DocumentEmis
    from .pdf import TITRES

    types_titrage = {t for _, _, types in C.TITRAGE_CHAMPS.values() for t in types}
    scans = [x for x in d.documents if x.retire_at is None and x.type in types_titrage]
    emis = DocumentEmis.query.filter(DocumentEmis.dossier_id == d.id, DocumentEmis.pdf_cle.isnot(None), DocumentEmis.revoque_at.is_(None)).order_by(DocumentEmis.id.desc()).all()
    from . import bornage as Bo

    b = Bo.courant(d)
    ecart, recoupe = Bo.ecart_avec_plan_croquis(d, b) if b else (None, None)
    return render_template(
        "titrage/fiche.html",
        d=d,
        bornage={
            "visible": d.etape_titre in (*Bo.ETAPES_DEPOT, C.TIT_FIN),
            "courant": b,
            "versions": list(reversed(d.bornages)),
            "peut_deposer": Bo.peut_deposer(d, current_user),
            "peut_imprimer": "bornage" in W.documents_imprimables(d, current_user),
            "etape_faite": Bo.etape_faite(d),
            "nb_bornes": Bo._nb_bornes(b) if b else 0,
            "ecart": ecart,
            "recoupe": recoupe,
        },
        titrage=contexte(d, current_user),
        scans=scans,
        emis=emis,
        titres_pdf=TITRES,
        proprietaires=d.proprietaires_actuels,
        peut_fiche=current_user.role != C.FONCTION_PUBLIQUE,
        bloque=d.est_bloque,
        blocages_actifs=d.blocages_actifs,
        peut_bloquer=Bl.peut_signaler(current_user, d),
        peut_lever=Bl.peut_lever(current_user),
        groupes_problemes=C.PROBLEMES_GROUPES,
    )
