import re

import pytest

from app import constants as C
from app.extensions import db
from app.models import Mutation

from test_mutations import ACTE, benef, derniere_mutation_id, dossier_certifie, proprio_id, saisir, vendre, vivant
from test_partages import cree, diviser, numero, valider


def lignes(page):
    """Numéros de dossier proposés dans le tableau d'une page."""
    return re.findall(r"SIGF-\d{4}-\d{5}", page)


def hypotheque(app, login, did, valider_aussi=True):
    saisir(login("dom"), vivant(app, did), "hypotheque", creancier_nom="Banque Test", montant="1000000")
    if valider_aussi:
        login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")


# ----------------------------------------------------------------- accès
def test_acces_a_la_page_mutations(app, login):
    for qui in ("dom", "dir", "admin", "pub"):
        assert login(qui).get("/mutations").status_code == 200, qui
    assert login("priv1").get("/mutations").status_code == 404                    # le topo privé n'y a pas accès
    assert app.test_client().get("/mutations").status_code == 302
    # menu : visible pour tous sauf le topographe privé
    assert "Mutations" in login("dom").get("/").data.decode()
    assert 'href="/mutations"' not in login("priv1").get("/").data.decode()


def test_seul_le_domaine_enregistre_et_voit_les_cartes(app, login):
    dom = login("dom").get("/mutations").data.decode()
    assert "Enregistrer une mutation" in dom
    for type_ in ("vente", "donation", "heritage", "expropriation", "hypotheque", "mainlevee", "division", "fusion", "rectification", "annulation"):
        assert f"/mutations/nouvelle?type={type_}" in dom, type_
    for qui in ("dir", "pub", "admin"):
        assert "Enregistrer une mutation" not in login(qui).get("/mutations").data.decode(), qui
        assert login(qui).get("/mutations/nouvelle?type=vente").status_code == 403
    assert login("priv1").get("/mutations/nouvelle?type=vente").status_code == 403
    assert login("dom").get("/mutations/nouvelle?type=inconnu").status_code == 404
    assert login("dom").get("/mutations/nouvelle").status_code == 404


# ----------------------------------------------------------------- liste et filtres
def test_liste_et_filtres(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))                        # validée : crée un nouveau dossier
    live = vivant(app, did)
    hypotheque(app, login, did, valider_aussi=False)                               # en attente (sur le dossier à jour)
    saisir(login("dom"), live, "rectification", notes="À rejeter")
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/rejeter", data={"motif": "Inutile"})

    dir_ = login("dir")
    tout = dir_.get("/mutations").data.decode()
    assert all(x in tout for x in ("Vente", "Hypothèque (inscription)", "Rectification", "Validée", "En attente", "Rejetée"))
    assert "Said Ali" in tout and "Moussa" in tout and "5 000 000 KMF" in tout.replace("\u202f", " ")
    assert numero(app, did) in tout and numero(app, live) in tout

    def n_lignes(url):
        return len(re.findall(r">(?:Ouvrir|Examiner)<", dir_.get(url).data.decode()))        # un bouton par ligne

    assert n_lignes("/mutations?type=vente") == 1
    assert n_lignes("/mutations?statut=en_attente") == 1 and "Examiner" in dir_.get("/mutations?statut=en_attente").data.decode()
    assert n_lignes("/mutations?statut=rejetee") == 1
    assert n_lignes("/mutations?q=Moussa") == 1                                    # par personne concernée
    assert n_lignes("/mutations?q=Banque") == 1                                    # par créancier
    assert n_lignes("/mutations?q=Acte notarié") == 3                              # par référence d'acte (commune aux 3 ici)
    assert n_lignes("/mutations?q=n°12") == 3 and n_lignes("/mutations?q=n°99") == 0
    assert n_lignes(f"/mutations?q={numero(app, did)}") == 1                       # la vente porte sur l'ancien dossier
    assert n_lignes(f"/mutations?q={numero(app, live)}") == 2                      # l'hypothèque et la rectification, sur le nouveau
    assert n_lignes("/mutations?q=introuvable") == 0 and "ne correspond à ces filtres" in dir_.get("/mutations?q=introuvable").data.decode()
    assert n_lignes("/mutations?du=2030-01-01") == 0 and n_lignes("/mutations?au=2030-01-01") == 3
    assert n_lignes("/mutations?du=pas-une-date&type=bidon&statut=bidon") == 3     # filtres invalides ignorés
    assert dir_.get("/mutations?page=999").status_code == 200 and dir_.get("/mutations?page=abc").status_code == 200


def test_pastille_et_alerte_pour_le_directeur(app, login):
    did = dossier_certifie(login)
    dire, dom = login("dir"), login("dom")
    assert 'badge text-bg-warning' not in dire.get("/").data.decode().split("Mutations")[1].split("</a>")[0]   # rien à valider
    hypotheque(app, login, did, valider_aussi=False)
    saisir(dom, did, "rectification", notes="encore une")
    accueil = dire.get("/").data.decode()
    assert re.search(r'Mutations</span>\s*<span class="badge text-bg-warning"[^>]*>2</span>', accueil)
    assert "2 mutations</strong> en attente de votre validation" in accueil and "statut=en_attente" in accueil
    assert "en attente de votre validation" not in dom.get("/").data.decode()          # réservé au Directeur
    assert not re.search(r'Mutations</span>\s*<span class="badge', dom.get("/").data.decode())


# ----------------------------------------------------------------- choisir le dossier
def test_choisir_ne_propose_que_les_dossiers_eligibles(app, login):
    certifie = cree(login, nom="Certifie", identite="C-1")
    ancien = login("dom").post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Ancien", "ile_id": "1", "commune": "Domoni", "reference_archive": "A-1990/7", "type_document": ["Titre foncier"],
        "libelle_document": [""], "fichiers": (__import__("io").BytesIO(b"%PDF-1.4 x"), "t.pdf")})
    id_ancien = int(ancien.headers["Location"].rstrip("/").split("/")[-1])
    en_cours = cree(login, nom="EnCours", identite="C-2", certifier=False)
    divise = cree(login, nom="Divise", identite="C-3")
    diviser(login("dom"), divise, [{"nom": "A"}, {"nom": "B"}])
    valider(login, derniere_mutation_id(app))                                       # divise est clôturé ; ses 2 filles sont en circuit

    page = login("dom").get("/mutations/nouvelle?type=vente").data.decode()
    propose = set(lignes(page))
    assert numero(app, certifie) in propose and numero(app, id_ancien) in propose
    assert numero(app, en_cours) not in propose and numero(app, divise) not in propose
    assert "ancien dossier · A-1990/7" in page


def test_choisir_recherche_et_liens_selon_le_type(app, login):
    a = cree(login, nom="Alpha", identite="C-1")
    b = cree(login, nom="Beta", identite="C-2")
    dom = login("dom")
    page = dom.get("/mutations/nouvelle?type=vente&q=Alpha").data.decode()
    assert numero(app, a) in page and numero(app, b) not in page                   # recherche par propriétaire
    assert f"/dossiers/{a}/mutations/nouvelle?type=vente" in page
    assert numero(app, b) in dom.get(f"/mutations/nouvelle?type=vente&q={numero(app, b)}").data.decode()
    assert f"/dossiers/{a}/mutations/nouvelle?type=heritage" in dom.get("/mutations/nouvelle?type=heritage&q=Alpha").data.decode()
    assert f"/dossiers/{a}/division/nouvelle" in dom.get("/mutations/nouvelle?type=division&q=Alpha").data.decode()
    assert f"/dossiers/{a}/fusion/nouvelle" in dom.get("/mutations/nouvelle?type=fusion&q=Alpha").data.decode()
    assert "Aucun dossier ne correspond" in dom.get("/mutations/nouvelle?type=vente&q=zzzz").data.decode()


def test_choisir_filtre_mainlevee_annulation_et_bloque_les_partages(app, login):
    avec_hyp = cree(login, nom="Hyp", identite="C-1")
    sans = cree(login, nom="Sans", identite="C-2")
    hypotheque(app, login, avec_hyp)
    dom = login("dom")

    assert lignes(dom.get("/mutations/nouvelle?type=mainlevee").data.decode()) == [numero(app, avec_hyp)] * len(lignes(dom.get("/mutations/nouvelle?type=mainlevee").data.decode()))
    assert numero(app, sans) not in dom.get("/mutations/nouvelle?type=mainlevee").data.decode()     # pas d'hypothèque à lever
    ann = dom.get("/mutations/nouvelle?type=annulation").data.decode()
    assert numero(app, avec_hyp) in ann and numero(app, sans) not in ann                              # rien à annuler sur « sans »

    div = dom.get("/mutations/nouvelle?type=division").data.decode()
    assert "Hypothèque active : faites d&#39;abord une mainlevée." in div or "Hypothèque active : faites d'abord une mainlevée." in div
    assert f"/dossiers/{avec_hyp}/division/nouvelle" not in div and f"/dossiers/{sans}/division/nouvelle" in div   # bloqué, pas de lien

    saisir(dom, sans, "rectification", notes="en attente")
    assert "mutation est en attente" in dom.get("/mutations/nouvelle?type=fusion").data.decode()


def test_le_formulaire_preselectionne_le_type(app, login):
    did = dossier_certifie(login)
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle?type=heritage").data.decode()
    assert re.search(r'<option value="heritage" selected>', page)
    assert not re.search(r'<option value="vente" selected>', page)
    assert not re.search(r'<option value="\w+" selected>', login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode())


# ----------------------------------------------------------------- parcours complet depuis le menu
def test_parcours_complet_depuis_le_menu(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    dom, dire = login("dom"), login("dir")

    # 1. le Domaine part de la page Mutations, choisit « Donation » puis son dossier
    choix = dom.get("/mutations/nouvelle?type=donation&q=Said").data.decode()
    assert f"/dossiers/{did}/mutations/nouvelle?type=donation" in choix
    # 2. formulaire présélectionné, il enregistre
    r = saisir(dom, did, "donation", **{f"cede_{said}": "1/2"}, **benef(("Nadia", "1/2")))
    assert r.status_code == 302
    # 3. la mutation apparaît dans la liste, en attente ; le Directeur est prévenu
    liste = dom.get("/mutations?statut=en_attente").data.decode()
    assert "Donation" in liste and numero(app, did) in liste and "Said Ali" in liste and "Nadia" in liste
    assert "1 mutation</strong> en attente" in dire.get("/").data.decode()
    # 4. le Directeur l'examine depuis la liste et la valide
    assert f"/dossiers/{did}#tracabilite" in dire.get("/mutations?statut=en_attente").data.decode()
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert "Validée" in dire.get("/mutations").data.decode() and "en attente de votre validation" not in dire.get("/").data.decode()
    # 5. le nouveau propriétaire est retrouvé par la recherche de dossier
    assert numero(app, vivant(app, did)) in dom.get("/mutations/nouvelle?type=vente&q=Nadia").data.decode()   # le nouveau dossier
