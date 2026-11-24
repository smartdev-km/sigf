from app import constants as C
from app.extensions import db
from app.models import User

from conftest import MDP, agir, connecter, creer_dossier


def connexion(app, username, **params):
    c = app.test_client()
    r = c.post("/connexion" + params.get("suffixe", ""), data={"username": username, "password": MDP})
    return c, r


def test_apres_la_connexion_on_arrive_sur_la_page_de_bienvenue(app):
    for username in ("admin", "dir", "dom", "pub", "priv1"):
        c, r = connexion(app, username)
        assert r.status_code == 302 and r.headers["Location"].endswith("/bienvenue"), username
        page = c.get(r.headers["Location"]).data.decode()
        assert "Bienvenue dans le Système Intégré de Gestion Foncière" in page and f"Bonjour <strong" in page


def test_la_redirection_demandee_est_respectee(app):
    c, r = connexion(app, "dom", suffixe="?next=/mutations")
    assert r.headers["Location"].endswith("/mutations")                              # un lien profond n'est pas écrasé
    c, r = connexion(app, "dom", suffixe="?next=//evil.example")
    assert r.headers["Location"].endswith("/bienvenue")                              # adresse externe refusée


def test_bouton_tableau_de_bord_pour_tous_les_roles(app, login):
    for role in ("admin", "dir", "dom", "pub", "priv1"):
        page = login(role).get("/bienvenue").data.decode()
        assert "Ouvrir le tableau de bord" in page and 'href="/"' in page, role


def test_acces_rapides_selon_le_role(app, login):
    # liens attendus par rôle (le topographe privé n'a ni mutations, ni propriétaires, ni blocages, ni anciens dossiers)
    attendus = {
        "admin": {"/utilisateurs/", "/mutations", "/proprietaires", "/blocages", "/carte", "/dossiers/nouveau", "/dossiers/anciens", "/dossiers/ancien/nouveau", "/verifier", "/profil"},
        "dir": {"/utilisateurs/", "/mutations", "/proprietaires", "/blocages", "/carte", "/dossiers/nouveau", "/dossiers/anciens", "/dossiers/ancien/nouveau", "/verifier", "/profil"},
        "dom": {"/mutations", "/proprietaires", "/blocages", "/carte", "/dossiers/anciens", "/dossiers/ancien/nouveau", "/verifier", "/profil"},
        "pub": {"/mutations", "/proprietaires", "/blocages", "/carte", "/dossiers/nouveau", "/dossiers/anciens", "/dossiers/ancien/nouveau", "/verifier", "/profil"},
        "priv1": {"/carte", "/dossiers/nouveau", "/verifier", "/profil", "/dossiers", "/proprietaires"},
    }
    interdits = {
        "dom": {"/utilisateurs/", "/dossiers/nouveau"},
        "pub": {"/utilisateurs/"},
        "priv1": {"/utilisateurs/", "/mutations", "/blocages", "/dossiers/anciens", "/dossiers/ancien/nouveau"},
    }
    for role, liens in attendus.items():
        zone = login(role).get("/bienvenue").data.decode().split("Accès rapides")[1]
        for lien in liens:
            assert f'href="{lien}"' in zone, (role, lien)
        for lien in interdits.get(role, ()):
            assert f'href="{lien}"' not in zone, (role, lien)


def test_chaque_acces_mene_a_une_page_qui_s_ouvre(app, login):
    import re

    for role in ("admin", "dir", "dom", "pub", "priv1"):
        c = login(role)
        for lien in re.findall(r'class="acces" href="([^"]+)"', c.get("/bienvenue").data.decode()):
            assert c.get(lien).status_code == 200, (role, lien)


def test_pastilles_role_ile_et_dossiers_a_traiter(app, login):
    pub = login("pub")
    creer_dossier(pub)                                                              # plan croquis en cours : à traiter par le topo public
    page = pub.get("/bienvenue").data.decode()
    assert "Topographe public" in page and "Toutes les îles" in page and "1 dossier à traiter" in page
    with app.app_context():
        User.query.filter_by(username="pub").one().ile_id = 2
        db.session.commit()
    assert "Ndzuwani" in pub.get("/bienvenue").data.decode()
    assert "Toutes les îles" in login("admin").get("/bienvenue").data.decode()      # l'administrateur n'a jamais d'île
    assert "à traiter" not in login("dom").get("/bienvenue").data.decode().split("Accès rapides")[0]


def test_compteurs_sur_les_acces(app, login):
    from test_mutations import dossier_certifie, saisir

    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000")
    page = login("dir").get("/bienvenue").data.decode()
    assert 'title="À traiter">1<' in page                                           # mutation à valider, pour le Directeur
    assert 'title="À traiter"' not in login("dom").get("/bienvenue").data.decode()


def test_page_protegee_et_nom_echappe(app, login):
    r = app.test_client().get("/bienvenue")
    assert r.status_code == 302 and "/connexion" in r.headers["Location"]
    with app.app_context():
        User.query.filter_by(username="dom").one().nom = "<script>alert(1)</script>"
        db.session.commit()
    assert "<script>alert(1)" not in login("dom").get("/bienvenue").data.decode()


def test_le_tableau_de_bord_garde_son_adresse(app, login):
    assert login("dir").get("/").status_code == 200
    assert 'id="a-traiter"' in login("dir").get("/").data.decode()


def test_le_logo_ramene_a_l_accueil_sans_bouton_accueil_dans_le_menu(app, login):
    for role in ("admin", "dir", "dom", "pub", "priv1"):
        page = login(role).get("/dossiers").data.decode()
        menu = page.split('<ul class="nav flex-column gap-1">')[1].split("</ul>")[0]
        assert "Accueil" not in menu and 'href="/bienvenue"' not in menu                       # pas de bouton « Accueil » dans le menu
        assert page.count('href="/bienvenue"') == 2, role                                       # logo « SIGF » du menu + logo de la barre mobile
        assert 'title="Accueil"' in page
    anonyme = app.test_client().get("/connexion").data.decode()
    assert "/bienvenue" not in anonyme                                                           # rien de tout cela avant la connexion
