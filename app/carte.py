"""Carte : import KMZ/KML, tracé de la parcelle, chevauchements, carte générale."""

import hashlib
import json
import uuid

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from . import constants as C
from . import geo
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Dossier, Geometrie, Ile, Parcelle, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("carte", __name__)

MAX_FICHIER_MO = 10
MIME = {".kmz": "application/vnd.google-earth.kmz", ".kml": "application/vnd.google-earth.kml+xml"}


# --------------------------------------------------------------------------
# Droits
# --------------------------------------------------------------------------
def peut_gerer_trace(user, d):
    """Qui peut déposer ou remplacer le tracé d'un dossier."""
    if d.statut == C.CLOTURE or d.est_bloque:
        return False
    if d.statut == C.ARCHIVE:
        return user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE, C.TOPO_PUBLIC)
    if user.role == C.TOPO_PRIVE:
        return d.topographe_id == user.id and d.avis_envoye_at is None
    return user.role in (C.ADMIN, C.DIRECTEUR, C.TOPO_PUBLIC)


def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    return d


# --------------------------------------------------------------------------
# Voisins et chevauchements
# --------------------------------------------------------------------------
def voisins_proches(g):
    """Tracés en vigueur d'AUTRES dossiers dont le rectangle englobant touche celui de `g`."""
    return (
        Geometrie.query.join(Dossier, Dossier.id == Geometrie.dossier_id)
        .filter(
            Dossier.statut != C.CLOTURE,
            Geometrie.remplace_at.is_(None),
            Geometrie.dossier_id != g.dossier_id,
            Geometrie.min_lon <= g.max_lon,
            Geometrie.max_lon >= g.min_lon,
            Geometrie.min_lat <= g.max_lat,
            Geometrie.max_lat >= g.min_lat,
        ).all()
    )


def voisins_detectes(g, tolerance_m=3.0, limite=12):
    """Parcelles enregistrées dans le système qui bordent celle-ci : [{"dossier", "cote", "distance_m", "chevauche"}], triées par côté.
    Utile pour les limitrophes du plan croquis ; l'information est « au jour de l'impression »."""
    if g is None:
        return []
    geom = geo.depuis_geojson(g.geojson)
    # présélection par rectangle englobant, élargi de ~10 m
    marge = 0.0001
    candidats = (
        Geometrie.query.join(Dossier, Dossier.id == Geometrie.dossier_id)
        .filter(
            Dossier.statut != C.CLOTURE,
            Geometrie.remplace_at.is_(None),
            Geometrie.dossier_id != g.dossier_id,
            Geometrie.min_lon <= g.max_lon + marge,
            Geometrie.max_lon >= g.min_lon - marge,
            Geometrie.min_lat <= g.max_lat + marge,
            Geometrie.max_lat >= g.min_lat - marge,
        )
        .all()
    )
    trouves = []
    for v in candidats:
        r = geo.voisinage(geom, geo.depuis_geojson(v.geojson), tolerance_m)
        if r is not None:
            trouves.append({"dossier": v.dossier, "cote": r[1], "distance_m": r[0], "chevauche": r[2]})
    ordre = {c: i for i, c in enumerate(geo.COTES_CARDINAUX)}
    trouves.sort(key=lambda t: (ordre[t["cote"]], t["distance_m"], t["dossier"].numero))
    return trouves[:limite]


def chevauchements_de(g):
    """[(Geometrie voisine, aire_m2, pct)] triés du plus grave au moins grave."""
    voisins = voisins_proches(g)
    par_id = {v.id: v for v in voisins}
    trouves = geo.chevauchements(geo.depuis_geojson(g.geojson), [(v.id, geo.depuis_geojson(v.geojson)) for v in voisins])
    return [(par_id[t["cle"]], t["aire_m2"], t["pct"]) for t in trouves]


def peut_voir_detail_voisin(user, voisin):
    """Un topographe privé ne doit pas découvrir le contenu des dossiers des autres."""
    return W.peut_voir(voisin.dossier, user)


# --------------------------------------------------------------------------
# Enregistrement
# --------------------------------------------------------------------------
def lire_fichier_trace(fichier):
    """Valide un KMZ/KML envoyé par formulaire. Retourne (octets, nom, extension, Resultat)."""
    if not fichier or not fichier.filename:
        raise geo.GeoError("Aucun fichier sélectionné.")
    nom = fichier.filename.replace("\\", "/").split("/")[-1][:200]
    extension = "." + nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    if extension not in MIME:
        raise geo.GeoError("Format non accepté : envoyez un fichier .kmz ou .kml.")
    octets = fichier.stream.read(MAX_FICHIER_MO * 1024 * 1024 + 1)
    if len(octets) > MAX_FICHIER_MO * 1024 * 1024:
        raise geo.GeoError(f"Fichier trop volumineux (maximum {MAX_FICHIER_MO} Mo).")
    if not octets:
        raise geo.GeoError("Le fichier est vide.")
    return octets, nom, extension, geo.lire(octets, nom)


def enregistrer_trace(d, user, fichier, motif=None):
    """Lit, valide et enregistre un tracé (sans commit). Retourne (Geometrie, cle_stockage)."""
    octets, nom, extension, resultat = lire_fichier_trace(fichier)

    actuel = d.geometrie_courante
    if actuel is not None:
        motif = (motif or "").strip()
        if not motif:
            raise geo.GeoError("Le motif du remplacement est obligatoire (le tracé actuel est conservé dans l'historique).")
    else:
        motif = None

    cle = f"dossiers/{d.id}/trace/{uuid.uuid4().hex}{extension}"
    get_storage().put(cle, octets, MIME[extension])
    minx, miny, maxx, maxy = resultat.bbox
    g = Geometrie(
        dossier_id=d.id,
        version=(actuel.version + 1) if actuel else 1,
        geojson=resultat.geojson,
        empreinte=resultat.empreinte,
        superficie_m2=resultat.superficie_m2,
        perimetre_m=resultat.perimetre_m,
        centre_lon=resultat.centre[0],
        centre_lat=resultat.centre[1],
        min_lon=minx,
        min_lat=miny,
        max_lon=maxx,
        max_lat=maxy,
        cle=cle,
        nom_original=nom,
        mime=MIME[extension],
        taille=len(octets),
        sha256=hashlib.sha256(octets).hexdigest(),
        uploaded_by_id=user.id,
        motif_remplacement=motif,
        elements_json=json.dumps(resultat.elements, separators=(",", ":")) if resultat.elements else None,
    )
    if actuel is not None:
        actuel.remplace_at = maintenant()
    db.session.add(g)
    db.session.flush()
    audit(
        "trace_remplace" if actuel else "trace_ajoute",
        "geometrie",
        g.id,
        ancien=f"v{actuel.version} {actuel.empreinte[:12]}" if actuel else None,
        nouveau=f"v{g.version} {g.empreinte[:12]} {g.superficie_m2:.0f} m² ; {motif or ''}".strip(" ;"),
        user=user,
    )
    return g, cle


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@bp.route("/dossiers/<int:dossier_id>/trace", methods=["POST"])
@login_required
def televerser(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not peut_gerer_trace(current_user, d):
        abort(403)
    cle = None
    try:
        g, cle = enregistrer_trace(d, current_user, request.files.get("fichier"), request.form.get("motif"))
        db.session.commit()
    except (geo.GeoError, StorageError) as e:
        db.session.rollback()
        if cle:
            get_storage().delete(cle)
        flash(str(e), "danger")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#carte")
    except Exception:
        db.session.rollback()
        if cle:
            get_storage().delete(cle)
        raise
    n_el = len(json.loads(g.elements_json)) if g.elements_json else 0
    flash(f"Tracé enregistré : superficie mesurée {g.superficie_m2:,.0f} m².".replace(",", " ") + (f" {n_el} repère(s) du croquis (routes, points, zones) lu(s) dans le fichier." if n_el else ""), "success")
    nb = len(chevauchements_de(g))
    if nb:
        flash(f"Attention : ce tracé chevauche {nb} autre(s) parcelle(s). Vérifiez avant de poursuivre.", "warning")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#carte")


@bp.route("/dossiers/<int:dossier_id>/trace/<int:version>/telecharger")
@login_required
def telecharger(dossier_id, version):
    d = _dossier_ou_404(dossier_id)
    g = next((x for x in d.geometries if x.version == version), None)
    if g is None:
        abort(404)
    audit("trace_telecharge", "geometrie", g.id)
    db.session.commit()
    try:
        return get_storage().reponse(g.cle, g.nom_original, g.mime, inline=False)
    except StorageError as e:
        flash(str(e), "danger")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#carte")


# --------------------------------------------------------------------------
# Export pour Google Earth
# --------------------------------------------------------------------------
def _anneau_kml(anneau):
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y in anneau.coords)


def kml_google_earth(d, g):
    """KML prêt à ouvrir dans Google Earth : contour jaune bien visible, nom, fiche, et caméra déjà
    placée sur la parcelle (LookAt). Contient le strict minimum : pas de propriétaire ni de numéros."""
    from math import cos, hypot, radians
    from xml.sax.saxutils import escape

    from shapely.geometry.polygon import orient

    forme = geo.depuis_geojson(g.geojson)
    polygones = list(forme.geoms) if forme.geom_type == "MultiPolygon" else [forme]
    blocs = []
    for p in polygones:
        p = orient(p, 1.0)  # contour anti-horaire, trous horaires (convention KML)
        interieurs = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_anneau_kml(i)}</coordinates></LinearRing></innerBoundaryIs>" for i in p.interiors)
        blocs.append(
            "<Polygon><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>"
            f"<outerBoundaryIs><LinearRing><coordinates>{_anneau_kml(p.exterior)}</coordinates></LinearRing></outerBoundaryIs>{interieurs}</Polygon>"
        )
    geometrie = blocs[0] if len(blocs) == 1 else f"<MultiGeometry>{''.join(blocs)}</MultiGeometry>"

    # Distance de vue : toute la parcelle visible, jamais plus près que 80 m
    largeur = (g.max_lon - g.min_lon) * 111320.0 * cos(radians(g.centre_lat))
    hauteur = (g.max_lat - g.min_lat) * 110574.0
    distance = max(80, round(hypot(largeur, hauteur) * 1.8))

    p = d.parcelle
    lignes = [f"Dossier : {d.numero}", f"Île : {p.ile.nom}", f"Commune : {p.commune}"]
    if p.village:
        lignes.append(f"Village / quartier : {p.village}")
    lignes += [f"Superficie mesurée : {g.superficie_m2:,.0f} m²".replace(",", " "), f"Périmètre : {g.perimetre_m:,.0f} m".replace(",", " "), f"Tracé : version {g.version}"]
    description = escape("\n".join(lignes)).replace("\n", "<br/>")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(d.numero)}</name>"
        '<Style id="parcelle"><LineStyle><color>ff00ffff</color><width>3</width></LineStyle>'
        "<PolyStyle><color>3300ffff</color><fill>1</fill><outline>1</outline></PolyStyle></Style>"
        f"<LookAt><longitude>{g.centre_lon:.7f}</longitude><latitude>{g.centre_lat:.7f}</latitude><altitude>0</altitude>"
        f"<heading>0</heading><tilt>0</tilt><range>{distance}</range><altitudeMode>clampToGround</altitudeMode></LookAt>"
        f"<Placemark><name>{escape(d.numero)}</name><description>{description}</description><styleUrl>#parcelle</styleUrl>{geometrie}</Placemark>"
        "</Document></kml>"
    ).encode("utf-8")


@bp.route("/dossiers/<int:dossier_id>/trace/google-earth.kml")
@login_required
def google_earth(dossier_id):
    d = _dossier_ou_404(dossier_id)
    g = d.geometrie_courante
    if g is None:
        abort(404)
    audit("trace_export_google_earth", "geometrie", g.id)
    db.session.commit()
    return Response(
        kml_google_earth(d, g),
        mimetype="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f'attachment; filename="{d.numero}.kml"', "Cache-Control": "no-store"},
    )


def tuiles():
    cfg = current_app.config
    return {
        "satellite": {"url": cfg["TUILES_SATELLITE_URL"], "attribution": cfg["TUILES_SATELLITE_ATTRIBUTION"], "zoom_natif": cfg["TUILES_SATELLITE_ZOOM_NATIF"]},
        "plan": {"url": cfg["TUILES_PLAN_URL"], "attribution": cfg["TUILES_PLAN_ATTRIBUTION"], "zoom_natif": 19},
    }


def donnees_carte_dossier(d):
    """GeoJSON à afficher sur la fiche : le tracé du dossier et les parcelles qui le chevauchent."""
    g = d.geometrie_courante
    if g is None:
        return None
    voisins = []
    for v, aire, pct in chevauchements_de(g):
        visible = peut_voir_detail_voisin(current_user, v)
        voisins.append(
            {
                "type": "Feature",
                "geometry": json.loads(v.geojson),
                "properties": {
                    "numero": v.dossier.numero if visible else None,
                    "url": url_for("main.detail_dossier", dossier_id=v.dossier_id) if visible else None,
                    "aire_m2": aire,
                    "pct": pct,
                },
            }
        )
    return {
        "tuiles": tuiles(),
        "parcelle": {"type": "Feature", "geometry": json.loads(g.geojson), "properties": {"numero": d.numero}},
        "voisins": {"type": "FeatureCollection", "features": voisins},
        "centre": [g.centre_lat, g.centre_lon],
        "couleur": COULEURS_CARTE.get(d.statut, "#2a78d6"),  # même couleur de statut que sur la carte des parcelles
        "statut": W.statut_affiche(d, current_user),
    }


@bp.route("/carte")
@login_required
def carte_generale():
    iles = W.iles_visibles(current_user)
    ile = W.ile_de(current_user) or request.args.get("ile", type=int)
    return render_template("carte.html", iles=iles, ile=ile, tuiles=tuiles(), legende=legende_carte(), ile_fixe=bool(W.ile_de(current_user)))


MAX_PARCELLES_CARTE = 3000

# Une couleur par statut, lisible sur fond satellite (les anciens dossiers ne sont plus blancs).
COULEURS_CARTE = {
    C.PLAN_CROQUIS: "#2a78d6",
    C.ATTENTE_AVIS: "#e0b000",
    C.SAISIE_RSP: "#14a3c7",
    C.ENREGISTREMENT_RCP: "#8a6bd6",
    C.VALIDATION_DIRECTEUR: "#d6336c",
    C.CERTIFIE: "#1baf7a",
    C.ARCHIVE: "#eb6834",
}


def legende_carte():
    return [{"code": k, "label": C.STATUTS[k], "couleur": v} for k, v in COULEURS_CARTE.items()]


@bp.route("/carte/donnees.json")
@login_required
def donnees_generales():
    q = (
        W.dossiers_visibles(current_user)
        .join(Geometrie, Geometrie.dossier_id == Dossier.id)
        .join(Parcelle, Parcelle.id == Dossier.parcelle_id)
        .filter(Geometrie.remplace_at.is_(None), Dossier.statut != C.CLOTURE)
    )
    ile_id = request.args.get("ile", type=int)
    if ile_id:
        q = q.filter(Parcelle.ile_id == ile_id)
    total = q.count()
    lignes = (
        q.options(joinedload(Dossier.parcelle).joinedload(Parcelle.ile))
        .with_entities(Dossier, Geometrie)
        .order_by(Dossier.id)
        .limit(MAX_PARCELLES_CARTE)
        .all()
    )
    restreint = W.est_restreint(current_user)
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(g.geojson),
            "properties": {
                "numero": d.numero,
                "statut": W.statut_affiche(d, current_user),
                "couleur": C.STATUT_COULEUR.get(d.statut, "secondary"),
                "statut_code": d.statut,
                "origine": d.origine,
                "ile": d.parcelle.ile.nom,
                "commune": d.parcelle.commune,
                "village": d.parcelle.village,
                "google_earth": url_for("carte.google_earth", dossier_id=d.id),
                "proprietaire": None if restreint else d.proprietaire_label,
                "superficie_m2": round(g.superficie_m2),
                "url": url_for("main.detail_dossier", dossier_id=d.id),
            },
        }
        for d, g in lignes
    ]
    r = jsonify({"type": "FeatureCollection", "features": features, "total": total, "tronque": total > len(features)})
    r.headers["Cache-Control"] = "no-store"
    return r
