import pytest

from app import create_app
from app import constants as C
from app.extensions import db
from app.models import Ile, User

MDP = "motdepasse-solide-1"


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            # Génération automatique des documents à la certification : désactivée par défaut dans les tests (rapidité, numéros de copie
            # prévisibles) ; les tests de tests/test_generation.py l'activent.
            "DOCUMENTS_AUTOMATIQUES": False,
        }
    )
    with app.app_context():
        db.create_all()
        for nom in C.ILES:
            db.session.add(Ile(nom=nom))
        for username, role in [
            ("admin", C.ADMIN),
            ("dir", C.DIRECTEUR),
            ("pub", C.TOPO_PUBLIC),
            ("priv1", C.TOPO_PRIVE),
            ("priv2", C.TOPO_PRIVE),
            ("dom", C.DOMAINE),
            ("fp", C.FONCTION_PUBLIQUE),
        ]:
            u = User(nom=username, username=username, role=role, doit_changer_mdp=False)
            u.set_password(MDP)
            db.session.add(u)
        db.session.commit()
    yield app


_compteur = iter(range(1, 10**9))
_URLS_CREATION = ("/dossiers/nouveau", "/dossiers/ancien/nouveau")


def _avec_identite(client_class):
    """Le n° de pièce d'identité est obligatoire à la création d'un dossier : les tests qui ne s'en occupent pas
    en reçoivent un, unique (donc une personne distincte par dossier). Une clé « proprio_identite » présente,
    même vide, est respectée telle quelle."""

    class Client(client_class):
        def post(self, url, *a, **kw):
            data = kw.get("data")
            if url in _URLS_CREATION and isinstance(data, dict) and "proprio_identite" not in data:
                kw["data"] = {**data, "proprio_identite": f"TEST-{next(_compteur):06d}"}
            return super().post(url, *a, **kw)

    return Client


def connecter(app, username):
    app.test_client_class = _avec_identite(type(app.test_client()))
    c = app.test_client()
    r = c.post("/connexion", data={"username": username, "password": MDP})
    assert r.status_code == 302
    return c


@pytest.fixture()
def login(app):
    return lambda email: connecter(app, email)


DONNEES_DOSSIER = {
    "proprio_nom": "Said",
    "proprio_prenom": "Ali",
    "ile_id": "1",
    "commune": "Moroni",
    "superficie": "450,5",
}


def creer_dossier(client):
    r = client.post("/dossiers/nouveau", data=DONNEES_DOSSIER)
    assert r.status_code == 302, r.data.decode()
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def agir(client, dossier_id, action, **data):
    return client.post(f"/dossiers/{dossier_id}/action/{action}", data=data, follow_redirects=True)
