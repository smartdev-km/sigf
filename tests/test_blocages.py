import io

import pytest

from app import constants as C
from app.extensions import db
from app.models import AuditLog, Blocage, Document, Dossier, ModificationPersonne, Mutation, Proprietaire

from conftest import agir, creer_dossier
from test_carte import LAT, LON, kml, points, televerser
from test_mutations import ACTE, benef, dossier_certifie, proprio_id, saisir
from test_partages import cree, numero
from test_verification import dernier_code, imprimer

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
DESC = "Un voisin conteste la limite nord de la parcelle."


def signaler(client, did, type_="limites_contestees", description=DESC):
    return client.post("/blocages/nouveau", data={"dossier": str(did), "type": type_, "description": description})


def lever(client, bid, motif="Jugement du 12/03 : limites confirmées", fichier=None, **kw):
    data = {"motif": motif, **kw}
    if fichier is not None:
        data["fichier"] = (io.BytesIO(fichier), "preuve.pdf")
    return client.post(f"/blocages/{bid}/lever", data=data, content_type="multipart/form-data")


def blocage_id(app):
    with app.app_context():
        return Blocage.query.order_by(Blocage.id.desc()).first().id


def bloque(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).est_bloque


# ------------------------------------------------------------------ signalement
def test_tous_les_roles_signalent_et_le_dossier_est_bloque(app, login):
    for role in ("admin", "dir", "dom", "pub"):
        did = dossier_certifie(login)
        r = signaler(login(role), did)
        assert r.status_code == 302 and bloque(app, did), role
    with app.app_context():
        assert Blocage.query.count() == 4 and AuditLog.query.filter_by(action="blocage_signale").count() == 4
        b = Blocage.query.first()
        assert (b.type, b.description, b.actif, b.signale_par.username) == ("limites_contestees", DESC, True, "admin")


def test_topo_prive_signale_seulement_ses_dossiers(app, login):
    p1, p2 = login("priv1"), login("priv2")
    did = creer_dossier(p1)
    assert signaler(p2, did).status_code == 400                 # dossier introuvable pour lui
    assert signaler(p1, did).status_code == 302 and bloque(app, did)


def test_validations_du_signalement(app, login):
    did = dossier_certifie(login)
    dom = login("dom")
    assert "Choisissez le type" in signaler(dom, did, type_="").data.decode()
    assert "Choisissez le type" in signaler(dom, did, type_="inconnu").data.decode()
    assert "obligatoire" in signaler(dom, did, description=" ").data.decode()
    assert "trop court" in signaler(dom, did, description="court").data.decode()
    assert "trop long" in signaler(dom, did, description="x" * 2001).data.decode()
    assert "introuvable" in dom.post("/blocages/nouveau", data={"numero": "SIGF-0000-00000", "type": "faux", "description": DESC}).data.decode()
    assert not bloque(app, did)


def test_signalement_par_numero(app, login):
    did = dossier_certifie(login)
    r = login("dom").post("/blocages/nouveau", data={"numero": numero(app, did), "type": "faux", "description": DESC})
    assert r.status_code == 302 and bloque(app, did)


def test_un_dossier_cloture_ne_se_signale_pas(app, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": "tout"}, **benef(("Moussa", "1")), montant="100")
    from test_mutations import derniere_mutation_id

    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert "clôturé" in signaler(login("dom"), did).data.decode()


def test_les_types_proposes(app, login):
    page = login("dom").get("/blocages/nouveau").data.decode()
    for libelle in ("Double vente ou double titre", "Soupçon de faux", "Usurpation d&#39;identité", "Chevauchement avec une autre parcelle", "Hypothèque contestée", "Autre problème"):
        assert libelle in page
    assert len(C.PROBLEMES) >= 24


# ------------------------------------------------------------------ le dossier est figé
def test_dossier_bloque_refuse_toutes_les_ecritures(app, login):
    did = dossier_certifie(login)
    pub, dom, dire, admin = login("pub"), login("dom"), login("dir"), login("admin")
    signaler(dom, did)
    # mutations
    assert saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000").status_code == 403
    # correction
    assert dom.post(f"/dossiers/{did}/corriger", data={"motif": "Faute de frappe sur le titre", "p_commune": "Autre"}).status_code == 403
    # impression
    for t in ("certificat", "extrait", "attestation", "plan_croquis"):
        assert dire.get(f"/dossiers/{did}/imprimer/{t}").status_code == 403
    # dépôt de document et de tracé
    assert dom.post(f"/dossiers/{did}/documents", data={"fichiers": (io.BytesIO(PDF), "a.pdf"), "type_document": ["Plan croquis"], "libelle_document": [""]}, content_type="multipart/form-data").status_code == 403
    assert televerser(pub, did, kml(points(LON, LAT, 50, 60)), "t.kml").status_code == 403
    # division / fusion
    assert dom.get(f"/dossiers/{did}/division").status_code in (403, 404)
    with app.app_context():
        assert Mutation.query.count() == 0 and Document.query.count() == 0


def test_dossier_en_circuit_bloque_ne_peut_pas_avancer(app, login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    signaler(dom, did)
    agir(pub, did, "terminer_plan")
    with app.app_context():
        assert db.session.get(Dossier, did).statut == C.PLAN_CROQUIS                       # n'a pas avancé
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "Dossier bloqué" in page and "terminer_plan" not in page
    lever(dire, blocage_id(app))
    agir(pub, did, "terminer_plan")
    with app.app_context():
        assert db.session.get(Dossier, did).statut == C.SAISIE_RSP


def test_mutation_en_attente_ne_se_valide_pas_tant_que_bloque(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000")
    from test_mutations import derniere_mutation_id

    mid = derniere_mutation_id(app)
    signaler(dom, did)
    dire.post(f"/mutations/{mid}/valider")
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_ATTENTE
    assert "bloqué" in dire.get(f"/dossiers/{did}").data.decode()
    lever(dire, blocage_id(app))
    dire.post(f"/mutations/{mid}/valider")
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_VALIDEE


def test_la_lecture_reste_possible(app, login):
    did = dossier_certifie(login)
    signaler(login("dom"), did)
    for role in ("admin", "dir", "dom", "pub"):
        assert login(role).get(f"/dossiers/{did}").status_code == 200
    assert login("dom").get("/carte/donnees.json").status_code == 200
    assert numero(app, did) in login("dir").get("/dossiers").data.decode()


def test_modification_d_une_personne_refusee_si_un_de_ses_dossiers_est_bloque(app, login):
    did = dossier_certifie(login)
    pid = proprio_id(app, did)
    signaler(login("dom"), did)
    r = login("dom").post(f"/proprietaires/{pid}/modifier", data={"nom": "Said Ahmed", "prenom": "Ali", "identite": "", "motif": "Faute de frappe"})
    assert r.status_code in (400, 302)
    with app.app_context():
        assert ModificationPersonne.query.count() == 0 and db.session.get(Proprietaire, pid).nom == "Said"


# ------------------------------------------------------------------ levée
def test_seul_le_directeur_leve_avec_une_preuve(app, login):
    did = dossier_certifie(login)
    signaler(login("dom"), did)
    bid = blocage_id(app)
    for role in ("dom", "admin", "pub", "priv1"):
        assert lever(login(role), bid).status_code in (403, 404)
    assert bloque(app, did)
    dire = login("dir")
    lever(dire, bid, motif=" ")
    lever(dire, bid, motif="court")
    assert bloque(app, did)
    assert lever(dire, bid).status_code == 302
    assert not bloque(app, did)
    with app.app_context():
        b = db.session.get(Blocage, bid)
        assert (b.actif, b.leve_par.username, b.motif_levee) == (False, "dir", "Jugement du 12/03 : limites confirmées")
        assert AuditLog.query.filter_by(action="blocage_leve").count() == 1
    lever(dire, bid, motif="Deuxième levée sur un blocage déjà levé")               # sans effet
    with app.app_context():
        assert db.session.get(Blocage, bid).motif_levee.startswith("Jugement")


def test_levee_avec_fichier_de_preuve(app, login):
    did = dossier_certifie(login)
    signaler(login("dom"), did)
    bid = blocage_id(app)
    lever(login("dir"), bid, fichier=PDF)
    with app.app_context():
        b = db.session.get(Blocage, bid)
        assert b.preuve is not None and b.preuve.nom_original == "preuve.pdf" and "Preuve de levée du blocage" in b.preuve.intitule
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "Historique des blocages" in page and "preuve.pdf" in page


def test_fichier_de_preuve_invalide_ne_leve_rien(app, login):
    did = dossier_certifie(login)
    signaler(login("dom"), did)
    bid = blocage_id(app)
    r = login("dir").post(f"/blocages/{bid}/lever", data={"motif": "Une preuve suffisamment détaillée", "fichier": (io.BytesIO(b"pas un pdf"), "x.pdf")}, content_type="multipart/form-data")
    assert bloque(app, did)
    with app.app_context():
        assert Document.query.count() == 0


def test_plusieurs_blocages_le_dossier_reste_bloque_jusqu_au_dernier(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    signaler(dom, did, "faux")
    signaler(dom, did, "double_vente")
    with app.app_context():
        premier, second = [b.id for b in Blocage.query.order_by(Blocage.id)]
    lever(dire, premier)
    assert bloque(app, did)
    assert "1 problème actif" in dire.get(f"/dossiers/{did}").data.decode()
    lever(dire, second)
    assert not bloque(app, did)
    assert saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000").status_code == 302


# ------------------------------------------------------------------ QR et pages
def test_document_imprime_devient_suspendu_a_la_verification(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    imprimer(dire, did, "certificat")
    code = dernier_code(app)
    public = app.test_client()
    assert "Document authentique" in public.get(f"/verifier/{code}").data.decode()
    signaler(login("dom"), did)
    page = public.get(f"/verifier/{code}").data.decode()
    assert "Document suspendu" in page and "examen" in page
    assert "limite" not in page and DESC not in page                              # le public ne voit jamais la nature du problème
    lever(dire, blocage_id(app))
    assert "Document authentique" in public.get(f"/verifier/{code}").data.decode()


def test_page_des_blocages_actifs_et_leves(app, login):
    d1, d2 = dossier_certifie(login), dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    signaler(dom, d1, "faux", "Signature manifestement imitée sur l'acte.")
    signaler(dom, d2, "doublon", "Même parcelle que le dossier précédent.")
    page = dire.get("/blocages").data.decode()
    assert "Signature manifestement imitée" in page and "Lever ce blocage" in page and numero(app, d1) in page
    assert "Lever ce blocage" not in dom.get("/blocages").data.decode()              # seul le Directeur voit le formulaire de levée
    assert "Signature" in dire.get("/blocages?q=signature").data.decode() and "Même parcelle" not in dire.get("/blocages?q=signature").data.decode()
    assert "Même parcelle" in dire.get("/blocages?type=doublon").data.decode()
    lever(dire, blocage_id(app) - 1)
    assert "Signature manifestement imitée" not in dire.get("/blocages").data.decode()
    leves = dire.get("/blocages?onglet=leves").data.decode()
    assert "Signature manifestement imitée" in leves and "Jugement du 12/03" in leves


def test_acces_aux_pages(app, login):
    did = dossier_certifie(login)
    for role in ("admin", "dir", "dom", "pub"):
        assert login(role).get("/blocages").status_code == 200
    assert login("priv1").get("/blocages").status_code == 404
    assert app.test_client().get("/blocages").status_code == 302


def test_badge_bloque_dans_les_listes_et_menu(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    assert "Bloqué" not in dire.get("/dossiers").data.decode()
    signaler(login("dom"), did)
    assert "Bloqué" in dire.get("/dossiers").data.decode()
    assert 'title="Dossiers bloqués"' in dire.get("/").data.decode()


def test_topo_prive_voit_un_bandeau_sans_detail(app, login):
    p1 = login("priv1")
    did = creer_dossier(p1)
    signaler(login("dom"), did, "complicite", "Soupçon sérieux sur un agent du service, ne pas divulguer.")
    page = p1.get(f"/dossiers/{did}").data.decode()
    assert "Dossier bloqué" in page and "suspendu" in page
    assert "Soupçon sérieux" not in page and "Complicité" not in page and "Historique des blocages" not in page


def test_description_echappee(app, login):
    did = dossier_certifie(login)
    signaler(login("dom"), did, "autre", "<script>alert(1)</script> contenu suspect")
    for url in (f"/dossiers/{did}", "/blocages"):
        assert "<script>alert(1)" not in login("dir").get(url).data.decode()
