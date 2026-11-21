from app.extensions import db
from app.models import AuditLog, User

from conftest import MDP, connecter

NOUVEAU = "nouveau-mdp-tres-solide"


def infos(client, **champs):
    return client.post("/profil", data={"action": "infos", **champs})


def mdp(client, **champs):
    return client.post("/profil", data={"action": "mdp", **champs})


def user(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def test_le_nom_du_compte_mene_au_profil(app, login):
    for role in ("admin", "dir", "dom", "pub", "priv1"):
        c = login(role)
        page = c.get("/").data.decode()
        assert 'href="/profil"' in page and 'title="Mon profil' in page
        assert c.get("/profil").status_code == 200
    assert app.test_client().get("/profil").status_code == 302


def test_page_profil_affiche_le_compte(app, login):
    page = login("dom").get("/profil").data.decode()
    assert 'value="dom"' in page and "Domaine" in page and "Changer le mot de passe" in page and "disabled" in page
    assert "Compte actif" in page and "Compte créé le" in page and "Dernière connexion" in page


def test_modifier_le_nom_sans_mot_de_passe(app, login):
    c = login("dom")
    r = infos(c, nom="  Aïcha   Mohamed ", username="dom")
    assert r.status_code == 302
    with app.app_context():
        u = db.session.get(User, user(app, "dom"))
        assert (u.nom, u.username) == ("Aïcha Mohamed", "dom")
        assert AuditLog.query.filter_by(action="modification_profil", entite_id=u.id).count() == 1
    assert "Aïcha Mohamed" in c.get("/").data.decode()


def test_changer_d_identifiant_sans_mot_de_passe(app, login):
    c = login("dom")
    assert "Mot de passe actuel" not in c.get("/profil").data.decode()                  # le champ n'existe plus
    r = infos(c, nom="dom", username="Nouveau.Nom")
    assert r.status_code == 302
    with app.app_context():
        assert User.query.filter_by(username="nouveau.nom").count() == 1 and User.query.filter_by(username="dom").count() == 0
    connecter(app, "nouveau.nom")


def test_validations_des_informations(app, login):
    c = login("dom")
    assert "déjà utilisé" in infos(c, nom="dom", username="dir").data.decode()
    assert "Identifiant invalide" in infos(c, nom="dom", username="a b").data.decode()
    assert "obligatoire" in infos(c, nom=" ", username="dom").data.decode()
    assert "trop long" in infos(c, nom="x" * 121, username="dom").data.decode()
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).nom == "dom"


def test_le_type_de_compte_ne_se_change_pas_depuis_le_profil(app, login):
    c = login("dom")
    infos(c, nom="dom", username="dom", role="admin", actif="0")
    with app.app_context():
        u = db.session.get(User, user(app, "dom"))
        assert u.role == "domaine" and u.actif


def test_changer_le_mot_de_passe(app, login):
    c = login("dom")
    assert "Ancien mot de passe incorrect" in mdp(c, ancien="faux", nouveau=NOUVEAU, confirmation=NOUVEAU).data.decode()
    assert "au moins" in mdp(c, ancien=MDP, nouveau="court", confirmation="court").data.decode()
    assert "confirmation" in mdp(c, ancien=MDP, nouveau=NOUVEAU, confirmation="autre-chose-123").data.decode()
    assert "différent" in mdp(c, ancien=MDP, nouveau=MDP, confirmation=MDP).data.decode()
    assert mdp(c, ancien=MDP, nouveau=NOUVEAU, confirmation=NOUVEAU).status_code == 302
    with app.app_context():
        u = db.session.get(User, user(app, "dom"))
        assert u.check_password(NOUVEAU) and not u.check_password(MDP)
        assert AuditLog.query.filter_by(action="changement_mdp", entite_id=u.id).count() == 1
    r = app.test_client().post("/connexion", data={"username": "dom", "password": NOUVEAU})
    assert r.status_code == 302


def test_on_ne_modifie_que_son_propre_compte(app, login):
    c = login("dom")
    infos(c, nom="Pirate", username="dom", id=str(user(app, "dir")), user_id=str(user(app, "dir")))
    with app.app_context():
        assert db.session.get(User, user(app, "dir")).nom == "dir"


def test_action_inconnue_ne_change_rien(app, login):
    c = login("dom")
    assert c.post("/profil", data={"action": "autre", "nom": "X"}).status_code == 200
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).nom == "dom"


# ------------------------------------------------------------------ informations détaillées
def test_profil_sans_la_section_des_droits_du_role(app, login):
    for role in ("dom", "dir", "admin", "priv1"):
        page = login(role).get("/profil").data.decode()
        assert "Ce que mon type de compte me permet" not in page


def test_chiffres_d_activite(app, login):
    from conftest import creer_dossier

    pub = login("pub")
    creer_dossier(pub)
    creer_dossier(pub)
    page = pub.get("/profil").data.decode()
    assert "Dossiers créés" in page and ">2<" in page.replace(" ", "").replace("\n", "")            # deux dossiers créés
    assert "Mutations saisies" not in page and "Documents générés" in page                          # chiffres propres à chaque rôle
    dom = login("dom")
    assert "Mutations saisies" in dom.get("/profil").data.decode() and "Dossiers créés" not in dom.get("/profil").data.decode()


def test_la_section_mes_dernieres_actions_n_existe_plus(app, login):
    from conftest import creer_dossier

    creer_dossier(login("pub"))
    for role in ("admin", "dir", "dom", "pub", "priv1"):
        page = login(role).get("/profil").data.decode()
        assert "Mes dernières actions" not in page and "dernières actions" not in page
        assert 'name="du"' not in page and 'name="au"' not in page and "datetime-local" not in page     # ni son filtre par date
    assert "Connexions" in login("pub").get("/profil").data.decode()                                     # le reste du profil est inchangé


def test_connexions_et_tentatives_echouees(app, login):
    login("dom")
    for _ in range(3):
        app.test_client().post("/connexion", data={"username": "dom", "password": "faux"})
    page = login("dom").get("/profil").data.decode()
    assert "3 tentatives de connexion échouées" in page and "changez votre mot de passe" in page
    assert "Aucune tentative de connexion échouée" in login("dir").get("/profil").data.decode()


# ------------------------------------------------------------------ téléphone
def test_telephone_du_compte(app, login):
    c = login("dom")
    assert 'name="telephone"' in c.get("/profil").data.decode()
    assert infos(c, nom="dom", username="dom", telephone="  +269  333 20 20 ").status_code == 302
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).telephone == "+269 333 20 20"
    page = c.get("/profil").data.decode()
    assert page.count("+269 333 20 20") >= 2                                          # dans la carte d'identité et dans le formulaire
    assert infos(c, nom="dom", username="dom", telephone="").status_code == 302       # facultatif : on peut l'effacer
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).telephone is None


def test_telephone_invalide_refuse(app, login):
    c = login("dom")
    for faux in ("abc", "12", "+269 <script>", "1" * 30, "33-32-0a-20"):
        r = infos(c, nom="dom", username="dom", telephone=faux)
        assert r.status_code == 400 and "Numéro de téléphone invalide" in r.data.decode(), faux
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).telephone is None
    for bon in ("3332020", "+269 333 20 20", "(+269) 333-20-20", "0033.1.23.45.67.89"):
        assert infos(c, nom="dom", username="dom", telephone=bon).status_code == 302, bon


def test_changer_seulement_le_telephone(app, login):
    c = login("dom")
    assert infos(c, nom="dom", username="dom", telephone="3332020").status_code == 302
    with app.app_context():
        assert db.session.get(User, user(app, "dom")).telephone == "3332020"


def test_telephone_echappe(app, login):
    with app.app_context():
        db.session.get(User, user(app, "dom")).telephone = '"><b>x</b>'
        db.session.commit()
    assert "<b>x</b>" not in login("dom").get("/profil").data.decode()
