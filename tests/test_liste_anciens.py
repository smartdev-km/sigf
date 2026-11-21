import io
import re

from app import constants as C
from app.extensions import db
from app.models import Dossier

from test_mutations import benef, derniere_mutation_id, proprio_id, saisir
from test_partages import cree, diviser, numero, valider

PDF = b"%PDF-1.4 x"


def importer(client, nom="Vieux", ile="1", commune="Domoni", ref="A-1990/7", annee="1990", nb_scans=1):
    types = ["Titre foncier"] * nb_scans
    fichiers = [(io.BytesIO(PDF), f"scan{i}.pdf") for i in range(nb_scans)]
    r = client.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": nom, "ile_id": ile, "commune": commune, "reference_archive": ref, "annee": annee,
        "type_document": types, "libelle_document": [""] * nb_scans, "fichiers": fichiers})
    assert r.status_code == 302, r.data.decode()[:300]
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def numeros(page):
    return re.findall(r"SIGF-\d{4}-\d{5}", page)


# ----------------------------------------------------------------- séparation des deux listes
def test_dossiers_n_affiche_que_les_nouveaux(app, login):
    dom, dire = login("dom"), login("dir")
    ancien = importer(dom, nom="Ancienne")
    nouveau = cree(login, nom="Nouveau", identite="N-1")
    page = dire.get("/dossiers").data.decode()
    assert numero(app, nouveau) in page and numero(app, ancien) not in page
    # même en cherchant précisément un ancien dossier, ou en forçant l'ancien filtre d'origine
    recherche = dire.get(f"/dossiers?q={numero(app, ancien)}").data.decode()
    assert numero(app, ancien) not in recherche.replace(f'value="{numero(app, ancien)}"', "").replace(f"q={numero(app, ancien)}", "")   # (le champ et les onglets répètent seulement la saisie)
    assert numero(app, ancien) not in dire.get("/dossiers?origine=ancien").data.decode()
    assert numero(app, ancien) not in dire.get("/dossiers?origine=tous").data.decode()
    # plus de sélecteur d'origine, et plus de statut « Ancien dossier » dans la liste des nouveaux
    assert 'name="origine"' not in page and "Ancien dossier (numérisé)" not in page and "Anciens (scannés)" not in page
    assert dire.get("/dossiers?statut=archive").status_code == 200 and numero(app, ancien) not in dire.get("/dossiers?statut=archive").data.decode()


def test_page_des_anciens_dossiers(app, login):
    dom, dire = login("dom"), login("dir")
    ancien = importer(dom, nom="Ancienne")
    nouveau = cree(login, nom="Nouveau", identite="N-1")
    page = dire.get("/dossiers/anciens").data.decode()
    assert "Anciens dossiers" in page and numero(app, ancien) in page and numero(app, nouveau) not in page
    assert "A-1990/7" in page and "1990" in page and "Domoni" in page and "En vigueur" in page
    assert "1 dossier papier numérisé" in page


def test_bouton_ajouter_un_ancien_dossier(app, login):
    for qui in ("dom", "dir", "admin"):
        page = login(qui).get("/dossiers/anciens").data.decode()
        assert "Ajouter un ancien dossier" in page and 'href="/dossiers/ancien/nouveau"' in page, qui
    # le topographe public consulte la liste mais ne peut pas ajouter ; le topographe privé n'y a pas accès
    pub = login("pub").get("/dossiers/anciens").data.decode()
    assert "Ajouter un ancien dossier" not in pub and "Anciens dossiers" in pub
    assert login("priv1").get("/dossiers/anciens").status_code == 404
    assert app.test_client().get("/dossiers/anciens").status_code == 302
    assert login("pub").get("/dossiers/ancien/nouveau").status_code == 403


def test_etat_vide_propose_d_ajouter(app, login):
    page = login("dom").get("/dossiers/anciens").data.decode()
    assert "Aucun ancien dossier pour le moment" in page and "Ajouter le premier ancien dossier" in page
    page = login("pub").get("/dossiers/anciens").data.decode()
    assert "Aucun ancien dossier pour le moment" in page and "Ajouter le premier" not in page
    importer(login("dom"))
    assert "Aucun ancien dossier ne correspond" in login("dom").get("/dossiers/anciens?q=introuvable").data.decode()


# ----------------------------------------------------------------- filtres
def test_filtres_de_la_page_des_anciens(app, login):
    dom, dire = login("dom"), login("dir")
    a = importer(dom, nom="Alpha", ile="1", commune="Moroni", ref="A-1980/1", annee="1980")
    b = importer(dom, nom="Beta", ile="2", commune="Mutsamudu", ref="B-1995/2", annee="1995")
    c = importer(dom, nom="Gamma", ile="3", commune="Fomboni", ref="C-2001/3", annee="2001")

    def trouves(url):
        return set(numeros(dire.get(url).data.decode()))

    na, nb, nc = numero(app, a), numero(app, b), numero(app, c)
    assert trouves("/dossiers/anciens") == {na, nb, nc}
    assert trouves("/dossiers/anciens?ile=2") == {nb}
    assert trouves("/dossiers/anciens?annee=2001") == {nc}
    assert trouves("/dossiers/anciens?q=B-1995") == {nb} and trouves("/dossiers/anciens?q=Alpha") == {na}
    assert trouves("/dossiers/anciens?q=Fomboni&ile=3") == {nc} and trouves("/dossiers/anciens?q=Fomboni&ile=1") == set()
    assert trouves("/dossiers/anciens?annee=abc&ile=zzz&statut=bidon") == {na, nb, nc}      # filtres invalides ignorés
    assert dire.get("/dossiers/anciens?page=999").status_code == 200


def test_pagination_des_anciens(app, login):
    dom = login("dom")
    for i in range(21):
        importer(dom, nom=f"P{i:02d}", ref=f"R-{i}")
    page1 = login("dir").get("/dossiers/anciens").data.decode()
    assert len(set(numeros(page1))) == 20 and "?page=2" in page1
    page2 = login("dir").get("/dossiers/anciens?page=2").data.decode()
    assert len(set(numeros(page2))) == 1 and set(numeros(page1)).isdisjoint(numeros(page2))
    assert "21 dossiers papier numérisés" in page1


def test_nombre_de_scans_et_proprietaire_actuel(app, login):
    dom, dire = login("dom"), login("dir")
    did = importer(dom, nom="Vendeur", nb_scans=3)
    assert re.search(r"bi-paperclip text-muted\"></i> 3", dire.get("/dossiers/anciens").data.decode())
    # après une vente validée, la liste affiche le propriétaire ACTUEL
    saisir(dom, did, "vente", **{f"cede_{proprio_id(app, did, 'Vendeur')}": "tout"}, **benef(("Acheteur", "1")))
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    # l'ancien dossier est clôturé et garde son propriétaire d'AVANT ; le dossier créé par la vente (au nom de l'acheteur)
    # est un dossier numérique : il apparaît dans « Dossiers »
    anciens = dire.get("/dossiers/anciens").data.decode()
    assert "Vendeur" in anciens and "Acheteur" not in anciens and "Clôturé" in anciens
    nouveaux = dire.get("/dossiers").data.decode()
    assert "Acheteur" in nouveaux and "Vendeur" not in nouveaux


def test_un_ancien_dossier_divise_reste_dans_les_anciens_et_ses_filles_vont_dans_dossiers(app, login):
    dom, dire = login("dom"), login("dir")
    mere = importer(dom, nom="Mere")
    diviser(dom, mere, [{"nom": "Lot A"}, {"nom": "Lot B"}])
    valider(login, derniere_mutation_id(app))
    anciens = dire.get("/dossiers/anciens").data.decode()
    assert numero(app, mere) in anciens and "Clôturé" in anciens                                  # la mère clôturée y reste, marquée
    assert numeros(anciens) == [numero(app, mere)]                                                 # les filles n'y sont pas
    nouveaux = set(numeros(dire.get("/dossiers").data.decode()))
    assert numero(app, mere) not in nouveaux and len(nouveaux) == 2                                # les 2 filles, dans « Dossiers »
    assert numeros(dire.get("/dossiers/anciens?statut=cloture").data.decode()) == [numero(app, mere)]
    assert numeros(dire.get("/dossiers/anciens?statut=archive").data.decode()) == []


# ----------------------------------------------------------------- menu et navigation
def test_menu_et_navigation(app, login):
    dom = login("dom")
    ancien = importer(dom, nom="Ancienne")
    nouveau = cree(login, nom="Nouveau", identite="N-1")

    def actifs(page):
        menu = page.split('<nav class="sidebar')[1].split("</nav>")[0]                      # le menu latéral (les onglets de la page ont aussi des « nav-link active »)
        return re.findall(r'nav-link\s+active"\s+href="([^"]+)"', menu)

    assert actifs(dom.get("/dossiers").data.decode()) == ["/dossiers"]
    assert actifs(dom.get("/dossiers/anciens").data.decode()) == ["/dossiers/anciens"]
    assert actifs(dom.get("/dossiers/ancien/nouveau").data.decode()) == ["/dossiers/anciens"]       # le formulaire d'ajout reste dans « Anciens dossiers »
    assert actifs(dom.get(f"/dossiers/{ancien}").data.decode()) == ["/dossiers/anciens"]            # la fiche d'un ancien dossier aussi
    assert actifs(dom.get(f"/dossiers/{nouveau}").data.decode()) == ["/dossiers"]                    # et celle d'un nouveau dans « Dossiers »
    page = dom.get("/").data.decode()
    assert 'href="/dossiers/anciens"' in page and "Anciens dossiers" in page
    assert page.index(">Dossiers<") < page.index("Anciens dossiers") < page.index("Mutations")       # rangé juste sous « Dossiers »
    assert 'href="/dossiers/anciens"' not in login("priv1").get("/").data.decode()                   # absent pour le topographe privé
    menu = page.split('<nav class="sidebar')[1].split("</nav>")[0]
    assert "Ancien dossier</a>" not in menu and "Anciens dossiers</a>" in menu                        # l'ancienne entrée (formulaire direct) a disparu


def test_liens_de_retour(app, login):
    dom = login("dom")
    ancien = importer(dom)
    nouveau = cree(login, nom="Nouveau", identite="N-1")
    assert "← Anciens dossiers" in dom.get(f"/dossiers/{ancien}").data.decode()
    assert "← Dossiers" in dom.get(f"/dossiers/{nouveau}").data.decode()
    annuler = re.search(r'<a class="btn btn-link" href="([^"]+)">Annuler</a>', dom.get("/dossiers/ancien/nouveau").data.decode()).group(1)
    assert annuler == "/dossiers/anciens"
    annuler = re.search(r'<a class="btn btn-link" href="([^"]+)">Annuler</a>', dom.get("/dossiers/nouveau").data.decode())
    assert annuler is None or annuler.group(1) == "/dossiers"
