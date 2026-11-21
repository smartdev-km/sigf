"""Blocage d'un dossier « jusqu'à preuve du contraire ».

Tout agent qui a accès à un dossier peut signaler un problème. Tant qu'un blocage est actif, le dossier est figé :
lecture seule (ni avancement du circuit, ni mutation, ni correction, ni impression, ni dépôt de document ou de tracé).
Seul le Directeur lève un blocage, en indiquant la preuve (décision, jugement, pièce fournie…) et, s'il en a une,
en joignant le fichier. Un dossier peut cumuler plusieurs blocages : il reste figé tant qu'il en reste un.
"""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import documents as Doc
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Blocage, Dossier, maintenant
from .storage import StorageError

bp = Blueprint("blocages", __name__)

MAX_TEXTE = 2000


class BlocageError(Exception):
    """Refus affichable à l'utilisateur."""


# --------------------------------------------------------------------------
# Droits et règles
# --------------------------------------------------------------------------
def peut_signaler(user, d):
    """Tout agent qui voit le dossier (le topographe privé : seulement les siens). Un dossier clôturé n'est plus signalable :
    on signale le dossier à jour qui l'a remplacé. Un dossier TITRÉ (titrage terminé) ne peut plus être bloqué."""
    return d.statut != C.CLOTURE and d.etape_titre != C.TIT_FIN and W.peut_voir(d, user)


def peut_lever(user):
    return user.role == C.DIRECTEUR


def ids_bloques(ids):
    """Parmi ces dossiers, ceux qui ont au moins un blocage actif (une requête, pour les listes)."""
    ids = list(ids)
    if not ids:
        return set()
    return {i for (i,) in db.session.query(Blocage.dossier_id).filter(Blocage.dossier_id.in_(ids), Blocage.leve_at.is_(None))}


def _blocages_visibles(user):
    return W.restreindre_ile(Blocage.query.join(Dossier, Dossier.id == Blocage.dossier_id), user)


def nb_actifs(user):
    return _blocages_visibles(user).filter(Blocage.leve_at.is_(None)).count()


def _texte(brut, nom, obligatoire=True, mini=0):
    valeur = " ".join((brut or "").split())
    if obligatoire and not valeur:
        raise BlocageError(f"{nom} est obligatoire.")
    if valeur and len(valeur) < mini:
        raise BlocageError(f"{nom} est trop court : donnez quelques précisions (au moins {mini} caractères).")
    if len(valeur) > MAX_TEXTE:
        raise BlocageError(f"{nom} est trop long (maximum {MAX_TEXTE} caractères).")
    return valeur


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def signaler(user, d, type_, description):
    if not peut_signaler(user, d):
        if d.etape_titre == C.TIT_FIN:
            raise BlocageError("Ce dossier est titré : le titre foncier est délivré, le dossier ne peut plus être bloqué.")
        raise BlocageError("Vous ne pouvez pas signaler de problème sur ce dossier (dossier clôturé : signalez le dossier à jour).")
    if type_ not in C.PROBLEMES:
        raise BlocageError("Choisissez le type de problème.")
    description = _texte(description, "La description du problème", mini=10)
    b = Blocage(dossier_id=d.id, type=type_, description=description, signale_par_id=user.id)
    db.session.add(b)
    db.session.flush()
    db.session.refresh(d)
    audit("blocage_signale", "dossier", d.id, nouveau=f"{C.PROBLEMES[type_]} : {description}", user=user)
    return b


def lever(user, b, motif, fichier, cles):
    """Lève un blocage. `fichier` : fichier de preuve facultatif (FileStorage) ; `cles` : clés écrites dans le stockage."""
    if not peut_lever(user):
        raise BlocageError("Seul le Directeur peut lever un blocage.")
    if not b.actif:
        raise BlocageError("Ce blocage est déjà levé.")
    motif = _texte(motif, "La preuve (décision, jugement, pièce fournie…)", mini=10)
    if fichier is not None and fichier.filename:
        try:
            prets = Doc.preparer([fichier], [C.TYPE_AUTRE], [f"Preuve de levée du blocage n°{b.id}"])
            doc = Doc.enregistrer(b.dossier, prets, None, user, cles)[0]
        except Doc.DocumentError as e:
            raise BlocageError(str(e)) from None
        b.preuve_document_id = doc.id
    b.leve_par_id = user.id
    b.leve_at = maintenant().replace(microsecond=0)
    b.motif_levee = motif
    db.session.flush()
    db.session.refresh(b.dossier)
    audit("blocage_leve", "dossier", b.dossier_id, ancien=f"{b.type_label} : {b.description}", nouveau=f"Preuve : {motif}", user=user)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def _hors_topo_prive():
    if W.est_restreint(current_user):
        abort(404)


@bp.route("/blocages")
@login_required
def liste():
    _hors_topo_prive()
    onglet = "leves" if request.args.get("onglet") == "leves" else "actifs"
    q = _blocages_visibles(current_user)
    q = q.filter(Blocage.leve_at.isnot(None) if onglet == "leves" else Blocage.leve_at.is_(None))
    type_ = request.args.get("type", "")
    recherche = (request.args.get("q") or "").strip()
    if type_ in C.PROBLEMES:
        q = q.filter(Blocage.type == type_)
    if recherche:
        motif = f"%{recherche}%"
        q = q.filter(db.or_(Dossier.numero.ilike(motif), Blocage.description.ilike(motif)))
    ordre = Blocage.leve_at.desc() if onglet == "leves" else Blocage.signale_at.desc()
    page = request.args.get("page", 1, type=int)
    pagination = q.order_by(ordre, Blocage.id.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template(
        "blocages/liste.html",
        pagination=pagination,
        onglet=onglet,
        nb_actifs=nb_actifs(current_user),
        nb_leves=_blocages_visibles(current_user).filter(Blocage.leve_at.isnot(None)).count(),
        filtres={"type": type_, "q": recherche},
        peut_lever=peut_lever(current_user),
    )


@bp.route("/blocages/nouveau", methods=["GET", "POST"])
@login_required
def nouveau():
    f = request.form if request.method == "POST" else request.args
    numero = (f.get("numero") or "").strip()
    d = None
    if f.get("dossier", type=int):
        d = W.dossiers_visibles(current_user).filter(Dossier.id == f.get("dossier", type=int)).first()
        if d is not None:
            numero = d.numero
    elif numero:
        d = W.dossiers_visibles(current_user).filter(Dossier.numero == numero).first()
    if request.method == "POST":
        try:
            if d is None:
                raise BlocageError("Dossier introuvable : vérifiez le numéro (ex. SIGF-2026-00012).")
            signaler(current_user, d, f.get("type"), f.get("description"))
            db.session.commit()
            flash(f"Dossier {d.numero} bloqué. Il reste en lecture seule jusqu'à ce que le Directeur lève le blocage.", "warning")
            return redirect(url_for("main.detail_dossier", dossier_id=d.id))
        except BlocageError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return render_template("blocages/nouveau.html", groupes=C.PROBLEMES_GROUPES, f=f, d=d, numero=numero), 400
    return render_template("blocages/nouveau.html", groupes=C.PROBLEMES_GROUPES, f=f, d=d, numero=numero)


@bp.route("/blocages/<int:blocage_id>/lever", methods=["POST"])
@login_required
def lever_route(blocage_id):
    b = db.get_or_404(Blocage, blocage_id)
    if not W.peut_voir(b.dossier, current_user):
        abort(404)
    if not peut_lever(current_user):
        abort(403)
    cles = []
    try:
        lever(current_user, b, request.form.get("motif"), request.files.get("fichier"), cles)
        db.session.commit()
        reste = b.dossier.est_bloque
        flash("Blocage levé." + (" Le dossier reste bloqué : un autre problème est encore actif." if reste else " Le dossier est de nouveau utilisable."), "success")
    except (BlocageError, StorageError) as e:
        db.session.rollback()
        Doc.nettoyer(cles)
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        Doc.nettoyer(cles)
        raise
    if request.form.get("depuis") == "liste":
        return redirect(url_for("blocages.liste"))
    if request.form.get("depuis") == "titrage":                   # destinations fixes : jamais une adresse fournie par le formulaire
        return redirect(url_for("titrage.fiche", dossier_id=b.dossier_id))
    return redirect(url_for("main.detail_dossier", dossier_id=b.dossier_id))
