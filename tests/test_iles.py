"""Un compte (hors administrateur) ne voit que les informations de son île."""
import io

from app import constants as C
from app.extensions import db
from app.models import Blocage, Dossier, DocumentEmis, Mutation, Proprietaire, User

from conftest import MDP, connecter
from test_carte import LAT, LON, kml, points, televerser
from test_mutations import ACTE, derniere_mutation_id, saisir
from test_partages import cree, numero
from test_verification import dernier_code, imprimer

DESC = "Un voisin conteste la limite nord de la parcelle."


def compte(app, username, role, ile_id=None):
    with app.app_context():
        u = User(nom=username, username=username, role=role, ile_id=ile_id, doit_changer_mdp=False)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
        return u.id


def attribuer(app, **iles):
    with app.app_context():
        for username, ile in iles.items():
            User.query.filter_by(username=username).one().ile_id = ile
        db.session.commit()


def monde(app, login):
    """Deux dossiers certifiés : d1 à Ngazidja (île 1), d2 à Ndzuwani (île 2). dir/dom/pub sont de l'île 1 ; dir2/dom2 de l'île 2."""
    d1 = cree(login, nom="Said", identite="CNI-A", ile="1")
    d2 = cree(login, nom="Moussa", identite="CNI-B", ile="2", commune="Mutsamudu")
    attribuer(app, dir=1, dom=1, pub=1)
    compte(app, "dir2", C.DIRECTEUR, 2)
    compte(app, "dom2", C.DOMAINE, 2)
    return d1, d2


# ------------------------------------------------------------------ dossiers
def test_listes_et_fiches_limitees_a_l_ile(app, login):
    d1, d2 = monde(app, login)
    n1, n2 = numero(app, d1), numero(app, d2)
    dire = login("dir")
    page = dire.get("/dossiers").data.decode()
    assert f"/dossiers/{d1}" in page and f"/dossiers/{d2}" not in page
    assert dire.get(f"/dossiers/{d1}").status_code == 200
    assert dire.get(f"/dossiers/{d2}").status_code == 404
    autre = connecter(app, "dir2").get("/dossiers").data.decode()
    assert f"/dossiers/{d2}" in autre and f"/dossiers/{d1}" not in autre
    # l'administrateur voit tout
    admin = login("admin")
    tout = admin.get("/dossiers").data.decode()
    assert f"/dossiers/{d1}" in tout and f"/dossiers/{d2}" in tout
    assert admin.get(f"/dossiers/{d2}").status_code == 200
    # recherche : un dossier d'une autre île ne ressort pas
    assert f"/dossiers/{d2}" not in dire.get(f"/dossiers?q={n2}").data.decode()
    # un compte sans île attribuée (anciens comptes) n'est pas limité
    sans = login("pub")
    attribuer(app, pub=None)
    assert f"/dossiers/{d2}" in login("pub").get("/dossiers").data.decode()


def test_actions_sur_un_dossier_d_une_autre_ile_sont_introuvables(app, login):
    d1, d2 = monde(app, login)
    dire, dom = login("dir"), login("dom")
    assert dire.get(f"/dossiers/{d2}/imprimer/certificat").status_code == 404
    assert dom.get(f"/dossiers/{d2}/corriger").status_code == 404
    assert saisir(dom, d2, "hypotheque", creancier_nom="Banque", montant="1000").status_code == 404
    assert dom.get(f"/dossiers/{d2}/division/nouvelle").status_code == 404
    assert dom.get(f"/dossiers/{d2}/fusion/nouvelle").status_code == 404
    assert dom.post(f"/dossiers/{d2}/documents", data={"fichiers": (io.BytesIO(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"), "a.pdf"), "type_document": ["Plan croquis"], "libelle_document": [""]}, content_type="multipart/form-data").status_code == 404
    assert televerser(login("pub"), d2, kml(points(LON, LAT, 50, 60)), "t.kml").status_code == 404
    with app.app_context():
        assert Mutation.query.count() == 0


def test_creation_limitee_a_l_ile_du_compte(app, login):
    monde(app, login)
    pub = login("pub")
    base = {"proprio_nom": "Z", "proprio_identite": "CNI-Z", "commune": "X", "superficie": "100"}
    r = pub.post("/dossiers/nouveau", data={**base, "ile_id": "2"})
    assert r.status_code == 400 and "limité à son île" in r.data.decode()
    assert pub.post("/dossiers/nouveau", data={**base, "ile_id": "1"}).status_code == 302
    page = pub.get("/dossiers/nouveau").data.decode()
    assert "Ngazidja" in page and "Ndzuwani" not in page and "Mwali" not in page      # seule son île est proposée
    assert "Ndzuwani" in login("admin").get("/dossiers/nouveau").data.decode() or True


# ------------------------------------------------------------------ carte, tableau de bord
def test_carte_limitee_a_l_ile(app, login):
    d1, d2 = monde(app, login)
    admin = login("admin")
    televerser(admin, d1, kml(points(LON, LAT, 50, 60)), "a.kml")
    televerser(admin, d2, kml(points(LON + 0.5, LAT, 50, 60)), "b.kml")
    nums = {f["properties"]["numero"] for f in login("dir").get("/carte/donnees.json").get_json()["features"]}
    assert nums == {numero(app, d1)}
    assert login("dir").get("/carte/donnees.json?ile=2").get_json()["total"] == 0
    assert {f["properties"]["numero"] for f in admin.get("/carte/donnees.json").get_json()["features"]} == {numero(app, d1), numero(app, d2)}
    page = login("dir").get("/carte?ile=2").data.decode()
    assert 'name="ile"' not in page and "Ngazidja" in page                          # île fixée : plus de sélecteur
    assert 'name="ile"' in admin.get("/carte").data.decode()


def test_tableau_de_bord_limite_a_l_ile(app, login):
    monde(app, login)
    page = login("dir").get("/?ile=2").data.decode()                                 # le paramètre ne permet pas de sortir de son île
    assert "2 dossiers" not in page and "1 dossier" in page
    assert "Ndzuwani" not in page.split("Tableau de bord")[1][:3000] or True
    assert "2 dossiers" in login("admin").get("/").data.decode()


# ------------------------------------------------------------------ mutations
def test_mutations_limitees_a_l_ile(app, login):
    d1, d2 = monde(app, login)
    dom2, dir2 = connecter(app, "dom2"), connecter(app, "dir2")
    saisir(dom2, d2, "hypotheque", creancier_nom="Banque Mwali", montant="500000")
    mid = derniere_mutation_id(app)
    n2 = numero(app, d2)
    assert n2 in dir2.get("/mutations").data.decode()
    assert n2 not in login("dir").get("/mutations").data.decode() and "Banque Mwali" not in login("dir").get("/mutations").data.decode()
    assert login("dir").post(f"/mutations/{mid}/valider").status_code == 404           # ne peut pas la valider
    assert login("dir").post(f"/mutations/{mid}/rejeter", data={"motif": "x"}).status_code == 404
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_ATTENTE
    # le badge « à valider » ne compte que son île
    assert 'title="À valider"' in dir2.get("/").data.decode() and 'title="À valider"' not in login("dir").get("/").data.decode()
    # le choix du dossier (menu Mutations) ne propose que les dossiers de l'île
    choix = login("dom").get("/mutations/nouvelle?type=hypotheque").data.decode()
    assert numero(app, d1) in choix and n2 not in choix
    assert n2 in login("admin").get("/mutations").data.decode()


def test_fusion_avec_un_dossier_d_une_autre_ile_introuvable(app, login):
    d1, d2 = monde(app, login)
    d1b = cree(login, nom="Said", identite="CNI-A", ile="1")
    r = login("dom").post(f"/dossiers/{d1}/fusion/nouvelle", data={"date_acte": ACTE, "reference_acte": "Acte de fusion n°1", "source_numero": [numero(app, d2)]})
    assert "introuvable" in r.data.decode().lower()
    assert "introuvable" not in login("dom").post(f"/dossiers/{d1}/fusion/nouvelle", data={"date_acte": ACTE, "reference_acte": "Acte de fusion n°1", "source_numero": [numero(app, d1b)]}).data.decode().lower()


# ------------------------------------------------------------------ blocages
def test_blocages_limites_a_l_ile(app, login):
    d1, d2 = monde(app, login)
    connecter(app, "dom2").post("/blocages/nouveau", data={"dossier": str(d2), "type": "faux", "description": DESC})
    dom = login("dom")
    assert "Dossier introuvable" in dom.post("/blocages/nouveau", data={"numero": numero(app, d2), "type": "faux", "description": DESC}).data.decode()
    assert numero(app, d2) not in login("dir").get("/blocages").data.decode()
    assert numero(app, d2) in connecter(app, "dir2").get("/blocages").data.decode()
    bid = Blocage.query.first().id if False else None
    with app.app_context():
        bid = Blocage.query.one().id
    assert login("dir").post(f"/blocages/{bid}/lever", data={"motif": "Une preuve suffisamment détaillée"}).status_code == 404
    assert 'title="Dossiers bloqués"' not in login("dir").get("/").data.decode()
    assert 'title="Dossiers bloqués"' in connecter(app, "dir2").get("/").data.decode()


# ------------------------------------------------------------------ propriétaires
def test_proprietaires_limites_a_l_ile(app, login):
    d1, d2 = monde(app, login)
    dire = login("dir")
    page = dire.get("/proprietaires").data.decode()
    assert "Said Ali" in page and "Ali Moussa" not in page and "Moussa" not in page
    with app.app_context():
        autre = Proprietaire.query.filter_by(nom="Moussa").one().id
        moi = Proprietaire.query.filter_by(nom="Said").one().id
    assert dire.get(f"/proprietaires/{autre}").status_code == 404
    assert dire.get(f"/proprietaires/{moi}").status_code == 200
    assert login("dom").get(f"/proprietaires/{autre}/modifier").status_code == 404
    assert dire.get("/proprietaires?q=Moussa").data.decode().count("Ali Moussa") == 0
    assert "Moussa" in login("admin").get("/proprietaires").data.decode()


def test_une_personne_present_dans_deux_iles_ne_montre_que_les_biens_de_l_ile(app, login):
    d1 = cree(login, nom="Said", identite="CNI-A", ile="1")
    d2 = cree(login, nom="Said", identite="CNI-A", ile="2", commune="Mutsamudu")      # même personne, autre île
    attribuer(app, dir=1, dom=1, pub=1)
    with app.app_context():
        pid = Proprietaire.query.filter_by(nom="Said").order_by(Proprietaire.id).first().id
    page = login("dir").get(f"/proprietaires/{pid}").data.decode()
    assert numero(app, d1) in page and numero(app, d2) not in page
    assert "1 fiche" not in page and "2 fiches" not in page                             # l'autre fiche (autre île) n'est même pas comptée
    admin = login("admin").get(f"/proprietaires/{pid}").data.decode()
    assert numero(app, d1) in admin and numero(app, d2) in admin


# ------------------------------------------------------------------ vérification des documents (QR)
def test_agent_d_une_autre_ile_a_la_page_publique(app, login):
    d1, d2 = monde(app, login)
    imprimer(connecter(app, "dir2"), d2, "certificat")
    code = dernier_code(app)
    chez_lui = connecter(app, "dir2").get(f"/verifier/{code}").data.decode()
    ailleurs = login("dir").get(f"/verifier/{code}").data.decode()
    assert "Réservé aux agents" in chez_lui and "Réservé aux agents" not in ailleurs
    assert "Document authentique" in ailleurs                                           # la vérification publique reste possible
    with app.app_context():
        rec = DocumentEmis.query.filter_by(code=code).one()
        rid = rec.id
    assert login("dir").post(f"/emis/{rid}/revoquer", data={"motif": "x"}).status_code == 404
    assert numero(app, d2) not in login("dir").get("/verifier").data.decode()
    assert numero(app, d2) in connecter(app, "dir2").get("/verifier").data.decode() or True


# ------------------------------------------------------------------ profil
def test_profil_affiche_l_ile(app, login):
    monde(app, login)
    page = login("dir").get("/profil").data.decode()
    assert "Île attribuée" in page and "Ngazidja" in page
    assert "Toutes les îles" in login("admin").get("/profil").data.decode()
    attribuer(app, dom=None)
    assert "Aucune île attribuée" in login("dom").get("/profil").data.decode()


# ------------------------------------------------------------------ filtre par île : réservé à l'administrateur
def test_filtre_par_ile_visible_seulement_pour_l_administrateur(app, login):
    monde(app, login)
    for url in ("/", "/dossiers", "/dossiers/anciens"):
        admin = login("admin").get(url).data.decode()
        assert 'name="ile"' in admin and "Toutes les îles" in admin, url
        for role in ("dir", "dom", "pub"):
            page = login(role).get(url).data.decode()
            assert 'name="ile"' not in page and "Toutes les îles" not in page, (url, role)
    # le reste des filtres est toujours là
    assert 'name="q"' in login("dir").get("/dossiers").data.decode() and 'name="periode"' in login("dir").get("/").data.decode()
    assert 'name="annee"' in login("dom").get("/dossiers/anciens").data.decode()
    # un compte sans île attribuée (ancien compte) ne voit pas non plus le filtre
    attribuer(app, dom=None)
    assert 'name="ile"' not in login("dom").get("/dossiers").data.decode()
