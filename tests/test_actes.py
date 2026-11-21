"""Acte généré par le système pour chaque mutation qui cède des parts ou grève la parcelle (vente, donation, héritage, expropriation,
hypothèque, mainlevée) : imprimable, avec QR code, archivé et vérifiable."""
import io
import os

import pytest
from pypdf import PdfReader

from app import constants as C
from app.extensions import db
from app.models import AuditLog, DocumentEmis, Dossier, Mutation

from conftest import connecter
from test_mutations import benef, derniere_mutation_id, dossier_certifie, proprio_id, saisir, vivant


@pytest.fixture()
def auto(app):
    app.config["DOCUMENTS_AUTOMATIQUES"] = True
    return app


def texte(pdf):
    brut = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    return " ".join(brut.split())                                                              # espaces, espaces insécables et retours à la ligne ramenés à un espace


def actes(app, did=None):
    with app.app_context():
        q = DocumentEmis.query.filter(DocumentEmis.mutation_id.isnot(None))
        if did is not None:
            q = q.filter(DocumentEmis.dossier_id == did)
        return [(r.type, r.copie_n, r.mutation_id, r.pdf_cle, r.emis_par.username, r.dossier_id) for r in q.order_by(DocumentEmis.id)]


def valider(login, app):
    mid = derniere_mutation_id(app)
    login("dir").post(f"/mutations/{mid}/valider")
    return mid


def vente(app, login, did, part="tout", prix="5000000", acheteurs=(("Moussa", "1"),)):
    saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": part}, **benef(*acheteurs), montant=prix)
    return valider(login, app)


def pdf_de(app, login, rec_id):
    return login("dom").get(f"/emis/{rec_id}/pdf").data


# ------------------------------------------------------------------ génération à la validation
@pytest.mark.parametrize("type_, champs, code", [
    ("vente", lambda s: {f"cede_{s}": "tout", **benef(("Acheteur", "1")), "montant": "5000000"}, "acte_vente"),
    ("donation", lambda s: {f"cede_{s}": "1/2", **benef(("Donataire", "1/2"))}, "acte_donation"),
    ("heritage", lambda s: {f"cede_{s}": "tout", **benef(("Heritier A", "1/2"), ("Heritier B", "1/2"))}, "acte_heritage"),
    ("expropriation", lambda s: {f"cede_{s}": "tout", **benef(("Etat", "1"))}, "acte_expropriation"),
    ("hypotheque", lambda s: {"creancier_nom": "Banque", "montant": "1000000"}, "acte_hypotheque"),
])
def test_chaque_type_de_mutation_a_son_acte(auto, login, type_, champs, code):
    did = dossier_certifie(login)
    saisir(login("dom"), did, type_, **champs(proprio_id(auto, did)))
    mid = valider(login, auto)
    [(t, copie, m, cle, par, dossier)] = actes(auto)
    assert (t, copie, m, par, dossier) == (code, 1, mid, "dir", did) and cle and os.path.exists(os.path.join(auto.config["UPLOAD_DIR"], cle))
    pdf = pdf_de(auto, login, _id(auto, mid))
    assert pdf.startswith(b"%PDF") and C.TYPES_ACTES[code].upper() in texte(pdf)


def _id(app, mutation_id):
    with app.app_context():
        return DocumentEmis.query.filter_by(mutation_id=mutation_id).one().id


def test_la_mainlevee_a_son_acte(auto, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque BIC", montant="10000000")
    hyp = valider(login, auto)
    saisir(login("dom"), did, "mainlevee", lie_a_id=str(hyp), reference_acte="Mainlevée n°7")
    ml = valider(login, auto)
    assert [(t, m) for t, _, m, *_ in actes(auto)] == [("acte_hypotheque", hyp), ("acte_mainlevee", ml)]
    pdf = pdf_de(auto, login, _id(auto, ml))
    assert "ACTE DE MAINLEVÉE" in texte(pdf) and "Banque BIC" in texte(pdf) and "10 000 000 KMF" in texte(pdf)


def test_pas_d_acte_pour_rectification_annulation_division_fusion(auto, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000")
    valider(login, auto)
    saisir(login("dom"), did, "rectification", notes="Orthographe du nom")
    valider(login, auto)
    saisir(login("dom"), did, "annulation", notes="Rectification abusive")
    valider(login, auto)
    assert [t for t, *_ in actes(auto)] == ["acte_hypotheque"]


def test_contenu_de_l_acte_de_vente_avec_les_m2(auto, login):
    did = dossier_certifie(login)                                                            # parcelle de 450,5 m²
    mid = vente(auto, login, did, part="1/2", acheteurs=(("Moussa", "1/2"),))
    pdf = pdf_de(auto, login, _id(auto, mid))
    t = texte(pdf)
    assert "ACTE DE VENTE" in t and "Said Ali" in t and "Moussa" in t and "5 000 000 KMF" in t
    assert "225,25 m²" in t and "1/2" in t and "450,5 m²" in t                                # la moitié de 450,5 m² = 225,25 m²
    assert "Nouveau dossier" in t and "Acte notarié n°12" in t and "Validé par" in t
    nouveau = vivant(auto, did)
    with auto.app_context():
        assert db.session.get(Dossier, nouveau).numero in t


# ------------------------------------------------------------------ où l'acte apparaît
def test_l_acte_apparait_dans_les_documents_et_l_historique(auto, login):
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    nouveau = vivant(auto, did)
    dire = login("dir")
    ancien = dire.get(f"/dossiers/{did}").data.decode()
    assert "Acte de vente" in ancien and f"/mutations/{mid}/acte" in ancien                    # bouton dans l'historique de l'ancien dossier
    assert 'id="documents"' in ancien and f"/emis/{_id(auto, mid)}/pdf" in ancien              # et dans ses documents générés
    suivant = dire.get(f"/dossiers/{nouveau}").data.decode().split('id="documents"')[1]
    assert "Acte de vente" in suivant and f"Mutation n° 1" in suivant                           # le nouveau dossier liste l'acte qui l'a créé
    assert "dossier SIGF-" in suivant                                                            # avec le dossier d'origine


def test_ouvrir_l_acte_rouvre_l_exemplaire_genere(auto, login):
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    r = login("dom").get(f"/mutations/{mid}/acte")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/emis/{_id(auto, mid)}/pdf")   # pas de copie supplémentaire
    assert len(actes(auto)) == 1
    assert login("dom").get(r.headers["Location"]).data.startswith(b"%PDF")


def test_sans_generation_automatique_l_acte_se_genere_au_premier_clic(app, login):
    assert app.config["DOCUMENTS_AUTOMATIQUES"] is False
    did = dossier_certifie(login)
    mid = vente(app, login, did)
    assert actes(app) == []
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert f"/mutations/{mid}/acte" in page and "Génère l&#39;acte" in page
    r = login("dom").get(f"/mutations/{mid}/acte")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and "ACTE DE VENTE" in texte(r.data)
    assert [(t, c) for t, c, *_ in actes(app)] == [("acte_vente", 1)]
    assert login("dom").get(f"/mutations/{mid}/acte").status_code == 302                      # deuxième clic : on rouvre
    assert len(actes(app)) == 1


def test_un_echec_de_generation_ne_bloque_pas_la_validation(auto, login, monkeypatch):
    from app import pdf as Pdf

    vrai = Pdf.generer

    def casse(*a, **k):
        raise RuntimeError("PDF impossible")

    monkeypatch.setattr(Pdf, "generer", casse)
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    with auto.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_VALIDEE and db.session.get(Dossier, did).statut == C.CLOTURE
        assert "ÉCHEC : Acte de vente" in AuditLog.query.filter_by(action="documents_generes", entite_id=did).order_by(AuditLog.id.desc()).first().nouveau
    assert actes(auto) == []                                                                      # pas d'exemplaire fantôme
    monkeypatch.setattr(Pdf, "generer", vrai)
    assert login("dom").get(f"/mutations/{mid}/acte").status_code == 200                         # reprise par le bouton


# ------------------------------------------------------------------ droits
def test_droits_sur_l_acte(auto, login):
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    for role in ("dom", "dir", "admin"):
        assert login(role).get(f"/mutations/{mid}/acte").status_code == 302, role
    assert login("priv1").get(f"/mutations/{mid}/acte").status_code == 404
    assert login("pub").get(f"/mutations/{mid}/acte").status_code == 403                        # le topographe public ne traite pas les mutations
    assert auto.test_client().get(f"/mutations/{mid}/acte").status_code == 302                    # connexion exigée
    assert login("dir").get("/mutations/99999/acte").status_code == 404


def test_acte_refuse_pour_une_mutation_en_attente_ou_sans_acte(auto, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000")
    en_attente = derniere_mutation_id(auto)
    assert login("dir").get(f"/mutations/{en_attente}/acte").status_code == 404
    valider(login, auto)
    saisir(login("dom"), did, "rectification", notes="Orthographe")
    rect = valider(login, auto)
    assert login("dir").get(f"/mutations/{rect}/acte").status_code == 404                        # pas d'acte pour une rectification


def test_acte_refuse_sur_un_dossier_bloque(auto, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000")
    mid = valider(login, auto)
    login("dom").post("/blocages/nouveau", data={"dossier": str(did), "type": "faux", "description": "Soupçon sérieux de faux sur l'acte."})
    assert login("dir").get(f"/mutations/{mid}/acte").status_code == 403
    assert f"/mutations/{mid}/acte" not in login("dir").get(f"/dossiers/{did}").data.decode()


def test_acte_limite_a_l_ile(auto, login):
    from app.models import User

    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    with auto.app_context():
        User.query.filter_by(username="dom").one().ile_id = 2                                        # le dossier est à l'île 1
        db.session.commit()
    assert login("dom").get(f"/mutations/{mid}/acte").status_code == 404
    assert login("dom").get(f"/emis/{_id(auto, mid)}/pdf").status_code == 404


# ------------------------------------------------------------------ signature du Directeur et vérification
def test_la_signature_du_directeur_est_sur_l_acte(auto, login):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (400, 160), (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(40, 120), (120, 30), (200, 130), (330, 40)], fill=(10, 30, 120, 255), width=7)
    sortie = io.BytesIO()
    img.save(sortie, "PNG")
    dire = login("dir")
    dire.post("/parametres/signature", data={"titre": "Le Directeur général", "signature": (io.BytesIO(sortie.getvalue()), "s.png")}, content_type="multipart/form-data")
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    pdf = pdf_de(auto, login, _id(auto, mid))
    assert sum(len(p.images) for p in PdfReader(io.BytesIO(pdf)).pages) == 1 and "Le Directeur général" in texte(pdf)


def test_verification_publique_de_l_acte(auto, login):
    did = dossier_certifie(login)
    mid = vente(auto, login, did, part="1/2", acheteurs=(("Moussa", "1/2"),))
    with auto.app_context():
        code = DocumentEmis.query.filter_by(mutation_id=mid).one().code
    public = auto.test_client()
    page = public.get(f"/verifier/{code}").data.decode()
    assert "Document authentique" in page and "Acte de vente" in page                           # reste authentique malgré la clôture de l'ancien dossier
    assert "Parcelle divisée ou fusionnée" not in page and "Propriété transférée" not in page
    for attendu in ("Vente", "Cédant", "Said Ali", "Bénéficiaire", "Moussa", "225,25 m²", "5 000 000 KMF", "Acte notarié n°12"):
        assert attendu in page, attendu
    assert "Propriétaire(s)" not in page                                                         # fiche propre aux actes


def test_acte_non_a_jour_apres_annulation_ou_mainlevee(auto, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000000")
    hyp = valider(login, auto)
    with auto.app_context():
        code = DocumentEmis.query.filter_by(mutation_id=hyp).one().code
    public = auto.test_client()
    assert "Document authentique" in public.get(f"/verifier/{code}").data.decode()
    saisir(login("dom"), did, "mainlevee", lie_a_id=str(hyp), reference_acte="Mainlevée n°7")
    valider(login, auto)
    assert "non à jour" in public.get(f"/verifier/{code}").data.decode().lower()                  # l'hypothèque est levée
    # une mutation annulée : son acte n'est plus à jour
    did2 = dossier_certifie(login)
    saisir(login("dom"), did2, "hypotheque", creancier_nom="Autre", montant="500")
    h2 = valider(login, auto)
    with auto.app_context():
        code2 = DocumentEmis.query.filter_by(mutation_id=h2).one().code
    saisir(login("dom"), did2, "annulation", notes="Saisie par erreur")
    valider(login, auto)
    page = public.get(f"/verifier/{code2}").data.decode()
    assert "non à jour" in page.lower()


def test_acte_revocable_et_signature_protegee(auto, login):
    did = dossier_certifie(login)
    mid = vente(auto, login, did)
    rid = _id(auto, mid)
    with auto.app_context():
        code = db.session.get(DocumentEmis, rid).code
    login("dir").post(f"/emis/{rid}/revoquer", data={"motif": "Acte contesté"})
    assert "Document révoqué" in auto.test_client().get(f"/verifier/{code}").data.decode()
    with auto.app_context():                                                                       # on ne peut pas rattacher un acte à une autre mutation
        rec = db.session.get(DocumentEmis, rid)
        rec.mutation_id = None
        db.session.commit()
    assert "Enregistrement altéré" in auto.test_client().get(f"/verifier/{code}").data.decode()


def test_actes_sur_un_ancien_dossier(auto, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "proprio_identite": "CNI-V", "ile_id": "1", "commune": "Domoni", "superficie": "800", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": [(io.BytesIO(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"), "titre.pdf")]})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="2000000")
    mid = valider(login, auto)
    assert [(t, d) for t, _, _, _, _, d in actes(auto)] == [("acte_hypotheque", did)]
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "Générés par le système" in page and "Acte d&#39;inscription d&#39;hypothèque" in page
    assert "800 m²" in texte(pdf_de(auto, login, _id(auto, mid)))


def test_les_chiffres_du_profil_comptent_les_actes(auto, login):
    did = dossier_certifie(login)
    vente(auto, login, did)
    assert ">1<" in login("dir").get("/profil").data.decode().replace(" ", "").replace("\n", "")
