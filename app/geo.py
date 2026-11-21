"""Lecture des fichiers KMZ/KML, calculs géographiques et détection des chevauchements.

Tout fichier envoyé est traité comme hostile : archive piégée (zip bomb), XML malveillant
(entités externes, « milliard de rires »), coordonnées absurdes ou polygones invalides sont
rejetés avec un message clair. Aucune « réparation » silencieuse du tracé : un plan foncier
ne doit jamais être modifié sans que le topographe le sache.
"""

import io
import json
import math
import zipfile
from dataclasses import dataclass, field

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pyproj import Geod, Transformer
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity

GEOD = Geod(ellps="WGS84")
# UTM zone 38 Sud (WGS84) : le système usuel des plans de levé aux Comores
UTM38S = Transformer.from_crs("EPSG:4326", "EPSG:32738", always_xy=True)

# Emprise des trois îles (Ngazidja, Mwali, Ndzuwani), avec une marge. Mayotte est exclue.
EMPRISE = {"lon_min": 43.0, "lon_max": 44.8, "lat_min": -12.6, "lat_max": -11.2}

MAX_KML_OCTETS = 20 * 1024 * 1024      # taille d'un KML une fois décompressé
MAX_ENTREES_ZIP = 500
MAX_TOTAL_ZIP = 100 * 1024 * 1024
MAX_POLYGONES = 20
MAX_SOMMETS = 20000

SEUIL_CHEVAUCHEMENT_M2 = 1.0           # en dessous : simple imprécision de tracé
SEUIL_CHEVAUCHEMENT_PCT = 0.5          # % de la plus petite des deux parcelles


class GeoError(Exception):
    """Fichier ou géométrie refusés (message affichable à l'utilisateur)."""


@dataclass
class Resultat:
    geometrie: object           # shapely MultiPolygon, coordonnées (lon, lat)
    superficie_m2: float
    perimetre_m: float
    centre: tuple               # (lon, lat)
    bbox: tuple                 # (min_lon, min_lat, max_lon, max_lat)
    geojson: str                # JSON canonique
    empreinte: str              # SHA-256 du GeoJSON canonique
    nb_sommets: int
    elements: list = field(default_factory=list)   # repères du croquis : lignes, points, zones annexes nommés


# --------------------------------------------------------------------------
# Lecture du fichier
# --------------------------------------------------------------------------
def _extraire_kml(octets, nom):
    nom = (nom or "").lower()
    if nom.endswith(".kmz"):
        if not octets.startswith(b"PK\x03\x04"):
            raise GeoError("Ce fichier n'est pas une archive KMZ valide.")
        try:
            with zipfile.ZipFile(io.BytesIO(octets)) as z:
                infos = z.infolist()
                if len(infos) > MAX_ENTREES_ZIP or sum(i.file_size for i in infos) > MAX_TOTAL_ZIP:
                    raise GeoError("Archive KMZ trop volumineuse ou suspecte.")
                kmls = [i for i in infos if i.filename.lower().endswith(".kml") and not i.is_dir()]
                if not kmls:
                    raise GeoError("Aucun fichier .kml trouvé dans cette archive KMZ.")
                # doc.kml est la convention ; sinon le premier KML
                choisi = next((i for i in kmls if i.filename.lower().split("/")[-1] == "doc.kml"), kmls[0])
                with z.open(choisi) as f:
                    contenu = f.read(MAX_KML_OCTETS + 1)  # lecture bornée, quoi que l'en-tête annonce
        except zipfile.BadZipFile:
            raise GeoError("Archive KMZ corrompue.") from None
        if len(contenu) > MAX_KML_OCTETS:
            raise GeoError("Le KML contenu dans l'archive est trop volumineux.")
        return contenu
    if nom.endswith(".kml"):
        if len(octets) > MAX_KML_OCTETS:
            raise GeoError("Fichier KML trop volumineux.")
        return octets
    raise GeoError("Format non accepté : envoyez un fichier .kmz ou .kml.")


MAX_NOM_ELEMENT = 60
MAX_ELEMENTS = 200
MAX_SOMMETS_ELEMENTS = 3000


def _est_annexe(nom):
    """Un polygone dont le nom commence par « Annexe » est une zone du croquis (bâtiment, plantation…), pas la parcelle."""
    return nom.lower().startswith("annexe")


def _sans_prefixe_annexe(nom):
    return nom[len("annexe"):].lstrip(" :-–—") or "Zone"


def _nom_local(balise):
    return balise.rsplit("}", 1)[-1] if isinstance(balise, str) else ""


def _lire_anneau(texte, fermer=True):
    """« lon,lat[,alt] lon,lat … » -> [(lon, lat)] (altitude ignorée)."""
    points = []
    for jeton in (texte or "").split():
        morceaux = jeton.split(",")
        if len(morceaux) < 2:
            raise GeoError("Coordonnées mal formées dans le KML.")
        try:
            lon, lat = float(morceaux[0]), float(morceaux[1])
        except ValueError:
            raise GeoError("Coordonnées non numériques dans le KML.") from None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise GeoError("Coordonnées invalides dans le KML.")
        if not points or points[-1] != (lon, lat):  # supprime les doublons consécutifs
            points.append((lon, lat))
    if fermer and len(points) > 1 and points[0] != points[-1]:
        points.append(points[0])  # ferme l'anneau
    return points


def _anneaux_de(polygone_xml):
    exterieur, trous = None, []
    for el in polygone_xml.iter():
        nom = _nom_local(el.tag)
        if nom in ("outerBoundaryIs", "innerBoundaryIs"):
            coords = next((c for c in el.iter() if _nom_local(c.tag) == "coordinates"), None)
            if coords is None:
                continue
            anneau = _lire_anneau(coords.text)
            if nom == "outerBoundaryIs":
                exterieur = anneau
            else:
                trous.append(anneau)
    return exterieur, trous


def lire(octets, nom_fichier):
    """Retourne un Resultat à partir d'un KMZ/KML, ou lève GeoError."""
    kml = _extraire_kml(octets, nom_fichier)
    try:
        racine = SafeET.fromstring(kml)
    except DefusedXmlException:
        raise GeoError("Ce fichier KML contient des éléments XML dangereux : il est refusé.") from None
    except Exception:  # noqa: BLE001 — XML mal formé
        raise GeoError("Le fichier KML est illisible (XML invalide).") from None

    parents = {enfant: parent for parent in racine.iter() for enfant in parent}

    def nom_du_repere(el):
        """Nom du <Placemark> qui contient cet élément."""
        while el is not None and _nom_local(el.tag) != "Placemark":
            el = parents.get(el)
        if el is None:
            return ""
        n = next((c for c in el if _nom_local(c.tag) == "name"), None)
        return " ".join((n.text or "").split())[:MAX_NOM_ELEMENT] if n is not None else ""

    polygones, sommets, elements, points_elements = [], 0, [], 0
    for el in racine.iter():
        genre = _nom_local(el.tag)
        if genre in ("LineString", "Point") or (genre == "Polygon" and _est_annexe(nom_du_repere(el))):
            if len(elements) >= MAX_ELEMENTS:
                continue
            try:
                if genre == "Polygon":
                    pts = [list(p) for p in (_anneaux_de(el)[0] or [])]
                else:
                    coords = next((c for c in el.iter() if _nom_local(c.tag) == "coordinates"), None)
                    pts = [list(p) for p in _lire_anneau(coords.text if coords is not None else "", fermer=False)]
                    if genre == "Point":
                        pts = pts[:1]
            except GeoError:
                continue
            if not pts or (genre != "Point" and len(pts) < 2) or points_elements + len(pts) > MAX_SOMMETS_ELEMENTS:
                continue
            if not all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts):
                continue
            points_elements += len(pts)
            nom = nom_du_repere(el)
            if genre == "Polygon":
                nom = _sans_prefixe_annexe(nom)
            elements.append({"type": {"LineString": "ligne", "Point": "point", "Polygon": "zone"}[genre],
                             "nom": nom, "coords": [[round(x, 7), round(y, 7)] for x, y in pts]})
            continue
        if genre != "Polygon":
            continue
        exterieur, trous = _anneaux_de(el)
        if not exterieur or len(exterieur) < 4:
            raise GeoError("Un polygone du KML a moins de 3 sommets.")
        sommets += len(exterieur) + sum(len(t) for t in trous)
        if sommets > MAX_SOMMETS:
            raise GeoError(f"Tracé trop détaillé (plus de {MAX_SOMMETS} sommets).")
        polygones.append(Polygon(exterieur, [t for t in trous if len(t) >= 4]))
        if len(polygones) > MAX_POLYGONES:
            raise GeoError(f"Trop de polygones dans le fichier (maximum {MAX_POLYGONES}).")
    if not polygones:
        raise GeoError("Aucun polygone trouvé : le fichier doit contenir le contour de la parcelle (pas seulement des points ou des lignes).")

    for p in polygones:
        minx, miny, maxx, maxy = p.bounds
        if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
            raise GeoError("Coordonnées hors du globe : vérifiez l'ordre longitude/latitude.")
        if not (EMPRISE["lon_min"] <= minx and maxx <= EMPRISE["lon_max"] and EMPRISE["lat_min"] <= miny and maxy <= EMPRISE["lat_max"]):
            raise GeoError(
                "Ce tracé n'est pas situé aux Comores (vérifiez le fichier : longitude et latitude "
                "inversées, mauvais système de coordonnées ou mauvaise parcelle)."
            )
        if not p.is_valid:
            raise GeoError(f"Polygone invalide : {explain_validity(p)}. Corrigez le tracé dans Google Earth (lignes qui se croisent).")
        if p.area == 0:
            raise GeoError("Un polygone du KML est plat (surface nulle).")

    multi = MultiPolygon(polygones)
    if not multi.is_valid:
        raise GeoError("Les polygones du fichier se chevauchent entre eux : un même dossier ne peut pas avoir de parties superposées.")
    resultat = _resume(multi, sommets)
    resultat.elements = elements
    return resultat


# --------------------------------------------------------------------------
# Calculs
# --------------------------------------------------------------------------
def _polygones(geom):
    return list(geom.geoms) if geom.geom_type in ("MultiPolygon", "GeometryCollection") else [geom]


def aire_m2(geom):
    """Superficie géodésique (ellipsoïde WGS84), en m², trous soustraits.

    pyproj additionne les anneaux avec leur signe : un trou dessiné dans le même sens que le
    contour s'ajouterait au lieu de se soustraire. On impose donc l'orientation canonique
    (contour anti-horaire, trous horaires) avant de mesurer.
    """
    total = 0.0
    for p in _polygones(geom):
        if p.geom_type == "Polygon" and not p.is_empty:
            total += GEOD.geometry_area_perimeter(orient(p, 1.0))[0]
    return abs(total)


def perimetre_m(geom):
    return sum(GEOD.geometry_area_perimeter(orient(p, 1.0))[1] for p in _polygones(geom) if p.geom_type == "Polygon" and not p.is_empty)


def _arrondi(geom, n=7):
    """Coordonnées arrondies (~1 cm) : empreinte stable et JSON compact."""
    return shape(json.loads(json.dumps(mapping(geom), default=lambda x: x), parse_float=lambda s: round(float(s), n)))


def _resume(multi, sommets):
    from hashlib import sha256

    geom = _arrondi(multi)
    geojson = json.dumps(mapping(geom), sort_keys=True, separators=(",", ":"))
    centre = geom.representative_point() if geom.centroid.is_empty or not geom.contains(geom.centroid) else geom.centroid
    return Resultat(
        geometrie=geom,
        superficie_m2=round(aire_m2(geom), 2),
        perimetre_m=round(perimetre_m(geom), 2),
        centre=(centre.x, centre.y),
        bbox=geom.bounds,
        geojson=geojson,
        empreinte=sha256(geojson.encode()).hexdigest(),
        nb_sommets=sommets,
    )


def depuis_geojson(texte):
    return shape(json.loads(texte))


def resume_geometrie(geom):
    """Resultat complet à partir d'une géométrie shapely (Polygon ou MultiPolygon)."""
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    if geom.geom_type != "MultiPolygon" or geom.is_empty or not geom.is_valid:
        raise GeoError("Géométrie résultante invalide.")
    return _resume(geom, sum(len(p.exterior.coords) + sum(len(i.coords) for i in p.interiors) for p in geom.geoms))


TOLERANCE_CONTACT_M = 1.0  # deux parcelles « se touchent » si moins de 1 m les sépare (imprécision de tracé)


def _deg(metres):
    return metres / 111000.0


def sont_contigues(geoms):
    """Vrai si l'ensemble des parcelles forme un seul bloc (contact ou écart < 1 m)."""
    from shapely.ops import unary_union

    tampon = [g.buffer(_deg(TOLERANCE_CONTACT_M) / 2) for g in geoms]
    return unary_union(tampon).geom_type == "Polygon"


def union_parcelles(geoms):
    """Union de parcelles contiguës en un seul polygone (comble les écarts < 1 m), ou None."""
    from shapely.ops import unary_union

    if not sont_contigues(geoms):
        return None
    t = _deg(TOLERANCE_CONTACT_M)
    fusion = unary_union([g.buffer(t / 2) for g in geoms]).buffer(-t / 2)
    return fusion if fusion.geom_type == "Polygon" and fusion.is_valid else None


def aire_hors(geom, zone):
    """Superficie (m²) de `geom` qui dépasse de `zone`."""
    reste = geom.difference(zone)
    return aire_m2(reste) if not reste.is_empty else 0.0


def aire_commune(a, b):
    inter = a.intersection(b)
    return aire_m2(inter) if not inter.is_empty else 0.0


def chevauchements(geom, voisins):
    """Parcelles voisines qui empiètent réellement sur `geom`.

    `voisins` : iterable de (clé, géométrie shapely). Retourne une liste triée du plus
    important au moins important. Un simple contact de limites, ou un recouvrement
    inférieur au seuil (imprécision de tracé), n'est pas signalé.
    """
    a_ref = aire_m2(geom)
    trouves = []
    for cle, autre in voisins:
        if not geom.intersects(autre):
            continue
        inter = geom.intersection(autre)
        parts = [g for g in getattr(inter, "geoms", [inter]) if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
        aire = sum(aire_m2(g) for g in parts)
        if aire <= 0:
            continue
        petite = min(a_ref, aire_m2(autre)) or 1.0
        pct = 100.0 * aire / petite
        if aire >= SEUIL_CHEVAUCHEMENT_M2 and pct >= SEUIL_CHEVAUCHEMENT_PCT:
            trouves.append({"cle": cle, "aire_m2": round(aire, 1), "pct": round(pct, 1)})
    return sorted(trouves, key=lambda t: -t["aire_m2"])


# --------------------------------------------------------------------------
# Projection locale (dessin du croquis)
# --------------------------------------------------------------------------
def vers_metres(lon, lat, lon0, lat0):
    """Projection plane locale (équirectangulaire) : suffisante pour dessiner une parcelle."""
    return (lon - lon0) * 111320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 110574.0


# --------------------------------------------------------------------------
# Détail du contour (plan croquis) : bornes, côtés, orientations
# --------------------------------------------------------------------------
def vers_utm(lon, lat):
    """(Est, Nord) en mètres, UTM 38 Sud."""
    return UTM38S.transform(lon, lat)


def bornes(geom):
    """Détail du contour EXTÉRIEUR de chaque partie du tracé :
    [{"prefixe": "" | "A" | "B"…, "bornes": [{"n", "lon", "lat", "est", "nord"}],
      "cotes": [{"de", "vers", "longueur_m", "azimut"}], "perimetre_m"}].
    Les bornes gardent l'ordre du tracé (celui du dessin). Plusieurs parties : les bornes sont étiquetées A1, A2…, B1, B2…"""
    parties = [p for p in _polygones(geom) if p.geom_type == "Polygon" and not p.is_empty]
    sortie = []
    for k, poly in enumerate(parties):
        prefixe = chr(65 + k) if len(parties) > 1 else ""
        sommets = list(poly.exterior.coords)[:-1]
        etiquettes = [f"{prefixe}{i}" for i in range(1, len(sommets) + 1)]
        liste = []
        for etiquette, (lon, lat) in zip(etiquettes, sommets):
            est, nord = vers_utm(lon, lat)
            liste.append({"n": etiquette, "lon": lon, "lat": lat, "est": est, "nord": nord})
        cotes, total = [], 0.0
        for i, (lon1, lat1) in enumerate(sommets):
            lon2, lat2 = sommets[(i + 1) % len(sommets)]
            az, _, distance = GEOD.inv(lon1, lat1, lon2, lat2)
            total += distance
            cotes.append({"de": etiquettes[i], "vers": etiquettes[(i + 1) % len(sommets)], "longueur_m": distance, "azimut": az % 360})
        sortie.append({"prefixe": prefixe, "bornes": liste, "cotes": cotes, "perimetre_m": total})
    return sortie


COTES_CARDINAUX = ["Nord", "Nord-Est", "Est", "Sud-Est", "Sud", "Sud-Ouest", "Ouest", "Nord-Ouest"]


def cote_cardinal(dx, dy):
    """Côté (Nord, Nord-Est…) vers lequel pointe le vecteur (dx = Est, dy = Nord)."""
    azimut = math.degrees(math.atan2(dx, dy)) % 360
    return COTES_CARDINAUX[int(((azimut + 22.5) % 360) // 45)]


def voisinage(geom, autre, tolerance_m=3.0):
    """(distance en m, côté cardinal, chevauche) de `autre` par rapport à `geom`, ou None si elle est plus loin que `tolerance_m`.
    Mesures dans le plan UTM : « limitrophe » = à moins de `tolerance_m` (les tracés se touchent à l'imprécision près)."""
    from shapely.ops import nearest_points, transform

    a, b = transform(UTM38S.transform, geom), transform(UTM38S.transform, autre)
    distance = a.distance(b)
    if distance > tolerance_m:
        return None
    chevauche = a.intersection(b).area >= SEUIL_CHEVAUCHEMENT_M2
    if distance == 0:
        # les tracés se touchent : la direction est celle de leurs centres, vue depuis la partie commune
        commun = a.intersection(b)
        p1 = commun.representative_point() if not commun.is_empty else a.centroid
        p2 = b.centroid
        dx, dy = p2.x - p1.x, p2.y - p1.y
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            dx, dy = b.centroid.x - a.centroid.x, b.centroid.y - a.centroid.y
    else:
        p1, p2 = nearest_points(a, b)
        dx, dy = p2.x - p1.x, p2.y - p1.y
    return distance, cote_cardinal(dx, dy), chevauche
