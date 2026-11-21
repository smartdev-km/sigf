"""Le topographe privé peut joindre des documents à son avis."""
import io

from app import constants as C
from app.extensions import db
from app.models import AuditLog, Document, Dossier

from conftest import agir, creer_dossier

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def avis(client, did, texte="Limites vérifiées sur le terrain.", fichiers=()):
    data = {"commentaire": texte}
    if fichiers:
        data["pieces_avis"] = [(io.BytesIO(contenu), nom) for nom, contenu in fichiers]
    return client.post(f"/dossiers/{did}/action/envoyer_avis", data=data, content_type="multipart/form-data", follow_redirects=True)


def pret(login):
    priv = login("priv1")
    did = creer_dossier(priv)
    agir(priv, did, "terminer_plan")
    return priv, did


def statut(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).statut


def test_le_formulaire_de_l_avis_propose_les_documents(app, login):
    priv, did = pret(login)
    page = priv.get(f"/dossiers/{did}").data.decode()
    assert 'name="pieces_avis"' in page and "Documents joints à l'avis" in page and 'enctype="multipart/form-data"' in page


def test_avis_avec_documents_joints(app, login):
    priv, did = pret(login)
    page = avis(priv, did, fichiers=[("croquis.pdf", PDF), ("photo.png", PNG)]).data.decode()
    assert "2 documents joints à l" in page and statut(app, did) == C.SAISIE_RSP
    assert "croquis.pdf" in page and "photo.png" in page and "Documents joints" in page               # visibles par le topographe privé lui-même
    with app.app_context():
        docs = Document.query.filter_by(dossier_id=did).order_by(Document.id).all()
        assert [(d.type, d.nom_original, d.uploaded_by.username) for d in docs] == [("Avis", "croquis.pdf", "priv1"), ("Avis", "photo.png", "priv1")]
        assert AuditLog.query.filter_by(action="document_ajoute").count() == 2
        ids = [d.id for d in docs]
    assert priv.get(f"/documents/{ids[0]}").status_code == 200                                           # il peut rouvrir ses pièces
    assert login("dom").get(f"/documents/{ids[0]}").status_code == 200
    assert login("priv2").get(f"/documents/{ids[0]}").status_code == 404                                  # pas celles d'un autre


def test_avis_sans_document_comme_avant(app, login):
    priv, did = pret(login)
    page = avis(priv, did).data.decode()
    assert "Étape enregistrée." in page and "joint" not in page.split("Étape enregistrée.")[1].split("<")[0] and statut(app, did) == C.SAISIE_RSP
    with app.app_context():
        assert Document.query.count() == 0


def test_document_invalide_n_envoie_pas_l_avis(app, login):
    priv, did = pret(login)
    page = avis(priv, did, fichiers=[("faux.pdf", b"ceci n'est pas un pdf")]).data.decode()
    assert "PDF, JPG, PNG ou TIFF valide" in page and statut(app, did) == C.ATTENTE_AVIS
    page = avis(priv, did, fichiers=[("script.exe", PDF)]).data.decode()
    assert "format non accepté" in page and statut(app, did) == C.ATTENTE_AVIS
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert d.avis is None and Document.query.count() == 0                                             # rien n'est resté


def test_cinq_documents_au_maximum(app, login):
    priv, did = pret(login)
    page = avis(priv, did, fichiers=[(f"p{i}.pdf", PDF) for i in range(6)]).data.decode()
    assert "5 documents au maximum" in page and statut(app, did) == C.ATTENTE_AVIS
    assert "5 documents joints" in avis(priv, did, fichiers=[(f"p{i}.pdf", PDF) for i in range(5)]).data.decode()


def test_les_pieces_de_l_avis_n_ouvrent_pas_la_section_documents(app, login):
    priv, did = pret(login)
    avis(priv, did, fichiers=[("croquis.pdf", PDF)])
    pub, dom = login("pub"), login("dom")
    page = dom.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' not in page and "croquis.pdf" in page                                         # lien dans la carte « Avis »
    # le topographe privé n'a toujours pas accès aux autres documents ni à la section
    page = priv.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' not in page
    pub_doc = pub.post(f"/dossiers/{did}/documents", data={"fichiers": (io.BytesIO(PDF), "x.pdf"), "type_document": "Plan croquis", "libelle_document": ""}, content_type="multipart/form-data")
    with app.app_context():
        autres = [d.id for d in Document.query.filter(Document.dossier_id == did, Document.type != "Avis")]
    for i in autres:
        assert priv.get(f"/documents/{i}").status_code == 404


def test_un_avis_deja_envoye_ne_peut_plus_recevoir_de_document(app, login):
    priv, did = pret(login)
    avis(priv, did)
    page = avis(priv, did, fichiers=[("tard.pdf", PDF)]).data.decode()
    assert "Action non autorisée" in page
    with app.app_context():
        assert Document.query.count() == 0
