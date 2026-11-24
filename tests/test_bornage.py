"""Plan de bornage : tracé KMZ déposé à l'étape « Plan de bornage » du titrage, puis impression (feuille A3, bornes numérotées sur le dessin)."""
import io
import math
import re
from datetime import date, timedelta
from io import BytesIO

from pypdf import PdfReader

from app import constants as C
from app.extensions import db
from app.models import AuditLog, Bornage, DocumentEmis, Dossier

from conftest import creer_dossier
from test_carte import LAT, LON, kml, points, televerser
from test_partages import cree, numero
from test_titrage import avancer, jour, etape, bloquer, DESCRIPTION


def dlon(m):
    return m / (111320.0 * math.cos(math.radians(abs(LAT))))


def fichier_bornage(largeur=60, hauteur=40, decalage=0, extra=""):
    return kml(points(LON + dlon(decalage), LAT, largeur, hauteur), extra=extra)


def deposer(client, did, contenu=None, nom="bornage.kml", motif=None):
    data = {"fichier": (io.BytesIO(fichier_bornage() if contenu is None else contenu), nom)}
    if motif is not None:
        data["motif"] = motif
    return client.post(f"/dossiers/{did}/titrage/bornage", data=data, content_type="multipart/form-data", follow_redirects=True)


def texte(rep):
    return re.sub(r"[ \xa0]", " ", "\n".join(p.extract_text() for p in PdfReader(BytesIO(rep.data)).pages))


def pret_pour_bornage(login, identite="CNI-B"):
    """Dossier certifié avec tracé, Journal officiel fait : le Domaine est à l'étape « Plan de bornage »."""
    did = cree(login, identite=identite)
    televerser(login("pub"), did, kml(points(LON, LAT, 60, 40)))
    avancer(login("fp"), did, "JO-1", jour(-30))
    return did


def borne(app, login, identite="CNI-B", ref="PV-1"):
    did = pret_pour_bornage(login, identite)
    deposer(login("dom"), did)
    avancer(login("dom"), did, ref, jour(-20))
    return did


# ------------------------------------------------------------------ dépôt du tracé
def test_le_domaine_depose_le_trace_de_bornage(app, login):
    did = pret_pour_bornage(login)
    page = deposer(login("dom"), did).data.decode()
    assert "Tracé de bornage enregistré" in page and "4 bornes" in page
    with app.app_context():
        b = Bornage.query.one()
        assert (b.dossier_id, b.version, b.uploaded_by.username) == (did, 1, "dom")
        assert 2300 < b.superficie_m2 < 2500 and b.cle.startswith(f"dossiers/{did}/bornage/")
        assert AuditLog.query.filter_by(action="bornage_trace_ajoute").count() == 1
    page = login("dom").get(f"/titrage/{did}").data.decode()
    assert "bornage.kml" in page and "4 bornes" in page and "Imprimable une fois l'étape" in page and "Imprimer le plan de bornage" not in page


def test_depot_possible_pendant_l_etape_dans_le_formulaire(app, login):
    did = pret_pour_bornage(login)
    dom = login("dom")
    page = dom.get(f"/titrage/{did}").data.decode()
    assert 'name="fichier_bornage"' in page
    r = dom.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "PV-9", "date_etape": jour(-20), "fichier_bornage": (io.BytesIO(fichier_bornage()), "b.kml")},
                 content_type="multipart/form-data", follow_redirects=True)
    assert "enregistrée" in r.data.decode() and etape(app, did) == C.TIT_JC
    with app.app_context():
        assert Bornage.query.count() == 1


def test_fichier_de_bornage_invalide_n_avance_pas_l_etape(app, login):
    did = pret_pour_bornage(login)
    dom = login("dom")
    r = dom.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "PV-9", "date_etape": jour(-20), "fichier_bornage": (io.BytesIO(b"pas du kml"), "b.kml")},
                 content_type="multipart/form-data", follow_redirects=True)
    assert "illisible" in r.data.decode() and etape(app, did) == C.TIT_BORNAGE
    with app.app_context():
        assert Bornage.query.count() == 0
    assert "Format non accepté" in deposer(dom, did, nom="plan.pdf").data.decode()


def test_remplacement_avec_motif_et_versions(app, login):
    did = pret_pour_bornage(login)
    dom = login("dom")
    deposer(dom, did)
    assert "motif du remplacement est obligatoire" in deposer(dom, did, fichier_bornage(70, 40)).data.decode()
    page = deposer(dom, did, fichier_bornage(70, 40), motif="Levé repris au GPS").data.decode()
    assert "Tracé de bornage enregistré" in page and "Versions" in page and "Levé repris au GPS" in page
    with app.app_context():
        v1, v2 = Bornage.query.order_by(Bornage.version).all()
        assert v1.remplace_at is not None and v2.remplace_at is None and v2.version == 2
        assert AuditLog.query.filter_by(action="bornage_trace_remplace").count() == 1


def test_avertissement_si_le_bornage_ne_recoupe_pas_le_plan_croquis(app, login):
    did = pret_pour_bornage(login)
    page = deposer(login("dom"), did, fichier_bornage(60, 40, decalage=500)).data.decode()
    assert "ne recoupe pas le plan croquis" in page
    assert "ne recoupe pas" not in deposer(login("dom"), did, fichier_bornage(), motif="Bon fichier").data.decode()


def test_telechargement_du_fichier_d_origine(app, login):
    did = pret_pour_bornage(login)
    deposer(login("dom"), did)
    rep = login("dom").get(f"/dossiers/{did}/titrage/bornage/1/telecharger")
    assert rep.status_code == 200 and b"<kml" in rep.data
    assert login("dom").get(f"/dossiers/{did}/titrage/bornage/9/telecharger").status_code == 404


# ------------------------------------------------------------------ droits
def test_droits_de_depot(app, login):
    did = pret_pour_bornage(login)
    for client in (login("fp"), login("dir"), login("pub")):
        assert client.post(f"/dossiers/{did}/titrage/bornage", data={"fichier": (io.BytesIO(fichier_bornage()), "b.kml")}, content_type="multipart/form-data").status_code in (403, 404)
    assert login("priv1").post(f"/dossiers/{did}/titrage/bornage", data={}).status_code == 404
    assert login("admin").post(f"/dossiers/{did}/titrage/bornage", data={"fichier": (io.BytesIO(fichier_bornage()), "b.kml")}, content_type="multipart/form-data").status_code == 302
    assert f'action="/dossiers/{did}/titrage/bornage"' not in login("fp").get(f"/titrage/{did}").data.decode()     # pas de formulaire de dépôt pour la Fonction publique
    assert f'action="/dossiers/{did}/titrage/bornage"' in login("dom").get(f"/titrage/{did}").data.decode()


def test_pas_de_depot_avant_l_etape_du_bornage_ni_apres_le_titre(app, login):
    did = cree(login)                                                        # étape « Journal officiel » : trop tôt
    assert login("dom").post(f"/dossiers/{did}/titrage/bornage", data={"fichier": (io.BytesIO(fichier_bornage()), "b.kml")}, content_type="multipart/form-data").status_code == 403
    assert 'id="bornage"' not in login("dom").get(f"/titrage/{did}").data.decode()
    fini = pret_pour_bornage(login, "CNI-F")
    deposer(login("dom"), fini); avancer(login("dom"), fini, "PV", jour(-20)); avancer(login("fp"), fini, "JC", jour(-10)); avancer(login("dom"), fini, "TF-1", jour(-1))
    assert etape(app, fini) == C.TIT_FIN
    assert login("dom").post(f"/dossiers/{fini}/titrage/bornage", data={"fichier": (io.BytesIO(fichier_bornage()), "b.kml")}, content_type="multipart/form-data").status_code == 403


def test_dossier_bloque_ne_recoit_pas_de_bornage(app, login):
    did = pret_pour_bornage(login)
    bloquer(login("fp"), did)
    assert login("dom").post(f"/dossiers/{did}/titrage/bornage", data={"fichier": (io.BytesIO(fichier_bornage()), "b.kml")}, content_type="multipart/form-data").status_code == 403


# ------------------------------------------------------------------ impression
def test_impression_reservee_apres_l_etape_et_avec_un_trace(app, login):
    dom, dire = login("dom"), login("dir")
    did = pret_pour_bornage(login)
    assert dire.get(f"/dossiers/{did}/imprimer/bornage").status_code == 403           # étape pas faite, pas de tracé
    deposer(dom, did)
    assert dire.get(f"/dossiers/{did}/imprimer/bornage").status_code == 403           # tracé déposé, étape pas validée
    avancer(dom, did, "PV-7", jour(-20))
    rep = dire.get(f"/dossiers/{did}/imprimer/bornage")
    assert rep.status_code == 200 and rep.mimetype == "application/pdf"
    assert dom.get(f"/dossiers/{did}/imprimer/bornage").status_code == 200 and login("admin").get(f"/dossiers/{did}/imprimer/bornage").status_code == 200
    for client in (login("pub"), login("priv1")):
        assert client.get(f"/dossiers/{did}/imprimer/bornage").status_code in (403, 404)
    assert login("fp").get(f"/dossiers/{did}/imprimer/bornage").status_code == 403
    sans_trace = cree(login, identite="CNI-S")
    avancer(login("fp"), sans_trace, "JO", jour(-30)); avancer(dom, sans_trace, "PV", jour(-20))
    assert dire.get(f"/dossiers/{sans_trace}/imprimer/bornage").status_code == 403    # étape faite mais aucun tracé de bornage


def test_le_plan_de_bornage_est_une_feuille_a3_avec_les_bornes(app, login):
    did = borne(app, login)
    rep = login("dir").get(f"/dossiers/{did}/imprimer/bornage")
    lecteur = PdfReader(BytesIO(rep.data))
    assert len(lecteur.pages) == 1
    largeur, hauteur = (float(x) for x in lecteur.pages[0].mediabox[2:])
    assert round(largeur / 28.3465) in (41, 42) and round(hauteur / 28.3465) in (29, 30)
    t = texte(rep)
    assert "PLAN DE BORNAGE" in t and "Bornes, côtés et orientations" not in t and "UTM fuseau" not in t and "Périmètre borné" in t   # pas de tableau des bornes
    assert "Procès-verbal de bornage" in t and "PV-1" in t and numero(app, did) in t
    assert "Superficie bornée" in t and "Superficie du plan croquis" in t and "Le Domaine" in t and "Scannez pour vérifier" in t
    assert re.search(r"6\d,\d\d m", t) and re.search(r"4\d,\d\d m", t)                                  # longueurs des côtés (60 m x 40 m)


def test_plan_de_bornage_exemplaire_signe_et_obsolescence(app, login):
    from app import verification as V

    did = borne(app, login)
    dire = login("dir")
    dire.get(f"/dossiers/{did}/imprimer/bornage")
    with app.app_context():
        rec = DocumentEmis.query.filter_by(dossier_id=did, type="bornage").one()
        assert rec.copie_n == 1 and rec.pdf_cle and V.evaluer(rec) == V.VALIDE
    # un nouveau tracé de bornage rend l'exemplaire déjà imprimé « non à jour »
    deposer(login("dom"), did, fichier_bornage(80, 50), motif="Contre-levé")
    with app.app_context():
        assert V.evaluer(DocumentEmis.query.filter_by(dossier_id=did, type="bornage").one()) == V.OBSOLETE
    dire.get(f"/dossiers/{did}/imprimer/bornage")
    with app.app_context():
        recs = DocumentEmis.query.filter_by(dossier_id=did, type="bornage").order_by(DocumentEmis.id).all()
        assert [r.copie_n for r in recs] == [1, 2] and V.evaluer(recs[1]) == V.VALIDE
    assert "Plan de bornage" in dire.get(f"/dossiers/{did}").data.decode()                        # listé dans les documents du dossier


def test_retour_en_arriere_invalide_le_plan_de_bornage(app, login):
    from app import verification as V

    did = borne(app, login)
    login("dir").get(f"/dossiers/{did}/imprimer/bornage")
    with app.app_context():
        assert V.evaluer(DocumentEmis.query.filter_by(dossier_id=did, type="bornage").one()) == V.VALIDE
        d = db.session.get(Dossier, did)
        assert d.etape_titre == C.TIT_JC
    # le Journal de clôture (Fonction publique) renvoie le dossier au bornage : le procès-verbal est annulé
    login("fp").post(f"/dossiers/{did}/titrage/retour", data={"motif": "Procès-verbal à reprendre"})
    with app.app_context():
        assert db.session.get(Dossier, did).etape_titre == C.TIT_BORNAGE
        assert V.evaluer(DocumentEmis.query.filter_by(dossier_id=did, type="bornage").one()) == V.OBSOLETE
    assert login("dir").get(f"/dossiers/{did}/imprimer/bornage").status_code == 403                # plus imprimable tant que l'étape n'est pas refaite


def test_bouton_d_impression_dans_la_page_titrage(app, login):
    did = borne(app, login)
    assert "Imprimer le plan de bornage" in login("dom").get(f"/titrage/{did}").data.decode()
    assert "Imprimer le plan de bornage" in login("dir").get(f"/titrage/{did}").data.decode()
    assert "Imprimer le plan de bornage" not in login("fp").get(f"/titrage/{did}").data.decode()


def test_plusieurs_parties_et_reperes_dans_le_fichier_de_bornage(app, login):
    did = pret_pour_bornage(login)
    ligne = "<Placemark><name>Chemin communal</name><LineString><coordinates>43.2540000,-11.6990000 43.2560000,-11.7010000</coordinates></LineString></Placemark>"
    deposer(login("dom"), did, kml(points(LON, LAT, 30, 30), points(LON + dlon(40), LAT, 20, 20), extra=ligne))
    avancer(login("dom"), did, "PV", jour(-20))
    rep = login("dir").get(f"/dossiers/{did}/imprimer/bornage")
    t = texte(rep)
    assert rep.status_code == 200 and "A1" in t and "B1" in t and "Chemin communal" in t               # bornes A1.. / B1.. et repère nommé
