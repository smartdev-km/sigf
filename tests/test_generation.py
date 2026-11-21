"""Génération automatique des documents du système (plan croquis, certificat, extrait, attestation) à la certification d'un dossier."""
import io
import os

import pytest
from pypdf import PdfReader

from app import constants as C
from app import generation
from app.extensions import db
from app.models import AuditLog, DocumentEmis, Dossier

from conftest import agir, creer_dossier
from test_carte import LAT, LON, kml, points, televerser
from test_mutations import benef, derniere_mutation_id, proprio_id, saisir, vivant
from test_verification import dossier_pret

TYPES = ["plan_croquis", "certificat", "extrait", "attestation"]


@pytest.fixture()
def auto(app):
    app.config["DOCUMENTS_AUTOMATIQUES"] = True
    return app


def emis(app, did):
    with app.app_context():
        return [(r.type, r.copie_n, r.pdf_cle, r.emis_par.username) for r in DocumentEmis.query.filter_by(dossier_id=did).order_by(DocumentEmis.id)]


def texte(pdf):
    return " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


# ------------------------------------------------------------------ à la validation du Directeur
def test_tous_les_documents_sont_generes_quand_le_dossier_est_certifie(auto, login):
    did = dossier_pret(login)                                                           # RTC + RTS saisis : pas encore certifié
    assert emis(auto, did) == []
    login("dir").post(f"/dossiers/{did}/action/valider")
    generes = emis(auto, did)
    assert sorted(t for t, *_ in generes) == sorted(TYPES)
    assert all(copie == 1 and cle and cle.startswith(f"documents-emis/{did}/") and par == "dir" for _, copie, cle, par in generes)
    for _, _, cle, _ in generes:
        assert os.path.exists(os.path.join(auto.config["UPLOAD_DIR"], cle))              # le PDF est archivé
    with auto.app_context():
        assert AuditLog.query.filter_by(action="documents_generes", entite_id=did).count() == 1


def test_les_documents_apparaissent_dans_la_section_sans_bouton_generer(auto, login):
    did = dossier_pret(login, "certifie")
    for qui in ("pub", "dom", "dir", "admin"):
        page = login(qui).get(f"/dossiers/{did}").data.decode()
        zone = page.split('id="documents"')[1].split("Documents ajoutés")[0]
        assert "Documents (4)" in page and "Générés par le système" in zone, qui
        for titre in ("Plan croquis", "Certificat de propriété", "Extrait du dossier foncier", "Attestation"):
            assert titre in zone, (qui, titre)
        assert "Générer : " not in zone and "Générer les documents manquants" not in zone, qui        # rien à générer : tout est là
        assert zone.count("Ouvrir") == 4
    assert "Ajouter un document" in login("dom").get(f"/dossiers/{did}").data.decode()                # l'ajout manuel reste possible


def test_les_exemplaires_generes_s_ouvrent_et_sont_verifiables(auto, login):
    did = dossier_pret(login, "certifie")
    with auto.app_context():
        recs = {r.type: (r.id, r.code) for r in DocumentEmis.query.filter_by(dossier_id=did)}
    pdf = login("dom").get(f"/emis/{recs['certificat'][0]}/pdf").data
    assert pdf.startswith(b"%PDF") and "CERTIFICAT DE PROPRIÉTÉ" in texte(pdf)
    public = auto.test_client()
    for t, (_, code) in recs.items():
        assert "Document authentique" in public.get(f"/verifier/{code}").data.decode(), t           # chaque document a son QR valide


def test_dossier_en_cours_sans_documents_et_section_cachee(auto, login):
    did = dossier_pret(login)
    page = login("dom").get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' not in page
    agir(login("dir"), did, "rejeter", commentaire="Pièce manquante")                           # un rejet ne génère rien
    assert emis(auto, did) == []


def test_les_exemplaires_manuels_suivent_les_exemplaires_automatiques(auto, login):
    did = dossier_pret(login, "certifie")
    login("dir").get(f"/dossiers/{did}/imprimer/certificat")                                    # réimpression : copie n°2, QR distinct
    certificats = [(c, cle) for t, c, cle, _ in emis(auto, did) if t == "certificat"]
    assert [c for c, _ in certificats] == [1, 2] and all(cle for _, cle in certificats)


def test_la_signature_du_directeur_est_sur_les_documents_automatiques(auto, login):
    from PIL import Image, ImageDraw
    from conftest import connecter

    img = Image.new("RGBA", (400, 160), (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(40, 120), (120, 30), (200, 130), (330, 40)], fill=(10, 30, 120, 255), width=7)
    sortie = io.BytesIO()
    img.save(sortie, "PNG")
    dire = login("dir")
    dire.post("/parametres/signature", data={"titre": "Le Directeur général", "signature": (io.BytesIO(sortie.getvalue()), "s.png")}, content_type="multipart/form-data")
    did = dossier_pret(login, "certifie")
    with auto.app_context():
        recs = {r.type: r.id for r in DocumentEmis.query.filter_by(dossier_id=did)}
    nb = lambda i: sum(len(p.images) for p in PdfReader(io.BytesIO(dire.get(f"/emis/{i}/pdf").data)).pages)  # noqa: E731
    assert nb(recs["certificat"]) == 1 and nb(recs["attestation"]) == 1 and nb(recs["extrait"]) == 0 and nb(recs["plan_croquis"]) == 0


# ------------------------------------------------------------------ dossier issu d'un transfert de propriété
def test_le_dossier_issu_d_une_vente_a_ses_documents(auto, login):
    did = dossier_pret(login, "certifie")
    saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(auto, did)}": "tout"}, **benef(("Moussa", "1")), montant="100")
    login("dir").post(f"/mutations/{derniere_mutation_id(auto)}/valider")
    nouveau = vivant(auto, did)
    assert nouveau != did
    assert sorted(t for t, *_ in emis(auto, nouveau)) == sorted(TYPES)
    with auto.app_context():
        recs = {r.type: r.id for r in DocumentEmis.query.filter_by(dossier_id=nouveau)}
    assert "Moussa" in texte(login("dir").get(f"/emis/{recs['certificat']}/pdf").data)              # le certificat est au nom du nouveau propriétaire
    assert len(emis(auto, did)) == 5                                                                  # l'ancien dossier clôturé : ses 4 documents + l'acte de la vente


# ------------------------------------------------------------------ robustesse
def test_un_echec_de_generation_ne_bloque_pas_la_validation(auto, login, monkeypatch):
    from app import pdf as Pdf

    vrai = Pdf.generer

    def casse(type_doc, d, user, qr=None, mutation=None):
        if type_doc == "extrait":
            raise RuntimeError("PDF impossible")
        return vrai(type_doc, d, user, qr=qr, mutation=mutation)

    monkeypatch.setattr(Pdf, "generer", casse)
    did = dossier_pret(login, "certifie")
    with auto.app_context():
        assert db.session.get(Dossier, did).statut == C.CERTIFIE                                    # validé malgré l'incident
    assert sorted(t for t, *_ in emis(auto, did)) == sorted(["plan_croquis", "certificat", "attestation"])   # pas d'exemplaire fantôme
    with auto.app_context():
        assert "ÉCHEC : Extrait du dossier foncier" in AuditLog.query.filter_by(action="documents_generes", entite_id=did).one().nouveau
    page = login("dom").get(f"/dossiers/{did}").data.decode()
    assert "Document manquant : Extrait du dossier foncier" in page and "Générer les documents manquants" in page
    # reprise une fois le problème réglé
    monkeypatch.setattr(Pdf, "generer", vrai)
    r = login("dom").post(f"/dossiers/{did}/documents-systeme", follow_redirects=True)
    assert "1 document généré" in r.data.decode() and sorted(t for t, *_ in emis(auto, did)) == sorted(TYPES)
    assert "Générer les documents manquants" not in login("dom").get(f"/dossiers/{did}").data.decode()
    assert "déjà générés" in login("dom").post(f"/dossiers/{did}/documents-systeme", follow_redirects=True).data.decode()


def test_un_echec_d_archivage_laisse_les_documents_non_archives(auto, login, monkeypatch):
    from app.storage import StorageError

    class Panne:
        def put(self, *a, **k):
            raise StorageError("disque plein")

    monkeypatch.setattr(generation, "get_storage", lambda: Panne())
    did = dossier_pret(login, "certifie")
    generes = emis(auto, did)
    assert len(generes) == 4 and all(cle is None for _, _, cle, _ in generes)
    assert "non archivé" in login("dom").get(f"/dossiers/{did}").data.decode()


def test_option_desactivee_pas_de_generation(app, login):
    assert app.config["DOCUMENTS_AUTOMATIQUES"] is False
    did = dossier_pret(login, "certifie")
    assert emis(app, did) == []
    page = login("dom").get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' in page and "Documents manquants : " in page and "Générer les documents manquants" in page   # section ouverte + reprise possible


def test_reprise_reservee_aux_roles_autorises(auto, login):
    app = auto
    app.config["DOCUMENTS_AUTOMATIQUES"] = False
    did = dossier_pret(login, "certifie")
    app.config["DOCUMENTS_AUTOMATIQUES"] = True
    assert "Générer les documents manquants" not in login("pub").get(f"/dossiers/{did}").data.decode()
    assert login("pub").post(f"/dossiers/{did}/documents-systeme").status_code == 403
    assert login("priv1").post(f"/dossiers/{did}/documents-systeme").status_code == 404
    assert app.test_client().post(f"/dossiers/{did}/documents-systeme").status_code == 302
    assert emis(app, did) == []
    for role in ("dom", "dir", "admin"):
        assert login(role).post(f"/dossiers/{did}/documents-systeme").status_code == 302
    assert len(emis(app, did)) == 4                                                                    # une seule génération : pas de doublon


def test_reprise_refusee_sur_un_dossier_bloque_ou_non_certifie(auto, login):
    did = dossier_pret(login)                                                                          # pas certifié
    assert "déjà générés" in login("dom").post(f"/dossiers/{did}/documents-systeme", follow_redirects=True).data.decode()
    assert emis(auto, did) == []
    did2 = dossier_pret(login, "certifie")
    login("dom").post("/blocages/nouveau", data={"dossier": str(did2), "type": "faux", "description": "Soupçon sérieux de faux sur l'acte."})
    auto.config["DOCUMENTS_AUTOMATIQUES"] = False
    assert login("dom").post(f"/dossiers/{did2}/documents-systeme").status_code == 403


def test_les_anciens_dossiers_n_ont_pas_de_documents_automatiques(auto, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "proprio_identite": "CNI-V", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": [(io.BytesIO(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"), "titre.pdf")]})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with auto.app_context():
        assert emis(auto, did) == [] and generation.manquants(db.session.get(Dossier, did)) == []


def test_les_chiffres_du_profil_comptent_les_documents_generes(auto, login):
    dossier_pret(login, "certifie")
    assert ">4<" in login("dir").get("/profil").data.decode().replace(" ", "").replace("\n", "")


# ------------------------------------------------------------------ rattrapage des dossiers déjà certifiés
def test_commande_generer_documents(app, login):
    from app.models import User

    app.config["DOCUMENTS_AUTOMATIQUES"] = False
    d1, d2 = dossier_pret(login, "certifie"), dossier_pret(login, "certifie")
    en_cours = dossier_pret(login)
    assert emis(app, d1) == [] and emis(app, d2) == []
    runner = app.test_cli_runner()
    r = runner.invoke(args=["generer-documents", "--utilisateur", "dir"])
    assert r.exit_code == 0 and "2 dossier(s) traité(s)" in r.output and "8 document(s) généré(s)" in r.output
    assert len(emis(app, d1)) == 4 and len(emis(app, d2)) == 4 and emis(app, en_cours) == []
    assert all(par == "dir" for *_, par in emis(app, d1))
    r = runner.invoke(args=["generer-documents", "--utilisateur", "dir"])                          # idempotent
    assert "0 dossier(s) traité(s)" in r.output and len(emis(app, d1)) == 4
    assert runner.invoke(args=["generer-documents", "--utilisateur", "inconnu"]).exit_code != 0       # compte introuvable
    with app.app_context():
        assert AuditLog.query.filter_by(action="documents_generes").count() == 2


def test_aucun_exemplaire_fantome_si_le_qr_ne_peut_pas_etre_construit(auto, login, monkeypatch):
    from app import verification as V

    def sans_adresse(code):
        raise RuntimeError("Working outside of request context.")

    monkeypatch.setattr(V, "url_verification", sans_adresse)
    did = dossier_pret(login, "certifie")
    with auto.app_context():
        assert db.session.get(Dossier, did).statut == C.CERTIFIE
    assert emis(auto, did) == []                                                                       # rien n'a été créé à moitié
    assert "ÉCHEC" in _dernier_audit(auto, did)


def _dernier_audit(app, did):
    with app.app_context():
        return AuditLog.query.filter_by(action="documents_generes", entite_id=did).one().nouveau


def test_seed_demo_genere_et_archive_les_documents(auto, tmp_path):
    from app.models import User

    with auto.app_context():
        for n, role in [("dd", C.DIRECTEUR), ("dm", C.DOMAINE), ("tp", C.TOPO_PUBLIC), ("v1x", C.TOPO_PRIVE), ("v2x", C.TOPO_PRIVE)]:
            u = User(nom=n, username=n, role=role, doit_changer_mdp=False)
            u.set_password("motdepasse-solide-1")
            db.session.add(u)
        db.session.commit()
    res = auto.test_cli_runner().invoke(args=["seed-demo"])
    assert res.exit_code == 0, res.output
    with auto.app_context():
        certifies = Dossier.query.filter_by(statut=C.CERTIFIE, origine=C.ORIGINE_NOUVEAU).all()
        assert certifies
        for d in certifies:
            types = {r.type for r in DocumentEmis.query.filter_by(dossier_id=d.id)}
            assert set(TYPES) <= types, d.numero                                                       # tous les documents, même en ligne de commande
        assert DocumentEmis.query.filter(DocumentEmis.pdf_cle.isnot(None)).count() >= len(certifies) * 4
