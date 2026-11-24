"""Plan croquis : bornes et côtés (calcul), parcelles voisines, description du terrain saisie par le topographe."""

import math
import re
from datetime import date, timedelta
from io import BytesIO

from pypdf import PdfReader

from app import carte, geo
from app.extensions import db
from app.models import AuditLog, Dossier

from conftest import agir, creer_dossier
from test_carte import LAT, LON, kml, points, televerser


def dlon(metres):
    return metres / (111320.0 * math.cos(math.radians(abs(LAT))))


def texte_pdf(reponse):
    brut = "\n".join(p.extract_text() for p in PdfReader(BytesIO(reponse.data)).pages)
    return re.sub(r"[ \xa0]", " ", brut)


# ------------------------------------------------------------------ calculs (unitaire)
def test_bornes_cotes_azimuts_et_perimetre():
    g = geo.lire(kml(points(LON, LAT, 40, 30)), "p.kml")
    parties = geo.bornes(geo.depuis_geojson(g.geojson))
    assert len(parties) == 1
    p = parties[0]
    assert [b["n"] for b in p["bornes"]] == ["1", "2", "3", "4"]
    assert all(300000 < b["est"] < 400000 and 8_000_000 < b["nord"] < 9_000_000 for b in p["bornes"])   # UTM 38 Sud
    longueurs = [round(c["longueur_m"]) for c in p["cotes"]]
    assert longueurs == [40, 30, 40, 30]
    assert [round(c["azimut"]) % 360 for c in p["cotes"]] == [90, 180, 270, 0]
    assert abs(p["perimetre_m"] - sum(c["longueur_m"] for c in p["cotes"])) < 0.01
    assert [(c["de"], c["vers"]) for c in p["cotes"]][-1] == ("4", "1")


def test_bornes_plusieurs_parties_etiquetees():
    g = geo.lire(kml(points(LON, LAT, 40, 30), points(LON + 0.01, LAT, 20, 20)), "p.kml")
    parties = geo.bornes(geo.depuis_geojson(g.geojson))
    assert [p["prefixe"] for p in parties] == ["A", "B"]
    assert [b["n"] for b in parties[0]["bornes"]][0] == "A1" and [b["n"] for b in parties[1]["bornes"]][0] == "B1"


# ------------------------------------------------------------------ parcelles voisines
def _dossier_trace(client, contenu):
    did = creer_dossier(client)
    televerser(client, did, contenu)
    return did


def test_voisins_detectes_cote_distance_et_exclusions(app, login):
    pub, dire = login("pub"), login("dir")
    centre = _dossier_trace(pub, kml(points(LON, LAT, 100, 80)))
    est = _dossier_trace(pub, kml(points(LON + dlon(101), LAT, 100, 80)))          # à 1 m, à l'est
    loin = _dossier_trace(pub, kml(points(LON + dlon(500), LAT, 100, 80)))         # trop loin
    with app.app_context():
        from app.models import Geometrie

        g = Geometrie.query.filter_by(dossier_id=centre, remplace_at=None).one()
        voisins = carte.voisins_detectes(g)
        numeros = {v["dossier"].id: v for v in voisins}
        assert est in numeros and loin not in numeros and centre not in numeros
        assert numeros[est]["cote"] == "Est" and numeros[est]["distance_m"] < 3 and not numeros[est]["chevauche"]
        db.session.get(Dossier, est).statut = "cloture"
        db.session.commit()
        assert est not in {v["dossier"].id for v in carte.voisins_detectes(g)}        # un dossier clôturé n'est plus un voisin


def test_voisin_qui_chevauche_est_signale(app, login):
    pub = login("pub")
    a = _dossier_trace(pub, kml(points(LON, LAT, 100, 80)))
    b = _dossier_trace(pub, kml(points(LON + dlon(50), LAT, 100, 80)))
    with app.app_context():
        from app.models import Geometrie

        g = Geometrie.query.filter_by(dossier_id=a, remplace_at=None).one()
        v = {x["dossier"].id: x for x in carte.voisins_detectes(g)}
        assert v[b]["chevauche"] is True


# ------------------------------------------------------------------ PDF
def _certifier(pub, dom, dire, did):
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")
    agir(dire, did, "valider")


def _pret(pub, dom, did):
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")


def test_pdf_contient_bornes_limitrophes_et_voisins(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    with app.app_context():
        from app.models import User

        User.query.filter_by(username="pub").one().agrement = "AG-77"
        db.session.commit()
    did = _dossier_trace(pub, kml(points(LON, LAT, 100, 80)))
    _pret(pub, dom, did)
    _dossier_trace(pub, kml(points(LON + dlon(101), LAT, 100, 80)))
    r = pub.post(f"/dossiers/{did}/description-terrain", data={
        "limitrophe_nord": "Route nationale", "limitrophe_est": "Ravin", "nature_terrain": "Terrain nu",
        "date_leve": "2026-01-15", "mode_leve": "GPS", "observations_plan": "Borne 2 à replacer"})
    assert r.status_code == 302
    texte = texte_pdf(dire.get(f"/dossiers/{did}/imprimer/plan_croquis"))
    assert "Bornes, côtés et orientations" not in texte and "UTM fuseau" not in texte               # pas de tableau des bornes sur le plan
    assert "Périmètre (tracé)" in texte and "<b>" not in texte
    assert "Route nationale" in texte and "Ravin" in texte and "Terrain nu" in texte
    assert "15/01/2026" in texte and "GPS" in texte and "Borne 2 à replacer" in texte
    assert "Topographe" in texte and "pub" in texte and "AG-77" in texte                # pris dans le compte du topographe
    assert "Parcelles voisines enregistrées" in texte and "Est" in texte


def test_pdf_sans_voisin_ni_trace(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    seul = _dossier_trace(pub, kml(points(LON, LAT, 50, 50)))
    _pret(pub, dom, seul)
    assert "Aucune parcelle voisine enregistrée" in texte_pdf(dire.get(f"/dossiers/{seul}/imprimer/plan_croquis"))
    sans = creer_dossier(pub)
    _pret(pub, dom, sans)
    rep = dire.get(f"/dossiers/{sans}/imprimer/plan_croquis")
    assert rep.status_code == 200
    assert "Bornes, côtés" not in texte_pdf(rep) and "Limitrophes" in texte_pdf(rep)


def test_pdf_contour_tres_detaille_sans_tableau_des_bornes(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    n = 80
    pts = [(LON + dlon(30) * math.cos(2 * math.pi * i / n), LAT + 30 / 110574.0 * math.sin(2 * math.pi * i / n)) for i in range(n)]
    pts.append(pts[0])
    did = _dossier_trace(pub, kml(pts))
    _pret(pub, dom, did)
    rep = dire.get(f"/dossiers/{did}/imprimer/plan_croquis")
    texte = texte_pdf(rep)
    assert rep.status_code == 200 and "Bornes, côtés" not in texte and "premières bornes" not in texte and len(PdfReader(BytesIO(rep.data)).pages) == 1


def test_le_croquis_occupe_toute_la_hauteur_de_la_colonne(app, login):
    """Sans tableau des bornes, le dessin prend toute la hauteur disponible (environ 23 cm sur la feuille A3)."""
    from app import pdf as Pdf

    pub = login("pub")
    did = _dossier_trace(pub, kml(points(LON, LAT, 60, 40)))
    with app.app_context():
        from reportlab.lib.units import cm

        d = db.session.get(Dossier, did)
        corps = Pdf._plan_croquis(d)[-1]                                          # tableau à deux colonnes + écart
        gauche = corps._cellvalues[0][0]                                          # KeepInFrame de gauche
        cadre = gauche._content[0]
        assert cadre.height > 22 * cm and cadre.width == Pdf.LARGEUR_GAUCHE


# ------------------------------------------------------------------ formulaire : droits, validation, audit
def _poster(client, did, **champs):
    return client.post(f"/dossiers/{did}/description-terrain", data=champs, follow_redirects=True)


def test_saisie_enregistree_et_auditee(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    page = _poster(pub, did, limitrophe_sud="Mer", nature_terrain="Terrain bâti", mode_leve="Station totale").data.decode()
    assert "Description du terrain enregistrée" in page
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert d.limitrophe_sud == "Mer" and d.nature_terrain == "Terrain bâti" and d.limitrophe_nord is None
        log = AuditLog.query.filter_by(action="description_terrain_modifiee").one()
        assert "Mer" in log.nouveau and "Limitrophe sud : —" in log.ancien
    assert "Aucune modification" in _poster(pub, did, limitrophe_sud="Mer", nature_terrain="Terrain bâti", mode_leve="Station totale").data.decode()


def test_saisie_refusee_si_invalide(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    assert "Nature du terrain inconnue" in _poster(pub, did, nature_terrain="Volcan").data.decode()
    assert "Mode de levé inconnu" in _poster(pub, did, mode_leve="Boussole").data.decode()
    assert "dans le futur" in _poster(pub, did, date_leve=(date.today() + timedelta(days=2)).isoformat()).data.decode()
    assert "date du levé est invalide" in _poster(pub, did, date_leve="31/02/2026").data.decode()
    assert "trop long" in _poster(pub, did, limitrophe_nord="x" * 201).data.decode()
    with app.app_context():
        assert db.session.get(Dossier, did).nature_terrain is None


def test_droits_sur_la_description(app, login):
    pub, dom, dire, p1, p2 = login("pub"), login("dom"), login("dir"), login("priv1"), login("priv2")
    did = creer_dossier(p1)
    assert p2.post(f"/dossiers/{did}/description-terrain", data={"limitrophe_nord": "x"}).status_code == 404   # pas son dossier
    assert p1.post(f"/dossiers/{did}/description-terrain", data={"limitrophe_nord": "Mer"}).status_code == 302
    assert dom.post(f"/dossiers/{did}/description-terrain", data={"limitrophe_nord": "Route"}).status_code == 302
    assert "Description du terrain (plan croquis)" in p1.get(f"/dossiers/{did}").data.decode()
    # après la certification : seuls Directeur / Admin
    d2 = creer_dossier(pub)
    _certifier(pub, dom, dire, d2)
    assert pub.post(f"/dossiers/{d2}/description-terrain", data={"limitrophe_nord": "x"}).status_code == 403
    assert dom.post(f"/dossiers/{d2}/description-terrain", data={"limitrophe_nord": "x"}).status_code == 403
    assert dire.post(f"/dossiers/{d2}/description-terrain", data={"limitrophe_nord": "x"}).status_code == 302


def test_topo_prive_ne_peut_plus_apres_l_avis(app, login):
    p1 = login("priv1")
    did = creer_dossier(p1)
    with app.app_context():
        from datetime import datetime

        db.session.get(Dossier, did).avis_envoye_at = datetime.utcnow()
        db.session.commit()
    assert p1.post(f"/dossiers/{did}/description-terrain", data={"limitrophe_nord": "x"}).status_code == 403


def test_description_modifiee_apres_impression_rend_le_plan_non_a_jour(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = _dossier_trace(pub, kml(points(LON, LAT, 40, 30)))
    _certifier(pub, dom, dire, did)
    dire.get(f"/dossiers/{did}/imprimer/plan_croquis")
    with app.app_context():
        from app import verification as V
        from app.models import DocumentEmis

        rec = DocumentEmis.query.filter_by(dossier_id=did, type="plan_croquis").one()
        assert V.evaluer(rec) == V.VALIDE
    _poster(dire, did, limitrophe_nord="Nouvelle route")
    with app.app_context():
        rec = DocumentEmis.query.filter_by(dossier_id=did, type="plan_croquis").one()
        assert V.evaluer(rec) == V.OBSOLETE


def test_plan_sans_description_garde_ses_donnees_d_origine(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    with app.app_context():
        from app import verification as V

        assert "description" not in V.donnees(db.session.get(Dossier, did), "plan_croquis")


# ------------------------------------------------------------------ repères du croquis (lignes, points, zones annexes)
def _placemark(nom, geometrie):
    return f"<Placemark><name>{nom}</name>{geometrie}</Placemark>"


def _ligne(nom, pts):
    return _placemark(nom, f"<LineString><coordinates>{' '.join(f'{x:.7f},{y:.7f},0' for x, y in pts)}</coordinates></LineString>")


def _point(nom, x, y):
    return _placemark(nom, f"<Point><coordinates>{x:.7f},{y:.7f},0</coordinates></Point>")


def _kml_reperes():
    parcelle = points(LON, LAT, 60, 40)
    route = [(LON - dlon(10), LAT + 20 / 110574.0), (LON + dlon(80), LAT + 20 / 110574.0)]
    zone = points(LON + dlon(10), LAT - 5 / 110574.0, 15, 10)
    extra = (_ligne("Route nationale", route) + _point("Tourelle", LON + dlon(30), LAT + 12 / 110574.0)
             + "<Placemark><name>Annexe : Maison</name><Polygon><outerBoundaryIs><LinearRing><coordinates>"
             + " ".join(f"{x:.7f},{y:.7f},0" for x, y in zone) + "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>")
    return kml(parcelle, extra=extra)


def test_lecture_des_reperes_sans_toucher_a_la_parcelle():
    simple = geo.lire(kml(points(LON, LAT, 60, 40)), "p.kml")
    r = geo.lire(_kml_reperes(), "p.kml")
    assert r.superficie_m2 == simple.superficie_m2 and r.empreinte == simple.empreinte      # la zone annexe n'est pas la parcelle
    assert [(e["type"], e["nom"]) for e in r.elements] == [("zone", "Maison"), ("ligne", "Route nationale"), ("point", "Tourelle")] or \
        sorted((e["type"], e["nom"]) for e in r.elements) == [("ligne", "Route nationale"), ("point", "Tourelle"), ("zone", "Maison")]
    assert simple.elements == []


def test_reperes_invalides_ignores():
    extra = _placemark("Cassé", "<LineString><coordinates>abc</coordinates></LineString>") + _placemark("Seul", "<LineString><coordinates>43.2,-11.7</coordinates></LineString>")
    r = geo.lire(kml(points(LON, LAT, 60, 40), extra=extra), "p.kml")
    assert r.elements == []


def test_reperes_enregistres_dessines_et_suivis(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    page = televerser(pub, did, _kml_reperes()).data.decode()
    assert "3 repère(s) du croquis" in page
    _pret(pub, dom, did)
    texte = texte_pdf(dire.get(f"/dossiers/{did}/imprimer/plan_croquis"))
    assert "Route nationale" in texte and "Tourelle" in texte and "Maison" in texte
    with app.app_context():
        from app import verification as V
        from app.models import DocumentEmis, Geometrie

        g = Geometrie.query.filter_by(dossier_id=did, remplace_at=None).one()
        assert g.elements_json and "reperes" in V.donnees(db.session.get(Dossier, did), "plan_croquis")
        rec = DocumentEmis.query.filter_by(dossier_id=did, type="plan_croquis").one()
        assert V.evaluer(rec) == V.VALIDE
    televerser(pub, did, kml(points(LON, LAT, 60, 40)), motif="sans repères")
    with app.app_context():
        assert V.evaluer(DocumentEmis.query.filter_by(dossier_id=did, type="plan_croquis").one()) == V.OBSOLETE


def test_parcelles_voisines_dessinees_sur_le_plan(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    a = _dossier_trace(pub, kml(points(LON, LAT, 60, 40)))
    b = _dossier_trace(pub, kml(points(LON + dlon(61), LAT, 50, 40)))
    _pret(pub, dom, a)
    with app.app_context():
        numero_b = db.session.get(Dossier, b).numero
    texte = texte_pdf(dire.get(f"/dossiers/{a}/imprimer/plan_croquis"))
    assert texte.count(numero_b) >= 2                                   # dans le dessin ET dans le tableau des voisins
    from app import pdf as Pdf
    from app.models import Geometrie

    with app.app_context():
        g = Geometrie.query.filter_by(dossier_id=a, remplace_at=None).one()
        voisins = Pdf._voisins_pour_dessin(g)
        assert [n for n, _ in voisins] == [numero_b]
        assert Pdf.mise_en_page_croquis(g, 17 * 28.3465, 9.5 * 28.3465, voisins)["n"] in Pdf.ECHELLES


# ------------------------------------------------------------------ saisie dès la création du dossier
def test_description_saisie_a_la_creation(app, login):
    from conftest import DONNEES_DOSSIER

    pub = login("pub")
    page = pub.get("/dossiers/nouveau").data.decode()
    assert "Description du terrain (plan croquis)" in page and 'name="limitrophe_nord"' in page and 'name="topographe_leve"' not in page and 'name="agrement_topographe"' not in page   # nom et agrément viennent du compte
    r = pub.post("/dossiers/nouveau", data={**DONNEES_DOSSIER, "limitrophe_nord": "Route", "nature_terrain": "Terrain nu",
                                             "date_leve": "2026-02-03", "mode_leve": "GPS", "observations_plan": "RAS"})
    assert r.status_code == 302
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert d.limitrophe_nord == "Route" and d.nature_terrain == "Terrain nu" and d.date_leve == date(2026, 2, 3) and d.observations_plan == "RAS"
        assert AuditLog.query.filter_by(action="description_terrain_modifiee", entite_id=did).count() == 1
    detail = pub.get(f"/dossiers/{did}").data.decode()
    assert 'value="Route"' in detail and "Terrain nu" in detail


def test_creation_refusee_si_description_invalide(app, login):
    from conftest import DONNEES_DOSSIER

    pub = login("pub")
    r = pub.post("/dossiers/nouveau", data={**DONNEES_DOSSIER, "nature_terrain": "Volcan"})
    assert r.status_code == 400 and "Nature du terrain inconnue" in r.data.decode()
    assert 'value="Said"' in r.data.decode()                                    # le reste de la saisie est conservé
    with app.app_context():
        assert Dossier.query.count() == 0


def test_ancien_dossier_sans_description(app, login):
    adm = login("admin")
    page = adm.get("/dossiers/ancien/nouveau").data.decode()
    assert "Description du terrain" not in page


def test_agrement_saisi_dans_le_compte_du_topographe(app, login):
    adm = login("admin")
    r = adm.post("/utilisateurs/nouveau", data={"nom": "Topo Nouveau", "username": "topo.n", "role": "topo_public", "ile_id": "1",
                                                 "agrement": "AG-0042", "password": "Provisoire-123456"})
    assert r.status_code == 302
    r = adm.post("/utilisateurs/nouveau", data={"nom": "Domaine N", "username": "dom.n", "role": "domaine", "ile_id": "1",
                                                 "agrement": "AG-9", "password": "Provisoire-123456"})
    assert r.status_code == 302
    with app.app_context():
        from app.models import User

        assert User.query.filter_by(username="topo.n").one().agrement == "AG-0042"
        assert User.query.filter_by(username="dom.n").one().agrement is None           # réservé aux topographes
    assert "N° d'agrément" in login("pub").get("/profil").data.decode()
    assert "N° d'agrément" not in login("dom").get("/profil").data.decode()


def test_bouton_modifier_et_disposition_de_la_fiche(app, login):
    pub, dom, dire, adm = login("pub"), login("dom"), login("dir"), login("admin")
    did = creer_dossier(pub)
    _poster(pub, did, limitrophe_nord="Route")
    _pret(pub, dom, did)
    agir(dire, did, "valider")
    for client, bouton in [(dire, True), (adm, True), (pub, False), (dom, False)]:
        page = client.get(f"/dossiers/{did}").data.decode()
        assert "Description du terrain (plan croquis)" in page and "Route" in page                      # visible par tous (résumé)
        assert ('id="btn-modifier-description"' in page) is bouton and ('id="form-description"' in page) is bouton
    page = dire.get(f"/dossiers/{did}").data.decode()
    # à gauche : propriétaire, parcelle, numéros ; à droite : la description
    assert page.index(">Propriétaire<") < page.index(">Parcelle<") < page.index(">Numéros<") < page.index('id="description-terrain"')
    ancien = adm.get("/dossiers/ancien/nouveau")
    assert ancien.status_code == 200


# ------------------------------------------------------------------ feuille A3 unique
def test_plan_croquis_tient_sur_une_feuille_a3_paysage(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    n = 70                                                                          # contour très détaillé (70 sommets)
    pts = [(LON + dlon(40) * math.cos(2 * math.pi * i / n), LAT + 40 / 110574.0 * math.sin(2 * math.pi * i / n)) for i in range(n)]
    pts.append(pts[0])
    did = _dossier_trace(pub, kml(pts))
    for k in range(6):                                                              # plusieurs parcelles voisines
        _dossier_trace(pub, kml(points(LON + dlon(42 + 60 * (k % 3)), LAT + (k // 3) * 0.0006 - 0.0002, 50, 40)))
    _poster(pub, did, limitrophe_nord="N" * 150, limitrophe_sud="S" * 150, usage_terrain="U" * 190, acces_terrain="A" * 190,
            observations_plan="\n".join(f"Observation {i} " + "x" * 120 for i in range(7)), nature_terrain="Terrain mixte",
            date_leve="2026-03-01", mode_leve="Station totale")
    _pret(pub, dom, did)
    rep = dire.get(f"/dossiers/{did}/imprimer/plan_croquis")
    lecteur = PdfReader(BytesIO(rep.data))
    assert len(lecteur.pages) == 1
    largeur, hauteur = (float(x) for x in lecteur.pages[0].mediabox[2:])
    assert round(largeur / 28.3465) in (42, 41) and round(hauteur / 28.3465) in (30, 29)        # A3 paysage (42 x 29,7 cm)
    texte = texte_pdf(rep)
    assert "Visa du Domaine" in texte and "Scannez pour vérifier" in texte and "Observation 6" in texte


def test_plan_sans_trace_a_aussi_une_seule_feuille_a3(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    _certifier(pub, dom, dire, did)
    lecteur = PdfReader(BytesIO(dire.get(f"/dossiers/{did}/imprimer/plan_croquis").data))
    assert len(lecteur.pages) == 1 and float(lecteur.pages[0].mediabox[2]) > float(lecteur.pages[0].mediabox[3])
    certificat = PdfReader(BytesIO(dire.get(f"/dossiers/{did}/imprimer/certificat").data)).pages[0].mediabox
    assert round(float(certificat[2]) / 28.3465) == 21                                      # les autres documents restent en A4
