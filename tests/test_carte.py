import io
import json
import math
import os
import re
import shutil
import subprocess
import zipfile
from io import BytesIO

import pytest
from pypdf import PdfReader

from app import constants as C
from app import geo
from app.extensions import db
from app.models import AuditLog, Dossier, Geometrie

from conftest import agir, creer_dossier

LON, LAT = 43.2550, -11.7000   # Moroni


# ----------------------------------------------------------------- fabrication de fichiers
def points(lon, lat, largeur_m, hauteur_m):
    dlat = hauteur_m / 110574.0
    dlon = largeur_m / (111320.0 * math.cos(math.radians(abs(lat))))
    return [(lon, lat), (lon + dlon, lat), (lon + dlon, lat - dlat), (lon, lat - dlat), (lon, lat)]


def coords_txt(pts, altitude=True):
    return " ".join(f"{x:.7f},{y:.7f}{',0' if altitude else ''}" for x, y in pts)


def kml(*polygones, trous=None, extra=""):
    """polygones : listes de (lon, lat)."""
    corps = ""
    for i, p in enumerate(polygones):
        interieur = ""
        for t in (trous or []) if i == 0 else []:
            interieur += f"<innerBoundaryIs><LinearRing><coordinates>{coords_txt(t)}</coordinates></LinearRing></innerBoundaryIs>"
        corps += (f"<Placemark><name>P{i}</name><Polygon><outerBoundaryIs><LinearRing><coordinates>{coords_txt(p)}"
                  f"</coordinates></LinearRing></outerBoundaryIs>{interieur}</Polygon></Placemark>")
    return (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>{corps}{extra}</Document></kml>').encode()


def kmz(contenu_kml, nom="doc.kml", extras=()):
    tampon = BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(nom, contenu_kml)
        for n, c in extras:
            z.writestr(n, c)
    return tampon.getvalue()


RECT = kml(points(LON, LAT, 100, 80))


# ================================================================= lecture (unitaire)
def test_kml_simple_superficie_et_perimetre():
    r = geo.lire(RECT, "parcelle.kml")
    assert r.superficie_m2 == pytest.approx(8000, rel=0.01)
    assert r.perimetre_m == pytest.approx(360, rel=0.01)
    assert len(r.empreinte) == 64 and r.nb_sommets == 5
    assert r.bbox[0] == pytest.approx(LON, abs=1e-6)
    assert json.loads(r.geojson)["type"] == "MultiPolygon"


def test_kmz_avec_autres_fichiers_et_sous_dossier():
    archive = kmz(RECT, nom="dossier/doc.kml", extras=[("files/icone.png", b"\x89PNG..."), ("lisezmoi.txt", "x")])
    assert geo.lire(archive, "p.KMZ").superficie_m2 == pytest.approx(8000, rel=0.01)


def test_kmz_sans_kml_ou_pas_une_archive():
    with pytest.raises(geo.GeoError, match="Aucun fichier .kml"):
        geo.lire(kmz(b"", nom="notes.txt"), "p.kmz")
    with pytest.raises(geo.GeoError, match="pas une archive KMZ"):
        geo.lire(b"<kml/>", "p.kmz")
    with pytest.raises(geo.GeoError, match="corrompue"):
        geo.lire(b"PK\x03\x04" + b"garbage" * 20, "p.kmz")


def test_polygone_a_trou_soustrait_le_trou():
    ext = points(LON, LAT, 100, 100)
    trou = points(LON + 0.00025, LAT - 0.00025, 50, 50)
    plein = geo.lire(kml(ext), "p.kml").superficie_m2
    perce = geo.lire(kml(ext, trous=[trou]), "p.kml").superficie_m2
    assert perce == pytest.approx(plein - 2500, rel=0.01)


def test_trou_dessine_dans_le_meme_sens_est_aussi_soustrait():
    ext = points(LON, LAT, 100, 100)
    trou = points(LON + 0.00025, LAT - 0.00025, 50, 50)
    a = geo.lire(kml(ext, trous=[trou]), "p.kml").superficie_m2
    b = geo.lire(kml(ext, trous=[trou[::-1]]), "p.kml").superficie_m2
    c = geo.lire(kml(ext[::-1], trous=[trou]), "p.kml").superficie_m2
    assert a == pytest.approx(b, rel=1e-9) == pytest.approx(c, rel=1e-9)


def test_plusieurs_polygones_et_anneau_non_ferme():
    a, b = points(LON, LAT, 50, 50), points(LON + 0.002, LAT, 50, 50)
    r = geo.lire(kml(a, b), "p.kml")
    assert r.superficie_m2 == pytest.approx(5000, rel=0.02)
    ouvert = points(LON, LAT, 100, 80)[:-1]                           # dernier point manquant
    assert geo.lire(kml(ouvert), "p.kml").superficie_m2 == pytest.approx(8000, rel=0.01)


def test_coordonnees_hors_comores_ou_inversees_refusees():
    with pytest.raises(geo.GeoError, match="pas situé aux Comores"):
        geo.lire(kml(points(2.35, 48.85, 100, 80)), "p.kml")          # Paris
    with pytest.raises(geo.GeoError, match="pas situé aux Comores"):
        geo.lire(kml(points(45.15, -12.80, 100, 80)), "p.kml")        # Mayotte
    inverse = [(lat, lon) for lon, lat in points(LON, LAT, 100, 80)]  # latitude/longitude permutées
    with pytest.raises(geo.GeoError, match="pas situé aux Comores|hors du globe"):
        geo.lire(kml(inverse), "p.kml")


def test_polygone_qui_se_croise_refuse():
    noeud = [(LON, LAT), (LON + 0.001, LAT - 0.001), (LON + 0.001, LAT), (LON, LAT - 0.001), (LON, LAT)]
    with pytest.raises(geo.GeoError, match="invalide"):
        geo.lire(kml(noeud), "p.kml")


def test_polygones_superposes_dans_un_meme_fichier_refuses():
    with pytest.raises(geo.GeoError, match="se chevauchent entre eux"):
        geo.lire(kml(points(LON, LAT, 100, 100), points(LON + 0.0003, LAT - 0.0003, 100, 100)), "p.kml")


def test_fichiers_sans_polygone_ou_degeneres():
    ligne = kml(extra="<Placemark><LineString><coordinates>43.25,-11.7,0 43.26,-11.7,0</coordinates></LineString></Placemark>")
    with pytest.raises(geo.GeoError, match="Aucun polygone"):
        geo.lire(ligne, "p.kml")
    with pytest.raises(geo.GeoError, match="moins de 3 sommets"):
        geo.lire(kml([(LON, LAT), (LON + 0.001, LAT)]), "p.kml")
    with pytest.raises(geo.GeoError, match="illisible"):
        geo.lire(b"ceci n'est pas du xml", "p.kml")
    with pytest.raises(geo.GeoError, match="non numériques|mal formées"):
        geo.lire(b'<kml xmlns="http://www.opengis.net/kml/2.2"><Polygon><outerBoundaryIs><LinearRing><coordinates>a,b c,d e,f</coordinates></LinearRing></outerBoundaryIs></Polygon></kml>', "p.kml")
    with pytest.raises(geo.GeoError, match="Format non accepté"):
        geo.lire(RECT, "p.pdf")


def test_trop_de_polygones_ou_de_sommets():
    petits = [points(LON + i * 0.001, LAT, 20, 20) for i in range(geo.MAX_POLYGONES + 1)]
    with pytest.raises(geo.GeoError, match="Trop de polygones"):
        geo.lire(kml(*petits), "p.kml")
    rond = [(LON + 0.01 * math.cos(t / 21000 * 2 * math.pi), LAT + 0.01 * math.sin(t / 21000 * 2 * math.pi)) for t in range(21000)]
    rond.append(rond[0])
    with pytest.raises(geo.GeoError, match="trop détaillé"):
        geo.lire(kml(rond), "p.kml")


# ----------------------------------------------------------------- attaques
def test_attaque_xxe_entite_externe_refusee(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("MOT-DE-PASSE-DU-SERVEUR")
    piege = (f'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY x SYSTEM "file://{secret}">]>'
             '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>&x;</name></Document></kml>').encode()
    with pytest.raises(geo.GeoError, match="dangereux") as e:
        geo.lire(piege, "p.kml")
    assert "MOT-DE-PASSE" not in str(e.value)


def test_attaque_milliard_de_rires_refusee():
    piege = (b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">'
             b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]><kml xmlns="http://www.opengis.net/kml/2.2"><Document>&c;</Document></kml>')
    with pytest.raises(geo.GeoError, match="dangereux"):
        geo.lire(piege, "p.kml")


def test_zip_bomb_refusee_sans_charger_en_memoire():
    enorme = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + b" " * (geo.MAX_KML_OCTETS + 1000) + b"</Document></kml>"
    archive = kmz(enorme)
    assert len(archive) < 100_000                                     # quelques Ko compressés
    with pytest.raises(geo.GeoError, match="trop volumineux"):
        geo.lire(archive, "p.kmz")
    with pytest.raises(geo.GeoError, match="trop volumineux"):
        geo.lire(enorme, "p.kml")


def test_archive_avec_enorme_nombre_d_entrees_refusee():
    tampon = BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("doc.kml", RECT)
        for i in range(geo.MAX_ENTREES_ZIP + 1):
            z.writestr(f"f{i}.txt", "x")
    with pytest.raises(geo.GeoError, match="suspecte"):
        geo.lire(tampon.getvalue(), "p.kmz")


# ----------------------------------------------------------------- chevauchements (unitaire)
def poly(lon, lat, w, h):
    return geo.lire(kml(points(lon, lat, w, h)), "p.kml").geometrie


def test_chevauchements_detectes_ou_non():
    base = poly(LON, LAT, 100, 100)
    decale = poly(LON + 50 / (111320 * math.cos(math.radians(LAT))), LAT, 100, 100)        # moitié commune
    contigu = poly(LON + 100 / (111320 * math.cos(math.radians(LAT))), LAT, 100, 100)      # partage une limite
    loin = poly(LON + 0.01, LAT, 100, 100)
    dedans = poly(LON + 0.0002, LAT - 0.0002, 30, 30)
    effleure = poly(LON + 99.8 / (111320 * math.cos(math.radians(LAT))), LAT, 100, 100)    # 20 cm d'empiètement

    trouves = geo.chevauchements(base, [("decale", decale), ("contigu", contigu), ("loin", loin), ("dedans", dedans), ("effleure", effleure)])
    cles = {t["cle"]: t for t in trouves}
    assert set(cles) == {"decale", "dedans"}
    assert cles["decale"]["pct"] == pytest.approx(50, abs=2) and cles["decale"]["aire_m2"] == pytest.approx(5000, rel=0.03)
    assert cles["dedans"]["pct"] == pytest.approx(100, abs=1)                # contenue : 100 % de la plus petite
    assert [t["cle"] for t in trouves] == ["decale", "dedans"]               # triés par gravité


# ================================================================= intégration
def televerser(client, did, contenu=None, nom="parcelle.kml", **champs):
    contenu = RECT if contenu is None else contenu
    data = {"fichier": (io.BytesIO(contenu), nom), **champs}
    return client.post(f"/dossiers/{did}/trace", data=data, content_type="multipart/form-data", follow_redirects=True)


def geometries(app, did):
    with app.app_context():
        return [(g.version, round(g.superficie_m2, -2), g.remplace_at is not None, g.motif_remplacement)
                for g in Geometrie.query.filter_by(dossier_id=did).order_by(Geometrie.version)]


def test_envoi_du_trace_stockage_page_et_telechargement(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    r = televerser(pub, did, kmz(RECT), "bornage.kmz")
    page = r.data.decode()
    assert "Tracé enregistré" in page and "Superficie mesurée" in page and 'id="carte-dossier"' in page
    assert "Aucun chevauchement" in page and "Fichier d'origine (KMZ)" in page.replace("&#39;", "'")
    assert geometries(app, did) == [(1, 8000, False, None)]

    with app.app_context():
        g = Geometrie.query.one()
        assert g.mime == "application/vnd.google-earth.kmz" and len(g.sha256) == 64 and g.nom_original == "bornage.kmz"
        assert os.path.exists(os.path.join(app.config["UPLOAD_DIR"], g.cle))
        assert AuditLog.query.filter_by(action="trace_ajoute").count() == 1
    dl = pub.get(f"/dossiers/{did}/trace/1/telecharger")
    assert dl.status_code == 200 and zipfile.ZipFile(BytesIO(dl.data)).namelist() == ["doc.kml"]
    assert "attachment" in dl.headers["Content-Disposition"]
    assert pub.get(f"/dossiers/{did}/trace/9/telecharger").status_code == 404


def test_fichier_invalide_ne_cree_rien_et_ne_stocke_rien(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    for contenu, nom in [(b"pas du xml", "a.kml"), (kml(points(2.35, 48.85, 100, 80)), "paris.kml"), (RECT, "a.pdf"), (b"", "v.kml")]:
        r = televerser(pub, did, contenu, nom)
        assert "alert-danger" in r.data.decode(), nom
    assert geometries(app, did) == []
    assert not os.path.exists(os.path.join(app.config["UPLOAD_DIR"], "dossiers"))


def test_remplacement_versionne_avec_motif(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    grand = kml(points(LON, LAT, 200, 80))
    assert "motif du remplacement est obligatoire" in televerser(pub, did, grand, "v2.kml").data.decode()
    assert len(geometries(app, did)) == 1

    televerser(pub, did, grand, "v2.kml", motif="Bornage rectifié")
    assert geometries(app, did) == [(1, 8000, True, None), (2, 16000, False, "Bornage rectifié")]
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "Historique des tracés (2 versions)" in page and "Bornage rectifié" in page
    with app.app_context():
        assert db.session.get(Dossier, did).geometrie_courante.version == 2
        assert AuditLog.query.filter_by(action="trace_remplace").count() == 1
    assert pub.get(f"/dossiers/{did}/trace/1/telecharger").status_code == 200   # l'ancien tracé reste récupérable


def test_droits_sur_le_trace(app, login):
    pub, p1, p2, dom, dire = login("pub"), login("priv1"), login("priv2"), login("dom"), login("dir")
    d_pub = creer_dossier(pub)
    d_priv = creer_dossier(p1)

    assert televerser(p2, d_priv).status_code == 404                              # autre topo privé
    assert televerser(p1, d_pub).status_code == 404                               # pas son dossier
    assert geometries(app, d_priv) == []
    assert dom.post(f"/dossiers/{d_pub}/trace", data={"fichier": (io.BytesIO(RECT), "a.kml")}, content_type="multipart/form-data").status_code == 403
    assert app.test_client().post(f"/dossiers/{d_pub}/trace").status_code == 302

    televerser(p1, d_priv)                                                         # son dossier, avant l'avis
    assert len(geometries(app, d_priv)) == 1
    agir(p1, d_priv, "terminer_plan")
    agir(p1, d_priv, "envoyer_avis", commentaire="OK")
    assert p1.post(f"/dossiers/{d_priv}/trace", data={"fichier": (io.BytesIO(RECT), "a.kml"), "motif": "x"}, content_type="multipart/form-data").status_code == 403
    assert televerser(dire, d_priv, motif="Correction", contenu=kml(points(LON, LAT, 90, 90))).status_code == 200
    assert len(geometries(app, d_priv)) == 2


def test_domaine_peut_geolocaliser_un_ancien_dossier(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"],
        "libelle_document": [""], "fichiers": (io.BytesIO(b"%PDF-1.4 x"), "t.pdf")})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    televerser(dom, did)
    assert len(geometries(app, did)) == 1


def test_chevauchement_signale_et_confidentialite(app, login):
    pub, p1, dire = login("pub"), login("priv1"), login("dir")
    d_pub = creer_dossier(pub)
    televerser(pub, d_pub)                                                         # parcelle de référence
    with app.app_context():
        numero_pub = db.session.get(Dossier, d_pub).numero

    d_priv = creer_dossier(p1)
    r = televerser(p1, d_priv, kml(points(LON + 0.0004, LAT, 100, 80)))           # empiète sur la première
    page = r.data.decode()
    assert "chevauche 1 autre(s) parcelle(s)" in page and "Chevauchement détecté" in page
    assert numero_pub not in page and "une parcelle d'un autre dossier" in page   # le topo privé ne voit pas l'autre dossier

    vue_dir = dire.get(f"/dossiers/{d_priv}").data.decode()
    assert numero_pub in vue_dir and f"/dossiers/{d_pub}" in vue_dir                 # le Directeur, si
    assert "Chevauchement détecté" in pub.get(f"/dossiers/{d_pub}").data.decode()    # et c'est réciproque


def test_ecart_de_superficie_signale(app, login):
    pub = login("pub")
    r = pub.post("/dossiers/nouveau", data={"proprio_nom": "Said", "ile_id": "1", "commune": "Moroni", "superficie": "5000"})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = televerser(pub, did).data.decode()                                       # tracé mesuré : 8000 m²
    assert "diffère de la superficie déclarée" in page and "+60.1 %" in page


def test_carte_generale_donnees_et_filtres(app, login):
    pub, p1, dire = login("pub"), login("priv1"), login("dir")
    d1 = creer_dossier(pub)
    televerser(pub, d1)
    d2 = creer_dossier(p1)
    televerser(p1, d2, kml(points(LON + 0.01, LAT, 60, 60)))
    creer_dossier(pub)                                                              # sans tracé : absent de la carte

    data = dire.get("/carte/donnees.json").get_json()
    assert data["total"] == 2 and len(data["features"]) == 2 and data["tronque"] is False
    assert {f["properties"]["numero"][:5] for f in data["features"]} == {"SIGF-"}
    assert dire.get("/carte/donnees.json?ile=2").get_json()["total"] == 0           # filtre par île
    assert dire.get("/carte/donnees.json").headers["Cache-Control"] == "no-store"

    privee = p1.get("/carte/donnees.json").get_json()                               # périmètre du topo privé
    assert privee["total"] == 1 and privee["features"][0]["properties"]["proprietaire"] is None
    assert app.test_client().get("/carte/donnees.json").status_code == 302
    assert login("dom").get("/carte").status_code == 200


def test_plan_croquis_dessine_le_trace_a_l_echelle(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")

    def texte(rep):
        return "\n".join(p.extract_text() for p in PdfReader(BytesIO(rep.data)).pages)

    sans = texte(dire.get(f"/dossiers/{did}/imprimer/plan_croquis"))
    assert "Échelle : 1/ ________" in sans and "Superficie mesurée" not in sans

    televerser(pub, did, kml(points(LON, LAT, 100, 70)))                            # 100 m x 70 m
    reponse = dire.get(f"/dossiers/{did}/imprimer/plan_croquis")
    avec = texte(reponse)
    assert "Échelle : 1/1000" in avec and "échelle 1/1000" in avec
    assert re.search(r"Superficie mesurée : 7 0\d\d m²", re.sub(r"[\u202f\xa0]", " ", avec))
    assert len(reponse.data) > len(dire.get(f"/dossiers/{did}/imprimer/attestation").data) * 0.3


def test_echelle_normalisee_et_trace_toujours_dans_le_cadre():
    from app import pdf as Pdf

    class G:  # tracé factice
        def __init__(self, texte):
            self.geojson = texte

    for largeur_m, hauteur_m in [(10, 10), (100, 80), (400, 60), (30, 900), (2500, 2000)]:
        r = geo.lire(kml(points(LON, LAT, largeur_m, hauteur_m)), "p.kml")
        plan = Pdf.mise_en_page_croquis(G(r.geojson), 17 * 28.3465, 7.5 * 28.3465)
        assert plan["n"] in Pdf.ECHELLES
        zone_l = 17 * 28.3465 - 2 * Pdf.MARGE_CROQUIS
        zone_h = 7.5 * 28.3465 - 2 * Pdf.MARGE_CROQUIS - Pdf.BANDE_BAS
        assert largeur_m * plan["pts_par_m"] <= zone_l + 1 and hauteur_m * plan["pts_par_m"] <= zone_h + 1, (largeur_m, hauteur_m)


def test_changer_le_trace_rend_le_plan_imprime_non_a_jour(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")
    agir(dire, did, "valider")
    televerser(pub, did)

    dire.get(f"/dossiers/{did}/imprimer/plan_croquis")
    dire.get(f"/dossiers/{did}/imprimer/certificat")
    with app.app_context():
        from app.models import DocumentEmis
        code_plan = DocumentEmis.query.filter_by(type="plan_croquis").one().code
        code_cert = DocumentEmis.query.filter_by(type="certificat").one().code
    anonyme = app.test_client()
    page = anonyme.get(f"/verifier/{code_plan}").data.decode()
    assert "Document authentique" in page and re.search(r"8 00\d m²", re.sub(r"[\u202f\xa0]", " ", page))

    televerser(pub, did, kml(points(LON, LAT, 120, 80)), "v2.kml", motif="Rectification")
    assert "non à jour" in anonyme.get(f"/verifier/{code_plan}").data.decode()
    assert "Document authentique" in anonyme.get(f"/verifier/{code_cert}").data.decode()   # le certificat n'affiche pas le tracé


def test_javascript_des_cartes_est_syntaxiquement_valide(app, login, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node absent")
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    pages = [pub.get(f"/dossiers/{did}").data.decode(), pub.get("/carte").data.decode()]
    n = 0
    for page in pages:
        for js in re.findall(r"<script>(.*?)</script>", page, re.S):
            fichier = tmp_path / f"s{n}.js"
            fichier.write_text(js)
            res = subprocess.run(["node", "--check", str(fichier)], capture_output=True, text=True)
            assert res.returncode == 0, res.stderr
            n += 1
    assert n >= 2


def test_la_carte_n_exige_pas_d_images_satellite_au_dela_de_leur_disponibilite(app, login):
    """Esri n'a pas d'imagerie aux zooms 18-19 sur les Comores (il renvoie « Map data not yet available »)."""
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    page = pub.get(f"/dossiers/{did}").data.decode()
    outils = pub.get("/static/js/carte_outils.js").data.decode()                      # code des fonds commun aux deux cartes
    assert '"zoom_natif": 17' in page and "maxNativeZoom: tuiles.satellite.zoom_natif" in outils and "SIGFCarte.fonds(carte, data.tuiles)" in page
    carte = pub.get("/carte").data.decode()
    assert '"zoom_natif": 17' in carte and "SIGFCarte.fonds(carte, cfg.tuiles)" in pub.get("/static/js/carte.js").data.decode()
    app.config["TUILES_SATELLITE_ZOOM_NATIF"] = 16                       # réglable sans toucher au code
    assert '"zoom_natif": 16' in pub.get("/carte").data.decode()


def test_liens_vers_google_centres_sur_la_parcelle(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    assert "google.com/maps" not in pub.get(f"/dossiers/{did}").data.decode()        # sans tracé, pas de lien
    televerser(pub, did)
    page = pub.get(f"/dossiers/{did}").data.decode()
    with app.app_context():
        g = Geometrie.query.one()
        lat, lon = f"{g.centre_lat:.6f}", f"{g.centre_lon:.6f}"                      # précision ~10 cm, pas 15 décimales
    assert f"center={lat},{lon}&amp;zoom=20&amp;basemap=satellite" in page
    assert f"earth.google.com/web/@{lat},{lon}," in page
    assert page.count('rel="noopener noreferrer"') >= 2                               # pas de fuite de l'adresse du site


def test_google_satellite_active_seulement_avec_une_cle(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    for url in (f"/dossiers/{did}", "/carte"):
        sans = pub.get(url).data.decode()
        assert "maps.googleapis.com" not in sans and "GoogleMutant" not in sans       # par défaut : rien de Google, rien à payer
    app.config["GOOGLE_MAPS_API_KEY"] = "CLE-DE-TEST-123"
    for url in (f"/dossiers/{did}", "/carte"):
        avec = pub.get(url).data.decode()
        assert "maps.googleapis.com/maps/api/js?key=CLE-DE-TEST-123" in avec
        assert avec.index("leaflet.min.js") < avec.index("Leaflet.GoogleMutant.js")   # le plugin se charge après Leaflet
        assert "carte_outils.js" in avec
        avec += pub.get("/static/js/carte_outils.js").data.decode()                          # le code des fonds est un fichier à part
        assert "googleMutant({type: 'satellite'" in avec and "Satellite Esri" in avec    # Esri reste disponible en secours


def test_javascript_valide_avec_la_cle_google(app, login, tmp_path):
    if not shutil.which("node"):
        pytest.skip("node absent")
    app.config["GOOGLE_MAPS_API_KEY"] = "CLE-DE-TEST-123"
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    n = 0
    for page in (pub.get(f"/dossiers/{did}").data.decode(), pub.get("/carte").data.decode()):
        for js in re.findall(r"<script>(.*?)</script>", page, re.S):
            f = tmp_path / f"g{n}.js"
            f.write_text(js)
            assert subprocess.run(["node", "--check", str(f)], capture_output=True, text=True).returncode == 0
            n += 1
    assert n >= 2


def test_fichier_env_local_est_lu_sans_ecraser_l_environnement(tmp_path, monkeypatch):
    import config

    env = tmp_path / ".env"
    env.write_text("# commentaire\nGOOGLE_TEST_A=depuis_le_fichier\nGOOGLE_TEST_B=\"entre guillemets\"\nGOOGLE_TEST_C=du_fichier\n")
    monkeypatch.setenv("GOOGLE_TEST_C", "de_l_environnement")
    for k in ("GOOGLE_TEST_A", "GOOGLE_TEST_B"):
        monkeypatch.delenv(k, raising=False)
    config._charger_env(str(env))
    assert os.environ["GOOGLE_TEST_A"] == "depuis_le_fichier" and os.environ["GOOGLE_TEST_B"] == "entre guillemets"
    assert os.environ["GOOGLE_TEST_C"] == "de_l_environnement"                          # la vraie variable gagne
    for k in ("GOOGLE_TEST_A", "GOOGLE_TEST_B"):
        os.environ.pop(k, None)


# ----------------------------------------------------------------- export pour Google Earth
def lire_kml_google_earth(reponse):
    import defusedxml.ElementTree as ET

    return ET.fromstring(reponse.data)


def test_export_google_earth_contour_style_et_camera(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    assert pub.get(f"/dossiers/{did}/trace/google-earth.kml").status_code == 404           # pas de tracé : rien à exporter
    televerser(pub, did)
    r = pub.get(f"/dossiers/{did}/trace/google-earth.kml")
    assert r.status_code == 200 and r.mimetype == "application/vnd.google-earth.kml+xml"
    with app.app_context():
        numero = db.session.get(Dossier, did).numero
        g = Geometrie.query.one()
        aire, lon, lat = g.superficie_m2, g.centre_lon, g.centre_lat
    assert r.headers["Content-Disposition"] == f'attachment; filename="{numero}.kml"'
    texte = r.data.decode()
    ns = "{http://www.opengis.net/kml/2.2}"
    racine = lire_kml_google_earth(r)                                                          # XML bien formé
    assert racine.find(f".//{ns}Placemark/{ns}name").text == numero
    assert racine.find(f".//{ns}LineStyle/{ns}color").text == "ff00ffff"                       # contour jaune opaque
    assert float(racine.find(f".//{ns}LookAt/{ns}longitude").text) == pytest.approx(lon, abs=1e-6)
    assert float(racine.find(f".//{ns}LookAt/{ns}latitude").text) == pytest.approx(lat, abs=1e-6)
    assert float(racine.find(f".//{ns}LookAt/{ns}range").text) >= 80                           # caméra cadrée sur la parcelle
    assert "clampToGround" in texte and "Superficie mesurée" in texte
    # le fichier relu par notre propre lecteur redonne exactement le même tracé
    relu = geo.lire(r.data, "export.kml")
    assert relu.superficie_m2 == pytest.approx(aire, rel=1e-6)
    with app.app_context():
        assert AuditLog.query.filter_by(action="trace_export_google_earth").count() == 1


def test_export_google_earth_trou_multi_polygone_et_texte_hostile(app, login):
    pub = login("pub")
    r = pub.post("/dossiers/nouveau", data={"proprio_nom": "Said", "ile_id": "1", "commune": "Moroni <script>&\"'", "village": "]]> <b>x</b>"})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    ext = points(LON, LAT, 100, 100)
    trou = points(LON + 0.00025, LAT - 0.00025, 50, 50)
    autre = points(LON + 0.003, LAT, 40, 40)
    televerser(pub, did, kml(ext, autre, trous=[trou]))
    r = pub.get(f"/dossiers/{did}/trace/google-earth.kml")
    racine = lire_kml_google_earth(r)                                                          # malgré < & " ' ]]> dans les champs
    assert "<script>" not in r.data.decode() and "<b>x</b>" not in r.data.decode()
    assert racine.find(".//{http://www.opengis.net/kml/2.2}MultiGeometry") is not None
    assert "innerBoundaryIs" in r.data.decode()
    assert geo.lire(r.data, "x.kml").superficie_m2 == pytest.approx(7500 + 1600, rel=0.03)    # trou soustrait, 2 parties


def test_export_google_earth_droits_et_donnees_minimales(app, login):
    pub, p1, p2 = login("pub"), login("priv1"), login("priv2")
    d_priv = creer_dossier(p1)
    televerser(p1, d_priv)
    assert p1.get(f"/dossiers/{d_priv}/trace/google-earth.kml").status_code == 200            # son propre dossier
    assert p2.get(f"/dossiers/{d_priv}/trace/google-earth.kml").status_code == 404            # celui d'un autre
    assert app.test_client().get(f"/dossiers/{d_priv}/trace/google-earth.kml").status_code == 302
    contenu = p1.get(f"/dossiers/{d_priv}/trace/google-earth.kml").data.decode()
    for prive in ("Said", "Ali", "RTC", "RTS", "CNI"):                                         # ni propriétaire ni numéros dans le fichier
        assert prive not in contenu
    page = pub.get(f"/dossiers/{creer_dossier(pub)}").data.decode()
    assert "google-earth.kml" not in page                                                      # pas de bouton sans tracé


def test_bouton_google_earth_avec_le_trace_sur_la_fiche(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert f"/dossiers/{did}/trace/google-earth.kml" in page and "Ouvrir dans Google Earth avec le tracé" in page
    assert "Fichier d\'origine" in page.replace("&#39;", "\'") and "(position)" in page


def test_carte_generale_proprietes_pour_filtres_et_panneau(app, login):
    pub, p1, dire = login("pub"), login("priv1"), login("dir")
    d1 = creer_dossier(pub)
    televerser(pub, d1)
    d2 = creer_dossier(p1)
    televerser(p1, d2, kml(points(LON + 0.01, LAT, 60, 60)))
    f = {x["properties"]["numero"]: x["properties"] for x in dire.get("/carte/donnees.json").get_json()["features"]}
    p = next(iter(f.values()))
    assert p["statut_code"] == C.PLAN_CROQUIS and p["origine"] == "nouveau" and p["commune"] == "Moroni" and p["ile"]
    assert p["google_earth"].startswith("/") and p["proprietaire"] == "Said Ali"
    # le topo privé ne reçoit toujours pas le propriétaire
    assert p1.get("/carte/donnees.json").get_json()["features"][0]["properties"]["proprietaire"] is None


def test_page_carte_legende_filtres_et_script(app, login):
    page = login("dir").get("/carte").data.decode()
    for statut in C.STATUTS:
        if statut != C.CLOTURE:
            assert f'data-statut="{statut}"' in page
    assert 'id="recherche"' in page and 'id="f-commune"' in page and 'id="detail-corps"' in page
    assert "js/carte.js" in page and "css/carte.css" in page
    assert "#eb6834" in page                                       # anciens dossiers : orange, plus blanc
    js = login("dir").get("/static/js/carte.js")
    assert js.status_code == 200 and b"innerHTML" not in js.data   # aucune injection de HTML : textContent seulement


def test_zoom_limite_a_19_sauf_avec_google(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    js = pub.get("/static/js/carte.js").data.decode()
    assert "L.map('carte-generale', {maxZoom: 19" in js
    assert "setMaxZoom(21)" in pub.get("/static/js/carte_outils.js").data.decode()        # Google seul va plus loin
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "L.map('carte-dossier', {scrollWheelZoom: false, maxZoom: 19})" in page


def test_fiche_dossier_meme_style_de_carte_que_la_carte_generale(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did)
    page = pub.get(f"/dossiers/{did}").data.decode()
    for attendu in ("css/carte.css", "js/carte_outils.js", "SIGFCarte.outils(carte", 'id="mesure-dossier"', 'id="zone-carte-dossier"', "pastille-carte"):
        assert attendu in page
    assert "#2a78d6" in page and "Plan croquis en cours" in page                      # couleur et libellé du statut, comme sur la carte
    outils = pub.get("/static/js/carte_outils.js").data.decode()
    for outil in ("Mesurer", "Ma position", "Plein écran", "L.control.scale"):
        assert outil in outils
    assert "innerHTML" not in outils
