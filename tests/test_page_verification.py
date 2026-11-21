import re

from app import constants as C
from app.extensions import db
from app.models import DocumentEmis, User

from test_verification import dernier_code, dossier_pret, imprimer


def tout_voir(app):
    with app.app_context():
        return [r.code for r in DocumentEmis.query.order_by(DocumentEmis.id)]


# ----------------------------------------------------------------- menu
def test_entree_de_menu_pour_tous_les_roles_connectes(app, login):
    for qui in ("dir", "dom", "pub", "admin", "priv1"):
        page = login(qui).get("/").data.decode()
        assert 'href="/verifier"' in page and "Vérifier un document" in page, qui
    page = login("dom").get("/verifier").data.decode()
    assert re.search(r'nav-link\s+active"\s+href="/verifier"', page)               # l'entrée est surlignée sur sa page
    assert "Vérification d'un document" in page


def test_page_integree_a_l_application_une_fois_connecte(app, login):
    connecte = login("dom").get("/verifier").data.decode()
    assert 'class="sidebar' in connecte and "Déconnexion" in connecte             # dans la mise en page de l'application
    public = app.test_client().get("/verifier").data.decode()
    assert 'class="sidebar' not in public and "Vérification d'un document" in public   # le public voit la page seule
    assert 'name="code"' in connecte and 'name="code"' in public


# ----------------------------------------------------------------- dernières vérifications
def test_dernieres_verifications_visibles_seulement_par_les_agents(app, login):
    did = dossier_pret(login)
    dire = login("dir")
    for _ in range(2):
        imprimer(dire, did)
    codes = tout_voir(app)
    anonyme = app.test_client()
    assert "Aucun document vérifié" in login("dom").get("/verifier").data.decode()          # rien de vérifié pour l'instant

    anonyme.get(f"/verifier/{codes[0]}")
    anonyme.get(f"/verifier/{codes[1]}")
    anonyme.get(f"/verifier/{codes[0]}")                                                   # le premier est vérifié en dernier
    with app.app_context():
        numero = db.session.get(DocumentEmis, 1).dossier.numero

    page = login("dom").get("/verifier").data.decode()
    assert "Dernières vérifications" in page and numero in page
    assert page.index(f"copie n° 1") < page.index("copie n° 2")                            # la plus récemment vérifiée d'abord
    assert 'text-bg-success">Valide' in page
    assert re.search(r'<td class="text-end text-muted[^"]*">2</td>', page)                    # compteur du premier : 2 vérifications

    # ni le public ni le topographe privé ne voient cet historique
    for page in (anonyme.get("/verifier").data.decode(), login("priv1").get("/verifier").data.decode()):
        assert "Dernières vérifications" not in page and numero not in page


def test_les_etats_apparaissent_dans_l_historique(app, login):
    did = dossier_pret(login, "certifie")
    dire = login("dir")
    for t in ("plan_croquis", "certificat"):
        imprimer(dire, did, t)
    codes = tout_voir(app)
    with app.app_context():
        ids = [r.id for r in DocumentEmis.query.order_by(DocumentEmis.id)]
    dire.post(f"/emis/{ids[0]}/revoquer", data={"motif": "Perdu"})
    dire.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "RTS-NOUVEAU"})              # le certificat devient « non à jour »
    anonyme = app.test_client()
    for c in codes:
        anonyme.get(f"/verifier/{c}")
    page = login("dom").get("/verifier").data.decode()
    assert 'text-bg-danger">Révoqué' in page and 'text-bg-warning">Non à jour' in page


# ----------------------------------------------------------------- résultat : fiche agent
def test_fiche_agent_sur_le_resultat(app, login):
    did = dossier_pret(login)
    imprimer(login("dir"), did)
    code = dernier_code(app)
    with app.app_context():
        numero = DocumentEmis.query.one().dossier.numero
    for qui in ("dir", "dom", "pub", "admin"):
        page = login(qui).get(f"/verifier/{code}").data.decode()
        assert "Réservé aux agents" in page, qui
        assert f"Ouvrir {numero}" in page and f"/dossiers/{did}#tracabilite" in page
        assert "Imprimé par" in page and "dir" in page                                      # l'émetteur (compte « dir »)
        assert "Document authentique" in page                                               # le résultat public reste affiché
    # le public et le topographe privé ont la page « QR » ordinaire : aucun détail interne
    for client in (app.test_client(), login("priv1")):
        page = client.get(f"/verifier/{code}").data.decode()
        assert "Réservé aux agents" not in page and "Imprimé par" not in page and "Ouvrir SIGF" not in page
        assert "Document authentique" in page


def test_revocation_depuis_la_page_de_verification(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    for qui, doit_voir in (("dir", True), ("admin", True), ("dom", False), ("pub", False)):
        page = login(qui).get(f"/verifier/{code}").data.decode()
        assert ("Révoquer cet exemplaire" in page) is doit_voir, qui
    assert "Révoquer cet exemplaire" not in app.test_client().get(f"/verifier/{code}").data.decode()

    with app.app_context():
        rec_id = DocumentEmis.query.one().id
    r = login("dir").post(f"/emis/{rec_id}/revoquer", data={"motif": "Copie perdue"})
    assert r.status_code == 302
    page = login("dir").get(f"/verifier/{code}").data.decode()
    assert "Document révoqué" in page and "Copie perdue" in page and "Révoquer cet exemplaire" not in page
    # le public voit « révoqué » mais jamais le motif ni l'agent qui l'a décidé
    public = app.test_client().get(f"/verifier/{code}").data.decode()
    assert "Document révoqué" in public and "Copie perdue" not in public


def test_code_inconnu_n_expose_rien_aux_agents(app, login):
    page = login("dir").get("/verifier/AAAAAAAAAAAAAAAAAAAAAAAA")
    assert page.status_code == 404
    corps = page.data.decode()
    assert "Code inconnu ou invalide" in corps and "Réservé aux agents" not in corps and "Dernières vérifications" not in corps


def test_noms_d_agents_echappes(app, login):
    did = dossier_pret(login)
    with app.app_context():
        User.query.filter_by(username="dir").one().nom = '<img src=x onerror=alert(1)>'
        db.session.commit()
    imprimer(login("dir"), did)
    page = login("dom").get(f"/verifier/{dernier_code(app)}").data.decode()
    assert "<img src=x" not in page and "&lt;img src=x" in page


def test_saisie_manuelle_depuis_la_page_integree(app, login):
    did = dossier_pret(login)
    imprimer(login("dir"), did)
    code = dernier_code(app)
    from app import verification as V

    dom = login("dom")
    r = dom.get(f"/verifier?code={V.formater(code).lower()}", follow_redirects=True)       # saisie avec tirets et minuscules
    assert "Document authentique" in r.data.decode() and "Réservé aux agents" in r.data.decode()
    r = dom.get("/verifier?code=pas-un-code", follow_redirects=True)
    assert r.status_code == 404 and "Code inconnu ou invalide" in r.data.decode()


# ----------------------------------------------------------------- présentation
def test_en_tete_officiel_pour_le_public_titre_simple_pour_les_agents(app, login):
    public = app.test_client().get("/verifier").data.decode()
    assert "verif-public" in public and "UNION DES COMORES" in public and "verif-embleme" in public
    agent = login("dom").get("/verifier").data.decode()
    assert "verif-app" in agent and "UNION DES COMORES" not in agent and "verif-embleme" not in agent
    assert "Contrôlez qu&#39;un certificat" in agent or "Contrôlez qu'un certificat" in agent


def test_aide_visuelle_et_saisie_assistee_sur_l_accueil(app, login):
    for page in (app.test_client().get("/verifier").data.decode(), login("dom").get("/verifier").data.decode()):
        assert 'class="aide-code' in page and "sous le QR code" in page            # où trouver le code
        assert 'id="code-verif"' in page and 'autocapitalize="characters"' in page   # saisie adaptée au téléphone
        assert "replace(/[^A-Z2-7]/g" in page                                          # mise en forme automatique du code
    resultat = app.test_client().get("/verifier/AAAAAAAAAAAAAAAAAAAAAAAA").data.decode()
    assert 'id="code-verif"' not in resultat or "verdict-inconnu" in resultat


def test_chaque_verdict_a_sa_presentation(app, login):
    did = dossier_pret(login, "certifie")
    dire, anonyme = login("dir"), app.test_client()
    for t in ("plan_croquis", "certificat", "attestation", "extrait"):
        imprimer(dire, did, t)
    codes = tout_voir(app)
    with app.app_context():
        ids = [r.id for r in DocumentEmis.query.order_by(DocumentEmis.id)]

    page = anonyme.get(f"/verifier/{codes[0]}").data.decode()                     # valide
    assert "verdict-valide" in page and "champ-large" in page and "Sans valeur" not in page
    assert page.index("Propriétaire(s)") < page.index("Dossier</div>")           # le propriétaire passe avant le reste

    dire.post(f"/emis/{ids[1]}/revoquer", data={"motif": "Perdu"})                # révoqué
    page = anonyme.get(f"/verifier/{codes[1]}").data.decode()
    assert "verdict-revoque" in page and "fiche-sans-valeur" in page and "Sans valeur" in page

    dire.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "RTS-NOUVEAU"})    # non à jour
    page = anonyme.get(f"/verifier/{codes[2]}").data.decode()
    assert "verdict-obsolete" in page and "Au jour de l&#39;émission" in page.replace("l'émission", "l&#39;émission")
    assert "Sans valeur" not in page

    with app.app_context():                                                       # altéré
        rec = db.session.get(DocumentEmis, ids[3])
        rec.snapshot = rec.snapshot.replace("RTC-777", "RTC-000")
        db.session.commit()
    page = anonyme.get(f"/verifier/{codes[3]}").data.decode()
    assert "verdict-altere" in page and "RTC-000" not in page and "champ-large" not in page   # aucune donnée douteuse affichée

    page = anonyme.get("/verifier/AAAAAAAAAAAAAAAAAAAAAAAA").data.decode()
    assert "verdict-inconnu" in page and "Code inconnu ou invalide" in page


def test_panneau_agent_seulement_pour_les_agents(app, login):
    did = dossier_pret(login)
    imprimer(login("dir"), did)
    code = dernier_code(app)
    assert "panneau-agent" in login("dom").get(f"/verifier/{code}").data.decode()
    for page in (app.test_client().get(f"/verifier/{code}").data.decode(), login("priv1").get(f"/verifier/{code}").data.decode()):
        assert "panneau-agent" not in page and "Réservé aux agents" not in page
