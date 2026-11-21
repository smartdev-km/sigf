import re
from io import BytesIO

from pypdf import PdfReader

from app import verification as V
from app.extensions import db
from app.models import AuditLog, DocumentEmis, Dossier

from conftest import agir, creer_dossier


def dossier_pret(login, jusqua="rcp"):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="RTC-777")
    agir(dom, did, "saisir_rcp", valeur="RTS-888")
    if jusqua == "certifie":
        agir(dire, did, "valider")
    return did


def imprimer(client, did, type_doc="plan_croquis"):
    r = client.get(f"/dossiers/{did}/imprimer/{type_doc}")
    assert r.status_code == 200
    return r


def dernier_code(app):
    with app.app_context():
        return DocumentEmis.query.order_by(DocumentEmis.id.desc()).first().code


# ------------------------------------------------------------------ émission
def test_impression_emet_un_exemplaire_signe_et_imprime_le_code(app, login):
    did = dossier_pret(login)
    r = imprimer(login("pub"), did)
    code = dernier_code(app)
    texte = "\n".join(p.extract_text() for p in PdfReader(BytesIO(r.data)).pages)
    assert V.formater(code)[:14] in texte and "copie n° 1" in texte
    with app.app_context():
        rec = DocumentEmis.query.one()
        assert (rec.type, rec.copie_n, rec.version_signature) == ("plan_croquis", 1, 1)
        assert len(rec.signature) == 64 and rec.signature == V.signer(rec)
        assert V.code_valide(rec.code) and len(rec.code) == 24
        assert '"rsp":"RTC-777"' in rec.snapshot


def test_chaque_impression_a_son_propre_code_et_numero_de_copie(app, login):
    did = dossier_pret(login)
    pub = login("pub")
    for _ in range(3):
        imprimer(pub, did)
    with app.app_context():
        recs = DocumentEmis.query.order_by(DocumentEmis.id).all()
        assert [r.copie_n for r in recs] == [1, 2, 3]
        assert len({r.code for r in recs}) == 3


def test_le_qr_pointe_vers_l_adresse_publique_configuree(app, login):
    app.config["PUBLIC_BASE_URL"] = "https://sigf.exemple.km"
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    with app.test_request_context():
        assert V.url_verification(code) == f"https://sigf.exemple.km/verifier/{code}"


def test_le_qr_ne_contient_aucune_donnee_personnelle(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    with app.test_request_context():
        url = V.url_verification(dernier_code(app))
    for secret in ("Said", "Ali", "RTC", "RTS", "Moroni"):
        assert secret not in url


# ------------------------------------------------------------------ vérification publique
def test_verification_publique_sans_connexion(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    anonyme = app.test_client()
    r = anonyme.get(f"/verifier/{code}")
    page = r.data.decode()
    assert r.status_code == 200 and "Document authentique" in page
    assert "Said Ali" in page and "RTC-777" in page and "RTS-888" in page and "Moroni" in page
    assert r.headers["Cache-Control"] == "no-store" and "noindex" in r.headers["X-Robots-Tag"]
    # saisie manuelle : minuscules, tirets et espaces tolérés
    manuel = V.formater(code).lower().replace("-", " - ")
    r = anonyme.get(f"/verifier?code={manuel}", follow_redirects=True)
    assert "Document authentique" in r.data.decode()


def test_verification_ne_divulgue_pas_les_donnees_sensibles(app, login):
    pub = login("pub")
    r = pub.post("/dossiers/nouveau", data={"proprio_nom": "Said", "proprio_prenom": "Ali", "proprio_identite": "CNI-SECRET-99",
                                            "proprio_telephone": "+269 333 00 00", "proprio_adresse": "Rue privée 12",
                                            "ile_id": "1", "commune": "Moroni"})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(login("dom"), did, "saisir_rcp", valeur="C1")
    imprimer(pub, did)
    page = app.test_client().get(f"/verifier/{dernier_code(app)}").data.decode()
    for prive in ("CNI-SECRET-99", "333 00 00", "Rue privée"):
        assert prive not in page


def test_codes_falsifies_inconnus_ou_mal_formes_donnent_la_meme_reponse(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    anonyme = app.test_client()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    falsifie_controle = code[:-1] + next(c for c in alphabet if c != code[-1])
    falsifie_alea = next(c for c in alphabet if c != code[0]) + code[1:]
    with app.app_context():
        inconnu_bien_forme = V.nouveau_code()  # signature correcte mais jamais émis
    for faux in (falsifie_controle, falsifie_alea, inconnu_bien_forme, "ABC", "0" * 24, "x" * 300):
        r = anonyme.get(f"/verifier/{faux}")
        assert r.status_code == 404 and "Code inconnu ou invalide" in r.data.decode(), faux
        assert "Said Ali" not in r.data.decode()
    # une tentative de traversée de chemin n'atteint même pas la vérification
    assert anonyme.get("/verifier/../../etc/passwd").status_code == 404


def test_compteur_de_verifications(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    anonyme = app.test_client()
    anonyme.get(f"/verifier/{code}")
    anonyme.get(f"/verifier/{code}")
    with app.app_context():
        rec = DocumentEmis.query.one()
        assert rec.nb_verifications == 2 and rec.derniere_verification_at is not None


# ------------------------------------------------------------------ états
def test_document_non_a_jour_apres_correction_du_dossier(app, login):
    did = dossier_pret(login)
    dire = login("dir")
    imprimer(dire, did)
    code = dernier_code(app)
    anonyme = app.test_client()
    assert "Document authentique" in anonyme.get(f"/verifier/{code}").data.decode()

    dire.post(f"/dossiers/{did}/corriger/rcp", data={"valeur": "RTS-CORRIGE"})
    page = anonyme.get(f"/verifier/{code}").data.decode()
    assert "non à jour" in page
    assert "RTS-888" in page and "RTS-CORRIGE" not in page  # on montre ce qui a été imprimé, pas les nouvelles données

    # un nouvel exemplaire reflète l'état actuel et est valide
    imprimer(dire, did)
    assert "Document authentique" in anonyme.get(f"/verifier/{dernier_code(app)}").data.decode()


def test_revocation_reservee_avec_motif_et_effet_immediat(app, login):
    did = dossier_pret(login)
    pub, dire = login("pub"), login("dir")
    imprimer(pub, did)
    imprimer(pub, did)
    with app.app_context():
        premier, second = [r.id for r in DocumentEmis.query.order_by(DocumentEmis.id)]
        code1, code2 = [r.code for r in DocumentEmis.query.order_by(DocumentEmis.id)]

    assert pub.post(f"/emis/{premier}/revoquer", data={"motif": "x"}).status_code == 403   # pas le topo public
    assert login("dom").post(f"/emis/{premier}/revoquer", data={"motif": "x"}).status_code == 403
    dire.post(f"/emis/{premier}/revoquer", data={"motif": "  "})                            # motif obligatoire
    anonyme = app.test_client()
    assert "Document authentique" in anonyme.get(f"/verifier/{code1}").data.decode()

    dire.post(f"/emis/{premier}/revoquer", data={"motif": "Copie perdue"})
    assert "Document révoqué" in anonyme.get(f"/verifier/{code1}").data.decode()
    assert "Document authentique" in anonyme.get(f"/verifier/{code2}").data.decode()        # l'autre copie reste valide
    with app.app_context():
        assert AuditLog.query.filter_by(action="document_revoque").count() == 1


def test_alteration_de_la_base_detectee(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    code = dernier_code(app)
    with app.app_context():  # quelqu'un modifie directement la base : propriétaire et RTS changés
        rec = DocumentEmis.query.one()
        rec.snapshot = rec.snapshot.replace("Said Ali", "Pirate Imposteur").replace("RTS-888", "RTS-000")
        db.session.commit()
    page = app.test_client().get(f"/verifier/{code}").data.decode()
    assert "Enregistrement altéré" in page
    assert "Pirate" not in page and "Document authentique" not in page   # on n'affiche pas des données douteuses


def test_la_signature_depend_de_la_cle_secrete(app, login):
    did = dossier_pret(login)
    imprimer(login("pub"), did)
    with app.app_context():
        rec = DocumentEmis.query.one()
        signature = V.signer(rec)
        app.config["QR_SECRET"] = "une-autre-cle"
        assert V.signer(rec) != signature
        assert not V.code_valide(rec.code)  # un faux serveur sans la clé ne sait pas produire de codes acceptés


# ------------------------------------------------------------------ interface agents
def test_exemplaires_visibles_pour_agents_pas_pour_topo_prive(app, login):
    did = dossier_pret(login, "certifie")
    dire = login("dir")
    imprimer(dire, did, "certificat")
    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "Générés par le système" in page and "Révoquer" in page and "Valide" in page and "Documents (1)" in page
    # le topo public et le Domaine voient la liste mais ne peuvent pas révoquer
    for qui in ("pub", "dom"):
        vue = login(qui).get(f"/dossiers/{did}").data.decode()
        assert "Générés par le système" in vue and "Révoquer" not in vue, qui
    # le topo privé n'a ni la liste ni la possibilité d'imprimer
    priv = login("priv1")
    did2 = creer_dossier(priv)
    assert "Générés par le système" not in priv.get(f"/dossiers/{did2}").data.decode()


def test_page_de_saisie_du_code_accessible_sans_connexion(app):
    r = app.test_client().get("/verifier")
    assert r.status_code == 200 and "Scannez le QR code" in r.data.decode()
