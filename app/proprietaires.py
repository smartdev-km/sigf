"""Pages « Propriétaires » : liste des personnes et profil (biens, anciens biens, créances, parcours)."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import personnes as P
from . import workflow as W
from .audit import audit
from .carte import tuiles
from .corrections import CHAMPS_PROPRIETAIRE
from .extensions import db
from .models import ModificationPersonne, Proprietaire

bp = Blueprint("proprietaires", __name__)


def _interdit():
    if W.est_restreint(current_user):
        abort(404)


@bp.route("/proprietaires")
@login_required
def liste():
    recherche = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)
    demandes = P.demandes_en_attente(current_user).order_by(ModificationPersonne.id).all()
    onglet = "attente" if demandes and request.args.get("onglet") == "attente" else "liste"  # l'onglet n'existe que s'il y a des demandes
    pagination = P.representants(recherche, current_user).order_by(Proprietaire.nom, Proprietaire.prenom, Proprietaire.id).paginate(
        page=page, per_page=25, error_out=False
    )
    resume = {}
    for p in pagination.items:
        fiches = P.groupe(p, current_user)
        patrimoine = P.biens(fiches, current_user)
        resume[p.id] = {
            "fiches": len(fiches),
            "actuels": len(patrimoine["actuels"]),
            "anciens": len(patrimoine["anciens"]),
            "creances": sum(1 for c in patrimoine["creances"] if c["active"]),
            "superficie": patrimoine["superficie_totale"],
        }
    return render_template(
        "proprietaires/liste.html",
        pagination=pagination,
        resume=resume,
        q=recherche,
        total=P.representants(None, current_user).count(),
        restreint=W.est_restreint(current_user),
        demandes=[] if W.est_restreint(current_user) else demandes,
        onglet=onglet,
        lignes=P.lignes,
        rapprochements=lambda m: P.rapprochements(m, current_user),
        peut_decider=P.peut_decider(current_user),
        peut_proposer=P.peut_proposer(current_user),
    )


@bp.route("/proprietaires/<int:proprietaire_id>")
@login_required
def profil(proprietaire_id):
    p = db.get_or_404(Proprietaire, proprietaire_id)
    fiches = P.groupe(p, current_user)
    if not fiches:
        abort(404)                                            # personne sans dossier dans le périmètre du compte (île, ou dossiers du topographe privé)
    principale = P.fiche_principale(fiches)
    audit("proprietaire_consulte", "proprietaire", p.id)
    db.session.commit()
    restreint = W.est_restreint(current_user)                 # topographe privé : coordonnées, parcelles et carte seulement
    patrimoine = P.biens(fiches, current_user)
    return render_template(
        "proprietaires/detail.html",
        p=principale,
        fiches=fiches,
        patrimoine=patrimoine,
        restreint=restreint,
        carte=P.carte(patrimoine, avec_anciens=not restreint),
        tuiles=tuiles(),
        similaires=[] if restreint else P.similaires(principale, fiches, user=current_user),
        demandes=[] if restreint else P.en_attente(fiches),
        lignes=P.lignes,
        peut_proposer=P.peut_proposer(current_user),
        telephones=sorted({f.telephone for f in fiches if f.telephone}),
        adresses=sorted({f.adresse for f in fiches if f.adresse}),
    )


@bp.route("/proprietaires/<int:proprietaire_id>/modifier", methods=["GET", "POST"])
@login_required
def modifier(proprietaire_id):
    _interdit()
    if not P.peut_proposer(current_user):
        abort(403)
    p = db.get_or_404(Proprietaire, proprietaire_id)
    fiches = P.groupe(p, current_user)
    if not fiches:
        abort(404)
    principale = P.fiche_principale(fiches)
    erreur = None
    if request.method == "POST":
        try:
            changements, motif = P.lire_formulaire(principale, request.form)
            P.proposer(current_user, principale, fiches, changements, motif)
            db.session.commit()
            flash("Demande enregistrée : elle attend la validation du Directeur. Rien ne change avant sa décision.", "success")
            return redirect(url_for("proprietaires.profil", proprietaire_id=principale.id))
        except P.ModificationError as e:
            db.session.rollback()
            erreur = str(e)
    return (
        render_template(
            "proprietaires/modifier.html",
            p=principale,
            fiches=fiches,
            v=request.form if request.method == "POST" else P.valeurs(principale),
            erreur=erreur,
            demandes=P.en_attente(fiches),
            lignes=P.lignes,
            champs=CHAMPS_PROPRIETAIRE,
        ),
        400 if erreur else 200,
    )


def _decider(demande_id, action):
    if not P.peut_decider(current_user):
        abort(403)
    _interdit()
    m = db.get_or_404(ModificationPersonne, demande_id)
    if not P.groupe(m.proprietaire, current_user):
        abort(404)
    try:
        if action == "valider":
            P.valider(current_user, m)
            flash("Modification validée et appliquée à toutes les fiches de la personne.", "success")
        else:
            P.rejeter(current_user, m, request.form.get("motif"))
            flash("Demande rejetée.", "success")
        db.session.commit()
    except P.ModificationError as e:
        db.session.rollback()
        flash(str(e), "danger")
    if request.form.get("depuis") == "liste":  # destinations fixes : jamais une adresse fournie par le formulaire
        reste = P.demandes_en_attente(current_user).count()
        return redirect(url_for("proprietaires.liste", onglet="attente" if reste else None))
    return redirect(url_for("proprietaires.profil", proprietaire_id=m.proprietaire_id))


@bp.route("/proprietaires/modifications/<int:demande_id>/valider", methods=["POST"])
@login_required
def valider_modification(demande_id):
    return _decider(demande_id, "valider")


@bp.route("/proprietaires/modifications/<int:demande_id>/rejeter", methods=["POST"])
@login_required
def rejeter_modification(demande_id):
    return _decider(demande_id, "rejeter")
