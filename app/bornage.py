"""Tracé de bornage : le KMZ/KML déposé à l'étape « Plan de bornage » du titrage et le document imprimable « Plan de bornage ».

Le plan de bornage est présenté comme le plan croquis (feuille A3, croquis à l'échelle, bornes numérotées et longueurs des côtés
sur le dessin) avec, en plus, le procès-verbal de bornage. Il n'est imprimable qu'une fois l'étape
« Plan de bornage » accomplie et un tracé de bornage déposé.
"""

import hashlib
import json
import uuid

from flask import Blueprint, abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from . import carte
from . import constants as C
from . import geo
from . import titrage as T
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Bornage, Dossier, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("bornage", __name__)

ETAPES_DEPOT = (C.TIT_BORNAGE, C.TIT_JC, C.TIT_TITRAGE)      # le tracé se dépose à l'étape du bornage, et peut être corrigé jusqu'au titre
ETAPES_APRES_BORNAGE = (C.TIT_JC, C.TIT_TITRAGE, C.TIT_FIN)


def courant(d):
    """Tracé de bornage en vigueur (version non remplacée) ou None."""
    return next((b for b in reversed(d.bornages) if b.remplace_at is None), None)


def peut_deposer(d, user):
    """Le Domaine (ou l'administrateur) dépose le tracé, à partir de l'étape « Plan de bornage » et jusqu'au titre."""
    return (
        T.en_titrage(d) and d.etape_titre in ETAPES_DEPOT and not d.est_bloque
        and W.peut_voir(d, user) and user.role in (C.ADMIN, C.TITRAGE_ACTEUR[C.TIT_BORNAGE])
    )


def etape_faite(d):
    """Le bornage a été accompli (l'étape « Plan de bornage » est derrière nous)."""
    return d.etape_titre in ETAPES_APRES_BORNAGE


def peut_imprimer(d, user):
    """Plan de bornage imprimable : étape accomplie, tracé déposé, par le Domaine, le Directeur ou l'administrateur."""
    return (
        d.origine == C.ORIGINE_NOUVEAU and d.statut == C.CERTIFIE and etape_faite(d) and courant(d) is not None
        and not d.est_bloque and user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE)
    )


def elements(b):
    try:
        return json.loads(b.elements_json) if b is not None and b.elements_json else []
    except ValueError:
        return []


def proces_verbal(d):
    """Étape « Plan de bornage » accomplie (procès-verbal : référence, date) ou None."""
    for e in reversed(d.etapes_titrage):
        if e.etape == C.TIT_BORNAGE and e.type == "fait" and e.annule_at is None:
            return e
    return None


def ecart_avec_plan_croquis(d, b):
    """(écart en % de superficie, le bornage recoupe-t-il le plan croquis) ; (None, None) sans plan croquis."""
    g = d.geometrie_courante
    if g is None or not g.superficie_m2:
        return None, None
    ecart = round((b.superficie_m2 - g.superficie_m2) / g.superficie_m2 * 100, 1)
    recoupe = geo.depuis_geojson(b.geojson).intersects(geo.depuis_geojson(g.geojson))
    return ecart, recoupe


def deposer(d, user, fichier, motif, cles):
    """Lit, valide et enregistre un tracé de bornage (sans commit). Retourne le Bornage. `cles` : clés écrites dans le stockage."""
    octets, nom, extension, resultat = carte.lire_fichier_trace(fichier)
    actuel = courant(d)
    motif = (motif or "").strip()
    if actuel is not None and not motif:
        raise geo.GeoError("Le motif du remplacement est obligatoire (le tracé de bornage actuel est conservé dans l'historique).")
    cle = f"dossiers/{d.id}/bornage/{uuid.uuid4().hex}{extension}"
    get_storage().put(cle, octets, carte.MIME[extension])
    cles.append(cle)
    b = Bornage(
        dossier_id=d.id,
        version=(actuel.version + 1) if actuel else 1,
        geojson=resultat.geojson,
        empreinte=resultat.empreinte,
        superficie_m2=resultat.superficie_m2,
        perimetre_m=resultat.perimetre_m,
        elements_json=json.dumps(resultat.elements, separators=(",", ":")) if resultat.elements else None,
        cle=cle,
        nom_original=nom,
        mime=carte.MIME[extension],
        taille=len(octets),
        sha256=hashlib.sha256(octets).hexdigest(),
        uploaded_by_id=user.id,
        motif_remplacement=(motif or None) if actuel else None,
    )
    if actuel is not None:
        actuel.remplace_at = maintenant()
    db.session.add(b)
    db.session.flush()
    audit(
        "bornage_trace_remplace" if actuel else "bornage_trace_ajoute", "bornage", b.id,
        ancien=f"v{actuel.version} {actuel.empreinte[:12]}" if actuel else None,
        nouveau=f"v{b.version} {b.empreinte[:12]} {b.superficie_m2:.0f} m²" + (f" ; {motif}" if actuel else ""),
        user=user,
    )
    return b


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user) or not T.peut_consulter(current_user) or not T.en_titrage(d):
        abort(404)
    return d


@bp.route("/dossiers/<int:dossier_id>/titrage/bornage", methods=["POST"])
@login_required
def route_deposer(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_deposer(d, current_user):
        abort(403)
    cles = []
    try:
        b = deposer(d, current_user, request.files.get("fichier"), request.form.get("motif"), cles)
        db.session.commit()
        flash(f"Tracé de bornage enregistré : {b.superficie_m2:,.0f} m², {_nb_bornes(b)} bornes.".replace(",", " "), "success")
        ecart, recoupe = ecart_avec_plan_croquis(d, b)
        if recoupe is False:
            flash("Attention : ce tracé de bornage ne recoupe pas le plan croquis de la parcelle. Vérifiez le fichier.", "warning")
        elif ecart is not None and abs(ecart) > 10:
            flash(f"Attention : la superficie bornée diffère de {ecart:+.1f} % du plan croquis.", "warning")
    except (geo.GeoError, StorageError) as e:
        db.session.rollback()
        _nettoyer(cles)
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        _nettoyer(cles)
        raise
    return redirect(url_for("titrage.fiche", dossier_id=d.id) + "#bornage")


def _nb_bornes(b):
    return sum(len(p["bornes"]) for p in geo.bornes(geo.depuis_geojson(b.geojson)))


def _nettoyer(cles):
    for cle in cles:
        get_storage().delete(cle)


@bp.route("/dossiers/<int:dossier_id>/titrage/bornage/<int:version>/telecharger")
@login_required
def telecharger(dossier_id, version):
    d = _dossier_ou_404(dossier_id)
    b = next((x for x in d.bornages if x.version == version), None)
    if b is None:
        abort(404)
    audit("bornage_trace_telecharge", "bornage", b.id)
    db.session.commit()
    try:
        return get_storage().reponse(b.cle, b.nom_original, b.mime, inline=False)
    except StorageError as e:
        flash(str(e), "danger")
        return redirect(url_for("titrage.fiche", dossier_id=d.id) + "#bornage")
