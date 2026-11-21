"""Description du terrain imprimée sur le plan croquis : limitrophes, nature et usage, levé, observations.

Saisie par le topographe pendant le circuit ; après la certification, seuls le Directeur et l'administrateur peuvent la corriger.
Toute modification est tracée (avant → après). Un plan croquis déjà imprimé devient « non à jour » si ces informations changent.
"""

from datetime import date

from flask import Blueprint, abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import corrections as Cor
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Dossier

bp = Blueprint("terrain", __name__)

LIBELLES = {champ: libelle for champ, libelle, _ in C.CHAMPS_DESCRIPTION}
MAXIMA = {champ: maxi for champ, _, maxi in C.CHAMPS_DESCRIPTION}


def peut_decrire(d, user):
    """Qui peut renseigner la description du terrain d'un NOUVEAU dossier."""
    if d.origine != C.ORIGINE_NOUVEAU or d.statut in (C.CLOTURE, C.ARCHIVE) or d.est_bloque:
        return False
    if d.statut == C.CERTIFIE:
        return user.role in (C.ADMIN, C.DIRECTEUR)
    return Cor.mode(user, d) == "direct"            # dossier en circuit : mêmes droits que la correction directe


def topographe_du_dossier(d):
    """Topographe du levé, tiré des comptes : le topographe privé rattaché au dossier, sinon le topographe qui l'a ouvert. Sinon None."""
    for u in (d.topographe, d.cree_par):
        if u is not None and u.role in (C.TOPO_PUBLIC, C.TOPO_PRIVE):
            return u
    return None


def description(d):
    """Champs renseignés : {champ: texte} (date au format ISO) ; vide si rien n'a été saisi."""
    sortie = {}
    for champ, _, _ in C.CHAMPS_DESCRIPTION:
        valeur = getattr(d, champ)
        if valeur is None or valeur == "":
            continue
        sortie[champ] = valeur.isoformat() if isinstance(valeur, date) else str(valeur)
    return sortie


class DescriptionError(Exception):
    """Saisie refusée (message affichable)."""


def lire(form):
    """Valide le formulaire : {champ: valeur typée ou None}."""
    valeurs, erreurs = {}, []
    for champ, libelle, maxi in C.CHAMPS_DESCRIPTION:
        brut = form.get(champ, "")
        if champ == "observations_plan":
            texte = "\n".join(" ".join(ligne.split()) for ligne in (brut or "").replace("\r", "").split("\n")).strip()
        else:
            texte = " ".join((brut or "").split())
        if len(texte) > maxi:
            erreurs.append(f"« {libelle} » est trop long (maximum {maxi} caractères).")
            continue
        if champ == "nature_terrain" and texte and texte not in C.NATURES_TERRAIN:
            erreurs.append("Nature du terrain inconnue.")
        elif champ == "mode_leve" and texte and texte not in C.MODES_LEVE:
            erreurs.append("Mode de levé inconnu.")
        elif champ == "date_leve":
            if texte:
                try:
                    jour = date.fromisoformat(texte)
                except ValueError:
                    erreurs.append("La date du levé est invalide.")
                    continue
                if jour > date.today():
                    erreurs.append("La date du levé ne peut pas être dans le futur.")
                    continue
                valeurs[champ] = jour
            else:
                valeurs[champ] = None
            continue
        valeurs[champ] = texte or None
    if erreurs:
        raise DescriptionError(" ".join(erreurs))
    return valeurs


def appliquer_a_la_creation(d, valeurs):
    """Description saisie dans le formulaire « Nouveau dossier » : enregistrée avec le dossier (sans commit)."""
    nouveaux = []
    for champ, valeur in valeurs.items():
        if valeur:
            setattr(d, champ, valeur)
            nouveaux.append(f"{LIBELLES[champ]} : {_texte(valeur)}")
    if nouveaux:
        audit("description_terrain_modifiee", "dossier", d.id, nouveau=" ; ".join(nouveaux))


@bp.route("/dossiers/<int:dossier_id>/description-terrain", methods=["POST"])
@login_required
def enregistrer(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    if not peut_decrire(d, current_user):
        abort(403)
    try:
        valeurs = lire(request.form)
    except DescriptionError as e:
        flash(str(e), "danger")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id, modifier=1) + "#description-terrain")
    anciens, nouveaux = [], []
    for champ, nouveau in valeurs.items():
        ancien = getattr(d, champ)
        if (ancien or None) != (nouveau or None):
            anciens.append(f"{LIBELLES[champ]} : {_texte(ancien)}")
            nouveaux.append(f"{LIBELLES[champ]} : {_texte(nouveau)}")
            setattr(d, champ, nouveau)
    changements = nouveaux
    if not changements:
        flash("Aucune modification.", "info")
    else:
        audit("description_terrain_modifiee", "dossier", d.id, ancien=" ; ".join(anciens), nouveau=" ; ".join(nouveaux))
        db.session.commit()
        flash("Description du terrain enregistrée : elle figure sur le plan croquis." + (" Les plans croquis déjà imprimés ne sont plus à jour." if _plan_deja_imprime(d) else ""), "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#description-terrain")


def _texte(valeur):
    return valeur.isoformat() if isinstance(valeur, date) else (valeur or "—")


def _plan_deja_imprime(d):
    from .models import DocumentEmis

    return DocumentEmis.query.filter_by(dossier_id=d.id, type="plan_croquis").first() is not None
