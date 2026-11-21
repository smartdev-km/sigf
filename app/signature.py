"""Signature numérique du Directeur : image déposée depuis la page « Paramètres », imprimée sur les certificats et attestations.

L'image est contrôlée et ré-encodée (PNG, fond transparent, recadrée) : seuls des PNG ou JPEG valides sont acceptés, les métadonnées
disparaissent et un fichier piégé ne peut pas être conservé tel quel. Elle sert à l'impression seulement : l'authenticité d'un document
repose sur son QR code signé, pas sur cette image, et changer ou supprimer la signature ne rend pas un document déjà imprimé « non à jour ».
"""

import base64
import binascii
import uuid
from io import BytesIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from PIL import Image, UnidentifiedImageError

from . import constants as C
from .audit import audit
from .auth import roles_required
from .extensions import db
from .models import User, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("parametres", __name__)

MAX_OCTETS = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000
TAILLE_MAX = (1200, 600)
TITRE_DEFAUT = "Le Directeur"


class SignatureError(Exception):
    """Refus affichable à l'utilisateur."""


def preparer_image(data):
    """Contrôle et normalise l'image : retourne des octets PNG recadrés sur la signature, fond transparent."""
    if not data:
        raise SignatureError("Le fichier est vide.")
    if len(data) > MAX_OCTETS:
        raise SignatureError("L'image dépasse 2 Mo : réduisez-la ou exportez-la à une taille plus petite.")
    try:
        verif = Image.open(BytesIO(data))
        verif.verify()
        img = Image.open(BytesIO(data))
        format_ = img.format
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise SignatureError("Ce fichier n'est pas une image valide (PNG ou JPEG attendu).") from None
    if format_ not in ("PNG", "JPEG"):
        raise SignatureError("Format non accepté : déposez un PNG (de préférence à fond transparent) ou un JPEG.")
    if img.width * img.height > MAX_PIXELS:
        raise SignatureError("L'image est trop grande (16 millions de pixels au maximum).")
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    if alpha.getextrema() == (255, 255):                      # pas de transparence (JPEG, ou PNG sur fond blanc) : le blanc devient transparent
        clair = img.convert("L").point(lambda v: 0 if v >= 235 else 255)
        img.putalpha(clair)
    encre = img.getchannel("A").point(lambda v: 255 if v > 16 else 0)
    boite = encre.getbbox()
    if boite is None:
        raise SignatureError("L'image ne contient aucune signature visible (elle est vide ou entièrement blanche).")
    marge = max(4, int(max(boite[2] - boite[0], boite[3] - boite[1]) * 0.04))
    boite = (max(0, boite[0] - marge), max(0, boite[1] - marge), min(img.width, boite[2] + marge), min(img.height, boite[3] + marge))
    img = img.crop(boite)
    img.thumbnail(TAILLE_MAX, Image.LANCZOS)
    sortie = BytesIO()
    img.save(sortie, format="PNG", optimize=True)         # ré-encodage : plus aucune métadonnée du fichier d'origine
    return sortie.getvalue()


def _titre(brut):
    titre = " ".join((brut or "").split())
    if len(titre) > 60:
        raise SignatureError("La mention sous la signature est limitée à 60 caractères.")
    return titre or None


def lire_dessin(dessin):
    """Octets d'un dessin envoyé par le cadre de signature (« data:image/png;base64,… »)."""
    prefixe = "data:image/png;base64,"
    if not dessin.startswith(prefixe):
        raise SignatureError("Le dessin n'a pas pu être lu : réessayez, ou importez une image.")
    brut = dessin[len(prefixe):]
    if len(brut) > MAX_OCTETS * 4 // 3 + 16:
        raise SignatureError("Le dessin est trop volumineux : simplifiez-le.")
    try:
        return base64.b64decode(brut, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("Le dessin n'a pas pu être lu : réessayez, ou importez une image.") from None


def enregistrer(user, fichier, titre_brut, dessin=None):
    """Remplace la signature de `user` (dessin du cadre de signature, ou fichier importé) et/ou sa mention.
    Retourne (clé écrite, ancienne clé à supprimer) ; la clé écrite est à nettoyer en cas d'échec."""
    titre = _titre(titre_brut)
    nouvelle_cle = None
    ancienne = user.signature_cle
    avec_fichier = fichier is not None and bool(fichier.filename)
    if dessin or avec_fichier:
        png = preparer_image(lire_dessin(dessin) if dessin else fichier.stream.read(MAX_OCTETS + 1))
        nouvelle_cle = f"signatures/{user.id}/{uuid.uuid4().hex}.png"
        get_storage().put(nouvelle_cle, png, "image/png")
        user.signature_cle, user.signature_at = nouvelle_cle, maintenant()
    elif not ancienne:
        raise SignatureError("Dessinez ou importez d'abord votre signature.")
    user.signature_titre = titre
    audit("signature_enregistree" if nouvelle_cle else "signature_titre_modifiee", "user", user.id, nouveau=titre or TITRE_DEFAUT, user=user)
    return nouvelle_cle, (ancienne if nouvelle_cle else None)


def supprimer(user):
    ancienne = user.signature_cle
    user.signature_cle = user.signature_at = None
    audit("signature_supprimee", "user", user.id, user=user)
    return ancienne


def pour_impression(d, user, candidats=None):
    """(png, mention, nom) à imprimer sur un certificat, une attestation ou un acte, ou None.
    Signataire : le Directeur qui a validé le dossier (ou la mutation) ; à défaut, le Directeur qui imprime (s'il a une signature)."""
    for candidat in (candidats if candidats is not None else (d.valide_par, user)):
        if candidat is not None and candidat.role == C.DIRECTEUR and candidat.signature_cle:
            try:
                return get_storage().lire(candidat.signature_cle), candidat.signature_titre or TITRE_DEFAUT, candidat.nom
            except StorageError:
                continue                                   # image introuvable : on n'imprime pas de signature plutôt que d'échouer
    return None


# --------------------------------------------------------------------------
# Page « Paramètres » (Directeur)
# --------------------------------------------------------------------------
@bp.route("/parametres")
@roles_required(C.DIRECTEUR, C.ADMIN)
def page():
    """Directeur : sa propre signature. Administrateur : les signatures déposées par chaque compte Directeur (consultation)."""
    directeurs = []
    if current_user.role == C.ADMIN:
        directeurs = User.query.filter(User.role == C.DIRECTEUR).order_by(User.ile_id.is_(None), User.ile_id, User.nom).all()
    return render_template("parametres.html", titre_defaut=TITRE_DEFAUT, maximum_mo=MAX_OCTETS // (1024 * 1024), directeurs=directeurs)


@bp.route("/parametres/signature", methods=["POST"])
@roles_required(C.DIRECTEUR)
def enregistrer_route():
    nouvelle = ancienne = None
    try:
        nouvelle, ancienne = enregistrer(current_user, request.files.get("signature"), request.form.get("titre"), request.form.get("dessin"))
        db.session.commit()
        flash("Signature enregistrée : elle apparaîtra sur les certificats et attestations que vous validez." if nouvelle else "Mention enregistrée.", "success")
    except (SignatureError, StorageError) as e:
        db.session.rollback()
        if nouvelle:
            get_storage().delete(nouvelle)
        flash(str(e), "danger")
        return redirect(url_for("parametres.page"))
    except Exception:
        db.session.rollback()
        if nouvelle:
            get_storage().delete(nouvelle)
        raise
    if ancienne:
        get_storage().delete(ancienne)
    return redirect(url_for("parametres.page"))


@bp.route("/parametres/signature/supprimer", methods=["POST"])
@roles_required(C.DIRECTEUR)
def supprimer_route():
    if not current_user.signature_cle:
        flash("Aucune signature à supprimer.", "warning")
        return redirect(url_for("parametres.page"))
    ancienne = supprimer(current_user)
    db.session.commit()
    get_storage().delete(ancienne)
    flash("Signature supprimée : les prochains certificats seront imprimés sans image de signature.", "success")
    return redirect(url_for("parametres.page"))


def _reponse_image(cle):
    try:
        octets = get_storage().lire(cle)
    except StorageError:
        abort(404)
    r = Response(octets, mimetype="image/png")
    r.headers["Cache-Control"] = "private, no-store"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@bp.route("/parametres/signature")
@login_required
def image():
    """L'image de SA signature (aperçu sur la page « Paramètres ») : un Directeur ne peut récupérer que la sienne."""
    if current_user.role != C.DIRECTEUR or not current_user.signature_cle:
        abort(404)
    return _reponse_image(current_user.signature_cle)


@bp.route("/parametres/signature/<int:user_id>")
@roles_required(C.ADMIN)
def image_du_directeur(user_id):
    """L'administrateur consulte la signature de n'importe quel compte Directeur."""
    u = db.session.get(User, user_id)
    if u is None or u.role != C.DIRECTEUR or not u.signature_cle:
        abort(404)
    return _reponse_image(u.signature_cle)
