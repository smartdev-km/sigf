import io
import struct

from PIL import Image

from conftest import MDP


def test_favicon_publique_et_valide(app):
    r = app.test_client().get("/favicon.ico")                                   # sans être connecté : sert aussi sur la page de connexion
    assert r.status_code == 200 and r.mimetype == "image/x-icon" and "max-age" in r.headers["Cache-Control"]
    data = r.data
    reserve, type_, nombre = struct.unpack("<HHH", data[:6])
    assert (reserve, type_) == (0, 1) and nombre >= 4                           # vrai fichier ICO avec plusieurs tailles
    ico = Image.open(io.BytesIO(data))
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico.ico.sizes())


def test_favicon_sert_meme_si_le_mot_de_passe_doit_changer(app):
    from app.extensions import db
    from app.models import User

    with app.app_context():
        User.query.filter_by(username="dom").one().doit_changer_mdp = True
        db.session.commit()
    c = app.test_client()
    c.post("/connexion", data={"username": "dom", "password": MDP})
    assert c.get("/favicon.ico").status_code == 200                              # pas redirigé vers « changer le mot de passe »


def test_les_pages_declarent_l_icone(app, login):
    for page in (app.test_client().get("/connexion").data.decode(), login("dir").get("/bienvenue").data.decode(), app.test_client().get("/verifier").data.decode()):
        assert 'rel="icon" href="/favicon.ico"' in page
        assert "favicon-32.png" in page and "apple-touch-icon.png" in page and 'name="theme-color"' in page


def test_fichiers_d_icone_statiques(app):
    c = app.test_client()
    for url, taille in (("/static/favicon-32.png", (32, 32)), ("/static/apple-touch-icon.png", (180, 180))):
        r = c.get(url)
        assert r.status_code == 200 and Image.open(io.BytesIO(r.data)).size == taille
