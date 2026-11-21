from io import BytesIO

from pypdf import PdfReader

from app import constants as C
from app.extensions import db
from app.models import AuditLog, Dossier

from conftest import agir, creer_dossier


def texte(reponse):
    assert reponse.status_code == 200 and reponse.mimetype == "application/pdf"
    assert reponse.data.startswith(b"%PDF")
    return "\n".join(p.extract_text() for p in PdfReader(BytesIO(reponse.data)).pages)


def dossier_a_etape(app, login, jusqua):
    """Fait avancer un dossier public : 'rsp', 'rcp' ou 'certifie'. Retourne son id."""
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="RTC-777")
    if jusqua in ("rcp", "certifie"):
        agir(dom, did, "saisir_rcp", valeur="RTS-888")
    if jusqua == "certifie":
        agir(dire, did, "valider")
    return did


def test_plan_croquis_contient_rsp_rcp_et_emplacement_qr(app, login):
    did = dossier_a_etape(app, login, "rcp")
    reponse = login("pub").get(f"/dossiers/{did}/imprimer/plan_croquis")
    t = texte(reponse)
    assert len(PdfReader(BytesIO(reponse.data)).pages) == 1  # tout tient sur une feuille A3
    for attendu in ("PLAN CROQUIS", "RTC-777", "RTS-888", "SIGF-", "Said", "Moroni", "Scannez pour v", "copie n", "UNION DES COMORES"):
        assert attendu in t, attendu
    with app.app_context():
        assert AuditLog.query.filter_by(action="impression", nouveau="plan_croquis copie 1").count() == 1


def test_plan_croquis_interdit_avant_rsp_et_rcp(app, login):
    pub, dom = login("pub"), login("dom")
    did = creer_dossier(pub)
    assert pub.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 403
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="R1")
    assert dom.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 403  # RTS manquant
    agir(dom, did, "saisir_rcp", valeur="C1")
    assert dom.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 200


def test_documents_officiels_seulement_apres_validation(app, login):
    did = dossier_a_etape(app, login, "rcp")
    dire = login("dir")
    for t in ("certificat", "extrait", "attestation"):
        assert dire.get(f"/dossiers/{did}/imprimer/{t}").status_code == 403
    agir(dire, did, "valider")
    assert "CERTIFICAT DE PROPRI" in texte(dire.get(f"/dossiers/{did}/imprimer/certificat"))
    extrait = texte(dire.get(f"/dossiers/{did}/imprimer/extrait"))
    assert "Historique du traitement" in extrait and "RTC-777" in extrait
    for action in ("Dossier ouvert", "Plan croquis terminé", "Numéro RTC saisi", "Numéro RTS saisi", "Certificat validé"):
        assert action in extrait, action
    attestation = texte(dire.get(f"/dossiers/{did}/imprimer/attestation"))
    assert "ATTESTATION" in attestation and "Said Ali" in attestation


def test_droits_impression_par_role(app, login):
    did = dossier_a_etape(app, login, "certifie")
    # le topo public n'imprime que le plan croquis
    pub = login("pub")
    assert pub.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 200
    assert pub.get(f"/dossiers/{did}/imprimer/certificat").status_code == 403
    # le domaine imprime tout
    assert login("dom").get(f"/dossiers/{did}/imprimer/certificat").status_code == 200
    # type inconnu
    assert login("dir").get(f"/dossiers/{did}/imprimer/n_importe_quoi").status_code == 404
    # non connecté
    assert app.test_client().get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 302


def test_topo_prive_ne_peut_rien_imprimer(app, login):
    priv, pub, dom, dire = login("priv1"), login("pub"), login("dom"), login("dir")
    did = creer_dossier(priv)
    agir(priv, did, "terminer_plan")
    agir(priv, did, "envoyer_avis", commentaire="OK")
    agir(pub, did, "saisir_rsp", valeur="R1")
    agir(dom, did, "saisir_rcp", valeur="C1")
    agir(dire, did, "valider")
    for t in ("plan_croquis", "certificat", "extrait", "attestation"):
        assert priv.get(f"/dossiers/{did}/imprimer/{t}").status_code == 403
    # et un autre topo privé ne voit même pas le dossier
    assert login("priv2").get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 404


def test_anciens_dossiers_sans_documents_officiels(app, login):
    import io

    dom = login("dom")
    r = dom.post(
        "/dossiers/ancien/nouveau",
        data={"proprio_nom": "X", "ile_id": "1", "commune": "Y", "type_document": ["Titre foncier"],
              "fichiers": (io.BytesIO(b"%PDF-1.4 x"), "a.pdf")},
        content_type="multipart/form-data",
    )
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    assert dom.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 403


def test_texte_hostile_ou_non_latin_ne_casse_pas_le_pdf(app, login):
    did = dossier_a_etape(app, login, "certifie")
    with app.app_context():
        d = db.session.get(Dossier, did)
        d.proprietaire.nom = "<b>Hacker</b> & <i>محمد</i> </para>"
        d.proprietaire.adresse = "A" * 600
        db.session.commit()
    dire = login("dir")
    for t in ("plan_croquis", "certificat", "extrait", "attestation"):
        t_pdf = texte(dire.get(f"/dossiers/{did}/imprimer/{t}"))
        assert "<b>Hacker</b>" in t_pdf or t == "plan_croquis"  # le balisage est affiché tel quel, pas interprété


def test_boutons_d_impression_sur_la_fiche(app, login):
    did = dossier_a_etape(app, login, "certifie")
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    for t in ("plan_croquis", "certificat", "extrait", "attestation"):
        assert f"/dossiers/{did}/imprimer/{t}" in page
    page = login("pub").get(f"/dossiers/{did}").data.decode()
    assert f"/dossiers/{did}/imprimer/certificat" not in page
