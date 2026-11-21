from app import constants as C
from app.extensions import db
from app.models import AuditLog, Dossier

from conftest import agir, creer_dossier


def statut(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).statut


def test_filiere_publique_complete(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    assert statut(app, did) == C.PLAN_CROQUIS

    agir(pub, did, "terminer_plan")
    assert statut(app, did) == C.SAISIE_RSP
    agir(pub, did, "saisir_rsp", valeur="RTC-12")
    assert statut(app, did) == C.ENREGISTREMENT_RCP
    agir(dom, did, "saisir_rcp", valeur="RTS-34")
    assert statut(app, did) == C.VALIDATION_DIRECTEUR
    agir(dire, did, "valider")
    assert statut(app, did) == C.CERTIFIE
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert (d.rsp, d.rcp) == ("RTC-12", "RTS-34")
        assert d.valide_par.role == C.DIRECTEUR


def test_filiere_privee_et_sortie_du_topo_prive(app, login):
    priv, pub, dom = login("priv1"), login("pub"), login("dom")
    did = creer_dossier(priv)
    agir(priv, did, "terminer_plan")
    assert statut(app, did) == C.ATTENTE_AVIS

    # Un avis vide est refusé
    agir(priv, did, "envoyer_avis", commentaire="  ")
    assert statut(app, did) == C.ATTENTE_AVIS
    agir(priv, did, "envoyer_avis", commentaire="Avis favorable")
    assert statut(app, did) == C.SAISIE_RSP

    # Le topo privé ne peut plus agir, ni saisir le RTC
    agir(priv, did, "saisir_rsp", valeur="HACK")
    assert statut(app, did) == C.SAISIE_RSP
    # Le Domaine ne peut pas saisir le RTC, le topo public oui
    agir(dom, did, "saisir_rsp", valeur="HACK")
    assert statut(app, did) == C.SAISIE_RSP
    agir(pub, did, "saisir_rsp", valeur="RTC-1")
    assert statut(app, did) == C.ENREGISTREMENT_RCP


def test_topo_prive_ne_voit_que_ses_dossiers(app, login):
    p1, p2, pub = login("priv1"), login("priv2"), login("pub")
    d1 = creer_dossier(p1)
    d_pub = creer_dossier(pub)

    assert p2.get(f"/dossiers/{d1}").status_code == 404
    assert p1.get(f"/dossiers/{d_pub}").status_code == 404
    assert agir(p2, d1, "terminer_plan").status_code == 404
    assert statut(app, d1) == C.PLAN_CROQUIS
    assert d_pub is not None
    assert "SIGF-" not in p2.get("/dossiers").data.decode()


def test_topo_prive_vue_restreinte_apres_avis(app, login):
    priv, pub, dom = login("priv1"), login("pub"), login("dom")
    did = creer_dossier(priv)
    agir(priv, did, "terminer_plan")
    agir(priv, did, "envoyer_avis", commentaire="OK")
    agir(pub, did, "saisir_rsp", valeur="RTC-SECRET")
    agir(dom, did, "saisir_rcp", valeur="RTS-SECRET")

    page = priv.get(f"/dossiers/{did}").data.decode()
    assert "RTC-SECRET" not in page and "RTS-SECRET" not in page
    assert "Avis envoyé" in page
    assert "Certificat émis" not in page
    assert "Validation du Directeur" not in page  # statut réel masqué


def test_rejets_avec_motif(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")

    agir(dom, did, "rejeter", commentaire="")  # motif obligatoire
    assert statut(app, did) == C.ENREGISTREMENT_RCP
    agir(dom, did, "rejeter", commentaire="RTC illisible")
    assert statut(app, did) == C.SAISIE_RSP

    agir(pub, did, "saisir_rsp", valeur="R2")
    agir(dom, did, "saisir_rcp", valeur="C1")
    agir(dire, did, "rejeter", commentaire="Erreur de superficie")
    assert statut(app, did) == C.ENREGISTREMENT_RCP


def test_impression_seulement_si_rsp_et_rcp(app, login):
    from app.workflow import peut_imprimer

    pub, dom = login("pub"), login("dom")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    with app.app_context():
        assert not peut_imprimer(db.session.get(Dossier, did))
    agir(dom, did, "saisir_rcp", valeur="C1")
    with app.app_context():
        assert peut_imprimer(db.session.get(Dossier, did))


def test_correction_numeros_et_audit(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")

    # Avant validation : le topo public corrige son RTC
    pub.post(f"/dossiers/{did}/corriger/rsp", data={"valeur": "R1-bis"})
    # Mais pas le RTS, ni le Domaine le RTC
    pub.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "HACK"})
    dom.post(f"/dossiers/{did}/corriger/rsp", data={"valeur": "HACK"})
    agir(dire, did, "valider")

    # Après validation : seuls le Directeur peut corriger
    pub.post(f"/dossiers/{did}/corriger/rsp", data={"valeur": "R-TARD"})
    dom.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "C-TARD"})
    dire.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "C1-dir"})

    with app.app_context():
        d = db.session.get(Dossier, did)
        assert (d.rsp, d.rcp) == ("R1-bis", "C1-dir")
        actions = [a.action for a in AuditLog.query.filter(AuditLog.action.like("correction_%"))]
        assert actions == ["correction_rsp", "correction_rcp"]


def test_comptes_et_roles(app, login):
    dire, admin, pub = login("dir"), login("admin"), login("pub")
    assert pub.get("/utilisateurs/").status_code == 403

    # Le Directeur ne peut créer que des topographes privés
    r = dire.post("/utilisateurs/nouveau", data={"nom": "N", "username": "nouveau", "role": C.ADMIN, "password": "longmotdepasse1"})
    assert r.status_code == 400
    r = dire.post("/utilisateurs/nouveau", data={"nom": "N", "username": "nouveau", "role": C.TOPO_PRIVE, "ile_id": "1", "password": "longmotdepasse1"})
    assert r.status_code == 302
    # L'Administrateur peut créer un Domaine
    r = admin.post("/utilisateurs/nouveau", data={"nom": "D", "username": "domaine2", "role": C.DOMAINE, "ile_id": "1", "password": "longmotdepasse1"})
    assert r.status_code == 302

    # Le nouveau compte doit changer son mot de passe
    nouveau = pub.application.test_client()
    nouveau.post("/connexion", data={"username": "nouveau", "password": "longmotdepasse1"})
    r = nouveau.get("/")
    assert r.status_code == 302 and "/mot-de-passe" in r.headers["Location"]


def test_login_refuse_compte_desactive_et_redirection_sure(app):
    c = app.test_client()
    r = c.post("/connexion?next=//evil.com", data={"username": "pub", "password": "motdepasse-solide-1"})
    assert r.status_code == 302 and "evil.com" not in r.headers["Location"]
    with app.app_context():
        from app.models import User

        User.query.filter_by(username="dom").first().actif = False
        db.session.commit()
    r = app.test_client().post("/connexion", data={"username": "dom", "password": "motdepasse-solide-1"})
    assert r.status_code == 200  # reste sur la page de login


def test_pages_protegees(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/dossiers").status_code == 302


def test_menu_lateral_mobile(app, login):
    page = login("dir").get("/").data.decode()
    for attendu in ('id="btn-menu"', 'id="menu-fond"', 'id="menu-lateral"', 'aria-controls="menu-lateral"', "menu-ouvert"):
        assert attendu in page
    css = login("dir").get("/static/css/app.css").data.decode()
    assert "translateX(-100%)" in css and "left: 0" in css                       # tiroir collé au bord GAUCHE sur téléphone
    anonyme = app.test_client().get("/connexion").data.decode()
    assert 'id="btn-menu"' not in anonyme and "menu-ouvert" not in anonyme       # pas de menu sur la page de connexion


# ------------------------------------------------------------------ page « Dossiers » : un onglet par statut
def test_onglets_de_statut_dans_la_liste_des_dossiers(app, login):
    import re

    from test_partages import numero

    pub, dom, dire = login("pub"), login("dom"), login("dir")
    e1 = creer_dossier(pub)                                                              # plan croquis
    e2 = creer_dossier(pub)
    agir(pub, e2, "terminer_plan")                                                       # saisie du RTC
    e3 = creer_dossier(pub)
    agir(pub, e3, "terminer_plan")
    agir(pub, e3, "saisir_rsp", valeur="R")
    agir(dom, e3, "saisir_rcp", valeur="C")
    agir(dire, e3, "valider")                                                            # certifié

    def onglets(page):
        return re.findall(r'<a class="nav-link[^"]*" href="/dossiers\?[^"]*">\s*(?:<span[^>]*></span>)?([^<]+?)\s*<span', page.split("onglets-statuts")[1].split("</ul>")[0])

    def dans_la_liste(page, did):
        return numero(app, did) in page.split("<tbody>")[1]

    page = dire.get("/dossiers").data.decode()
    assert onglets(page) == ["Plan croquis", "Avis", "Saisie du RTC", "Domaine (RTS)", "Validation", "Certificat", "Clôturé"]   # pas d'onglet « Tous »
    assert "Tous" not in page.split("onglets-statuts")[1].split("</ul>")[0] and 'name="statut"' not in page and "Tous les statuts" not in page
    # sans onglet choisi : le premier onglet qui contient des dossiers
    assert dans_la_liste(page, e1) and not dans_la_liste(page, e2) and not dans_la_liste(page, e3)
    assert page.count('nav-link active') >= 2 and 'text-bg-primary ms-1">1</span>' in page                  # (le menu latéral a aussi son « active »)
    for statut, did, autres in (("saisie_rsp", e2, (e1, e3)), ("certifie", e3, (e1, e2)), ("plan_croquis", e1, (e2, e3))):
        p = dire.get(f"/dossiers?statut={statut}").data.decode()
        assert dans_la_liste(p, did) and not any(dans_la_liste(p, x) for x in autres), statut
    vide = dire.get("/dossiers?statut=attente_avis").data.decode()
    assert "Aucun dossier dans « Avis »" in vide
    # la recherche ouvre le premier onglet qui contient des résultats, et les compteurs suivent la recherche
    p = dire.get(f"/dossiers?q={numero(app, e3)}").data.decode()
    assert dans_la_liste(p, e3) and not dans_la_liste(p, e1) and 'text-bg-primary ms-1">1</span>' in p
    assert "Aucun dossier" in dire.get("/dossiers?q=introuvable-xyz").data.decode()
    assert dire.get("/dossiers?statut=n-importe-quoi").status_code == 200 and dire.get("/dossiers?statut=archive").status_code == 200   # onglet inconnu : retour au premier onglet utile

def test_la_liste_ouvre_le_premier_onglet_utile(app, login):
    """Quand il n'y a que des dossiers certifiés, la page s'ouvre directement sur « Certificat » (pas sur un onglet vide)."""
    from test_partages import cree, numero

    did = cree(login, nom="Said", identite="CNI-1")                                      # certifié
    page = login("dir").get("/dossiers").data.decode()
    assert numero(app, did) in page.split("<tbody>")[1]
    actif = [a for a in page.split("onglets-statuts")[1].split("</ul>")[0].split("<li") if 'nav-link active' in a]
    assert len(actif) == 1 and "Certificat" in actif[0]
    # aucun résultat : on reste sur l'onglet demandé, vide
    assert "Aucun dossier dans « Plan croquis »" in login("admin").get("/dossiers?q=zzzz-introuvable&statut=plan_croquis").data.decode()


def test_onglets_absents_pour_le_topographe_prive(app, login):
    priv = login("priv1")
    creer_dossier(priv)
    page = priv.get("/dossiers").data.decode()
    assert "onglets-statuts" not in page and "Certificat émis" not in page               # pas d'onglets : ils révéleraient l'avancement après son avis
    assert priv.get("/dossiers?statut=certifie").status_code == 200
