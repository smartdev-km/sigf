"""Scans rattachés aux dossiers : validation, enregistrement, accès contrôlé."""

import hashlib
import os
import re
import uuid
from dataclasses import dataclass

from flask import Blueprint, abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Document, Dossier, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("documents", __name__)


TYPE_AVIS = "Avis"
MAX_PIECES_AVIS = 5


class DocumentError(Exception):
    """Fichier refusé (message affichable à l'utilisateur)."""


@dataclass
class Fichier:
    nom: str
    data: bytes
    mime: str
    ext: str
    sha256: str
    type_doc: str | None = None
    libelle: str | None = None


# --------------------------------------------------------------------------
# Droits
# --------------------------------------------------------------------------
def section_ouverte(d):
    """La section « Documents » d'un NOUVEAU dossier n'apparaît que lorsque le dossier est complet : certifié (tous ses documents sont
    alors générés automatiquement) ou dès qu'un document a été généré (plan croquis imprimé). Les anciens dossiers l'ont toujours,
    et un dossier qui contient déjà des documents la garde."""
    if d.origine == C.ORIGINE_ANCIEN or d.statut in (C.ARCHIVE, C.CERTIFIE):
        return True                                       # un dossier certifié est complet : ses documents sont générés automatiquement
    if any(x.retire_at is None and x.type != TYPE_AVIS for x in d.documents):      # les pièces jointes à l'avis du topographe privé n'ouvrent pas la section
        return True
    from .models import DocumentEmis

    return DocumentEmis.query.filter_by(dossier_id=d.id).first() is not None


def peut_televerser(d, user):
    if not section_ouverte(d):
        return False
    if d.statut == C.CLOTURE or d.est_bloque:
        return False
    if d.statut == C.ARCHIVE:
        return user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE)
    if user.role == C.TOPO_PRIVE:
        return False  # le topographe privé n'a pas accès à la section « Documents »
    return user.role in (C.ADMIN, C.DIRECTEUR, C.TOPO_PUBLIC, C.DOMAINE)


def peut_retirer(user):
    return user.role in (C.ADMIN, C.DIRECTEUR)


def documents_visibles(d, user):
    """Le topographe privé n'a pas accès à la section « Documents »."""
    if W.est_restreint(user):
        return []
    return [x for x in d.documents if x.retire_at is None]


def pieces_avis(d):
    """Documents joints par le topographe privé à son avis."""
    return [x for x in d.documents if x.retire_at is None and x.type == TYPE_AVIS and d.topographe_id and x.uploaded_by_id == d.topographe_id]


def peut_voir_document(doc, user):
    if doc.retire_at is not None or not W.peut_voir(doc.dossier, user):
        return False
    if W.est_restreint(user):
        # le topographe privé n'a pas la section « Documents », mais il retrouve les pièces qu'il a jointes à SON avis
        return doc.type == TYPE_AVIS and doc.uploaded_by_id == user.id
    return True


# --------------------------------------------------------------------------
# Validation du contenu
# --------------------------------------------------------------------------
def _detecter(data):
    """Type réel d'après les premiers octets (l'extension et le Content-Type client ne comptent pas)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tif"
    return None


def _nom_affichable(brut):
    nom = (brut or "").replace("\\", "/").split("/")[-1]
    nom = re.sub(r"[\x00-\x1f\x7f]", "", nom).strip()
    return nom[:200] or "document"


def preparer(fichiers_form, types=None, libelles=None):
    """Lit et valide les fichiers du formulaire. Lève DocumentError au moindre problème.

    Si `types` est fourni, il doit y avoir un type par ligne du formulaire (même ordre
    que les fichiers) : chaque fichier reçoit alors son propre type. `libelles` (même ordre)
    porte le nom d'un document de type « Autre », obligatoire dans ce cas.
    """
    if types is not None:
        libelles = libelles or [""] * len(types)  # liste absente : aucun nom précisé
        if len(types) != len(fichiers_form) or len(libelles) != len(types):
            raise DocumentError("Formulaire invalide : chaque fichier doit avoir un type.")
        lignes = [(f, t, lib) for f, t, lib in zip(fichiers_form, types, libelles) if f and f.filename]
    else:
        lignes = [(f, None, "") for f in fichiers_form if f and f.filename]
    if not lignes:
        raise DocumentError("Aucun fichier sélectionné.")
    maxi = current_app.config["MAX_FICHIER_MO"] * 1024 * 1024
    prets = []
    for f, type_ligne, libelle in lignes:
        nom = _nom_affichable(f.filename)
        if type_ligne is not None and type_ligne not in C.TYPES_DOCUMENT:
            raise DocumentError(f"« {nom} » : type de document invalide.")
        libelle = re.sub(r"\s+", " ", (libelle or "")).strip()
        if type_ligne == C.TYPE_AUTRE:
            if not libelle:
                raise DocumentError(f"« {nom} » : précisez le nom de ce document (type « Autre »).")
            if len(libelle) > 120:
                raise DocumentError("Le nom d'un document est limité à 120 caractères.")
        else:
            libelle = ""
        ext_demandee = os.path.splitext(nom)[1].lower()
        if ext_demandee not in C.EXTENSIONS_AUTORISEES:
            raise DocumentError(f"« {nom} » : format non accepté (PDF, JPG, PNG ou TIFF uniquement).")
        data = f.stream.read(maxi + 1)
        if len(data) > maxi:
            raise DocumentError(f"« {nom} » dépasse {current_app.config['MAX_FICHIER_MO']} Mo.")
        if not data:
            raise DocumentError(f"« {nom} » est vide.")
        detecte = _detecter(data)
        if detecte is None:
            raise DocumentError(f"« {nom} » n'est pas un PDF, JPG, PNG ou TIFF valide.")
        mime, ext = detecte
        prets.append(Fichier(nom, data, mime, ext, hashlib.sha256(data).hexdigest(), type_ligne, libelle or None))
    return prets


def enregistrer(d, fichiers, type_doc, user, cles_ecrites):
    """Écrit les fichiers dans le stockage et ajoute les lignes Document (sans commit).

    `cles_ecrites` permet à l'appelant de nettoyer le stockage si la transaction échoue.
    """
    stockage = get_storage()
    docs = []
    for fic in fichiers:
        type_fic = fic.type_doc or type_doc
        if type_fic not in C.TYPES_DOCUMENT:
            raise DocumentError("Type de document invalide.")
        cle = f"dossiers/{d.id}/{uuid.uuid4().hex}{fic.ext}"
        stockage.put(cle, fic.data, fic.mime)
        cles_ecrites.append(cle)
        doc = Document(
            dossier_id=d.id,
            type=type_fic,
            libelle=fic.libelle,
            nom_original=fic.nom,
            cle=cle,
            mime=fic.mime,
            taille=len(fic.data),
            sha256=fic.sha256,
            uploaded_by_id=user.id,
        )
        db.session.add(doc)
        db.session.flush()
        audit("document_ajoute", "document", doc.id, nouveau=f"{fic.nom} ({fic.libelle or type_fic}) sha256={fic.sha256}", user=user)
        docs.append(doc)
    return docs


def nettoyer(cles):
    stockage = get_storage()
    for cle in cles:
        stockage.delete(cle)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    return d


@bp.route("/dossiers/<int:dossier_id>/documents", methods=["POST"])
@login_required
def televerser(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_televerser(d, current_user):
        abort(403)
    cles = []
    try:
        fichiers = preparer(
            request.files.getlist("fichiers"),
            request.form.getlist("type_document"),
            request.form.getlist("libelle_document"),
        )
        docs = enregistrer(d, fichiers, None, current_user, cles)
        db.session.commit()
        flash(f"{len(docs)} document(s) ajouté(s).", "success")
    except (DocumentError, StorageError) as e:
        db.session.rollback()
        nettoyer(cles)
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        nettoyer(cles)
        raise
    return redirect(url_for("main.detail_dossier", dossier_id=d.id))


@bp.route("/documents/<int:doc_id>")
@login_required
def voir(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None or not peut_voir_document(doc, current_user):
        abort(404)
    audit("document_consulte", "document", doc.id)
    db.session.commit()
    inline = doc.mime != "image/tiff" and request.args.get("telecharger") != "1"
    try:
        return get_storage().reponse(doc.cle, doc.nom_original, doc.mime, inline)
    except StorageError as e:
        flash(str(e), "danger")
        return redirect(url_for("main.detail_dossier", dossier_id=doc.dossier_id))


@bp.route("/documents/<int:doc_id>/retirer", methods=["POST"])
@login_required
def retirer(doc_id):
    if not peut_retirer(current_user):
        abort(403)
    doc = db.session.get(Document, doc_id)
    if doc is None or doc.retire_at is not None:
        abort(404)
    motif = (request.form.get("motif") or "").strip()
    if not motif:
        flash("Le motif du retrait est obligatoire.", "danger")
    else:
        # Le fichier reste dans le stockage (preuve) ; il n'est simplement plus accessible.
        doc.retire_at = maintenant()
        doc.retire_par_id = current_user.id
        doc.retire_motif = motif
        audit("document_retire", "document", doc.id, ancien=doc.nom_original, nouveau=motif)
        db.session.commit()
        flash("Document retiré.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=doc.dossier_id))
