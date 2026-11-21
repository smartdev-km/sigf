import io
import os

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from app import constants as C
from app.extensions import db
from app.models import AuditLog, User

from conftest import MDP, connecter
from test_mutations import dossier_certifie
from test_verification import dernier_code, imprimer


# ------------------------------------------------------------------ images de test
def png(taille=(400, 200), fond=(255, 255, 255, 0), trait=(10, 30, 120, 255)):
    img = Image.new("RGBA", taille, fond)
    ImageDraw.Draw(img).line([(60, 140), (140, 40), (220, 150), (330, 60)], fill=trait, width=7)
    sortie = io.BytesIO()
    img.save(sortie, "PNG")
    return sortie.getvalue()


def jpeg_fond_blanc(taille=(800, 400)):
    img = Image.new("RGB", taille, "white")
    ImageDraw.Draw(img).line([(300, 250), (380, 120), (480, 260), (560, 140)], fill=(15, 25, 110), width=9)
    sortie = io.BytesIO()
    img.save(sortie, "JPEG", quality=90)
    return sortie.getvalue()


def envoyer(client, contenu=None, nom="signature.png", titre=""):
    data = {"titre": titre}
    if contenu is not None:
        data["signature"] = (io.BytesIO(contenu), nom)
    return client.post("/parametres/signature", data=data, content_type="multipart/form-data", follow_redirects=False)


def directeur(app):
    with app.app_context():
        return User.query.filter_by(username="dir").one()


def fichier_stocke(app, cle):
    return os.path.join(app.config["UPLOAD_DIR"], cle)


def nb_images(pdf):
    return sum(len(p.images) for p in PdfReader(io.BytesIO(pdf)).pages)


def texte(pdf):
    return " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def second_directeur(app, username="dir2", nom="Second Directeur"):
    with app.app_context():
        u = User(nom=nom, username=username, role=C.DIRECTEUR, doit_changer_mdp=False)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()


# ------------------------------------------------------------------ accès
def test_page_parametres_directeur_et_administrateur_seulement(app, login):
    assert login("dir").get("/parametres").status_code == 200
    assert login("admin").get("/parametres").status_code == 200                       # l'administrateur consulte
    for role in ("dom", "pub", "priv1"):
        assert login(role).get("/parametres").status_code == 403, role
    for role in ("admin", "dom", "pub", "priv1"):                                      # mais seul le Directeur dépose, remplace ou supprime
        c = login(role)
        assert envoyer(c, png()).status_code == 403
        assert c.post("/parametres/signature/supprimer").status_code == 403
    assert app.test_client().get("/parametres").status_code == 302


def test_menu_et_accueil_parametres_pour_le_directeur_et_l_administrateur(app, login):
    for role in ("dir", "admin"):
        assert 'href="/parametres"' in login(role).get("/dossiers").data.decode()
        assert 'href="/parametres"' in login(role).get("/bienvenue").data.decode()
    for role in ("dom", "pub", "priv1"):
        assert 'href="/parametres"' not in login(role).get("/dossiers").data.decode(), role


# ------------------------------------------------------------------ dépôt de la signature
def test_enregistrer_une_signature_png(app, login):
    dire = login("dir")
    r = envoyer(dire, png(), titre="  Le   Directeur général ")
    assert r.status_code == 302
    u = directeur(app)
    assert u.signature_cle and u.signature_cle.startswith(f"signatures/{u.id}/") and u.signature_titre == "Le Directeur général" and u.signature_at
    stocke = Image.open(fichier_stocke(app, u.signature_cle))
    assert stocke.format == "PNG" and stocke.mode == "RGBA" and stocke.width < 400 and stocke.height < 200          # recadrée sur le trait
    page = dire.get("/parametres").data.decode()
    assert "/parametres/signature?v=" in page and "Le Directeur général" in page and "Supprimer ma signature" in page
    with app.app_context():
        assert AuditLog.query.filter_by(action="signature_enregistree", entite_id=u.id).count() == 1


def test_jpeg_fond_blanc_devient_transparent_et_recadre(app, login):
    envoyer(login("dir"), jpeg_fond_blanc(), nom="scan.jpg")
    stocke = Image.open(fichier_stocke(app, directeur(app).signature_cle))
    assert stocke.format == "PNG" and stocke.width < 400 and stocke.height < 200
    alpha = stocke.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0 and alpha.getextrema()[1] == 255                 # coins transparents, trait opaque


def test_les_metadonnees_du_fichier_d_origine_ne_sont_pas_conservees(app, login):
    img = Image.open(io.BytesIO(png()))
    img.info["Comment"] = "SECRET-METADONNEE"
    sortie = io.BytesIO()
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    meta.add_text("Auteur", "SECRET-METADONNEE")
    img.save(sortie, "PNG", pnginfo=meta)
    envoyer(login("dir"), sortie.getvalue())
    octets = open(fichier_stocke(app, directeur(app).signature_cle), "rb").read()
    assert b"SECRET-METADONNEE" not in octets


@pytest.mark.parametrize("contenu, nom, attendu", [
    (b"", "vide.png", "Le fichier est vide"),
    (b"%PDF-1.4 pas une image", "faux.png", "pas une image valide"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "s.svg", "pas une image valide"),
    (b"\x89PNG\r\n\x1a\n" + b"x" * 50, "casse.png", "pas une image valide"),
])
def test_fichiers_refuses(app, login, contenu, nom, attendu):
    dire = login("dir")
    envoyer(dire, contenu, nom=nom)
    assert attendu in dire.get("/parametres").data.decode()
    assert directeur(app).signature_cle is None


def test_formats_et_tailles_refuses(app, login):
    dire = login("dir")
    gif = io.BytesIO()
    Image.new("RGB", (50, 50), "red").save(gif, "GIF")
    envoyer(dire, gif.getvalue(), nom="a.gif")
    assert "Format non accepté" in dire.get("/parametres").data.decode()
    envoyer(dire, png(fond=(255, 255, 255, 255), trait=(255, 255, 255, 255)))                  # entièrement blanc
    assert "aucune signature visible" in dire.get("/parametres").data.decode()
    envoyer(dire, b"\x89PNG" + os.urandom(2 * 1024 * 1024 + 10), nom="gros.png")
    assert "dépasse 2 Mo" in dire.get("/parametres").data.decode()
    enorme = io.BytesIO()
    Image.new("RGBA", (5000, 5000), (255, 255, 255, 0)).save(enorme, "PNG")
    envoyer(dire, enorme.getvalue(), nom="enorme.png")
    assert "trop grande" in dire.get("/parametres").data.decode()
    assert directeur(app).signature_cle is None


def test_remplacer_supprime_l_ancien_fichier(app, login):
    dire = login("dir")
    envoyer(dire, png())
    ancien = directeur(app).signature_cle
    assert os.path.exists(fichier_stocke(app, ancien))
    envoyer(dire, png(trait=(120, 10, 10, 255)))
    nouveau = directeur(app).signature_cle
    assert nouveau != ancien and os.path.exists(fichier_stocke(app, nouveau)) and not os.path.exists(fichier_stocke(app, ancien))


def test_supprimer_la_signature(app, login):
    dire = login("dir")
    assert "Aucune signature à supprimer" in dire.post("/parametres/signature/supprimer", follow_redirects=True).data.decode()
    envoyer(dire, png())
    cle = directeur(app).signature_cle
    dire.post("/parametres/signature/supprimer")
    u = directeur(app)
    assert u.signature_cle is None and u.signature_at is None and not os.path.exists(fichier_stocke(app, cle))
    assert dire.get("/parametres/signature").status_code == 404
    with app.app_context():
        assert AuditLog.query.filter_by(action="signature_supprimee").count() == 1


def test_changer_seulement_la_mention(app, login):
    dire = login("dir")
    envoyer(dire, None, titre="Le Directeur")                                                  # pas d'image encore
    assert "Dessinez ou importez d&#39;abord votre signature" in dire.get("/parametres").data.decode()
    envoyer(dire, png(), titre="")
    cle = directeur(app).signature_cle
    envoyer(dire, None, titre="Le Directeur du Domaine")
    u = directeur(app)
    assert u.signature_cle == cle and u.signature_titre == "Le Directeur du Domaine"           # l'image est conservée
    envoyer(dire, None, titre="x" * 61)
    assert "60 caractères" in dire.get("/parametres").data.decode() and directeur(app).signature_titre == "Le Directeur du Domaine"


def test_l_image_n_est_servie_qu_a_son_proprietaire(app, login):
    dire = login("dir")
    envoyer(dire, png())
    r = dire.get("/parametres/signature")
    assert r.status_code == 200 and r.mimetype == "image/png" and "no-store" in r.headers["Cache-Control"]
    for role in ("admin", "dom", "pub", "priv1"):
        assert login(role).get("/parametres/signature").status_code == 404, role
    second_directeur(app)
    assert connecter(app, "dir2").get("/parametres/signature").status_code == 404                # pas celle d'un autre Directeur
    assert app.test_client().get("/parametres/signature").status_code == 302


def test_echec_du_stockage_n_enregistre_rien(app, login, monkeypatch):
    from app import signature
    from app.storage import StorageError

    class Panne:
        def put(self, *a, **k):
            raise StorageError("Échec de l'envoi vers le stockage")

        def delete(self, *a, **k):
            pass

    monkeypatch.setattr(signature, "get_storage", lambda: Panne())
    dire = login("dir")
    envoyer(dire, png())
    assert directeur(app).signature_cle is None and "Échec de l" in dire.get("/parametres").data.decode()


# ------------------------------------------------------------------ impression
def test_certificat_et_attestation_portent_la_signature(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    assert nb_images(imprimer(dire, did, "certificat").data) == 0                              # avant : espace manuscrit vide
    envoyer(dire, png(), titre="Le Directeur général")
    certificat, attestation = imprimer(dire, did, "certificat").data, imprimer(dire, did, "attestation").data
    assert nb_images(certificat) == 1 and nb_images(attestation) == 1
    assert "Le Directeur général" in texte(certificat) and "dir" in texte(certificat)           # mention + nom sous la signature
    for autre in ("extrait", "plan_croquis"):
        assert nb_images(imprimer(dire, did, autre).data) == 0, autre                            # seuls certificat et attestation


def test_le_signataire_est_le_directeur_qui_a_valide(app, login):
    did = dossier_certifie(login)                                                              # validé par « dir »
    envoyer(login("dir"), png(), titre="Le Directeur")
    second_directeur(app)
    dir2 = connecter(app, "dir2")
    assert nb_images(imprimer(dir2, did, "certificat").data) == 1                              # imprimé par dir2 : signature de dir (validateur)


def test_a_defaut_le_directeur_qui_imprime(app, login):
    did = dossier_certifie(login)                                                              # dir n'a pas de signature
    second_directeur(app)
    dir2 = connecter(app, "dir2")
    assert nb_images(imprimer(dir2, did, "certificat").data) == 0
    envoyer(dir2, png(), titre="Directeur adjoint")
    pdf = imprimer(dir2, did, "attestation").data
    assert nb_images(pdf) == 1 and "Directeur adjoint" in texte(pdf) and "Second Directeur" in texte(pdf)
    # un autre rôle qui imprime n'ajoute jamais de signature
    assert nb_images(imprimer(login("dom"), did, "certificat").data) == 0


def test_les_documents_deja_imprimes_restent_valides_quand_la_signature_change(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    envoyer(dire, png())
    imprimer(dire, did, "certificat")
    code = dernier_code(app)
    public = app.test_client()
    assert "Document authentique" in public.get(f"/verifier/{code}").data.decode()
    envoyer(dire, png(trait=(120, 10, 10, 255)))
    dire.post("/parametres/signature/supprimer")
    assert "Document authentique" in public.get(f"/verifier/{code}").data.decode()              # la signature n'entre pas dans l'authenticité


def test_signature_introuvable_ou_corrompue_n_empeche_pas_l_impression(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    envoyer(dire, png())
    os.remove(fichier_stocke(app, directeur(app).signature_cle))                                # fichier perdu
    r = imprimer(dire, did, "certificat")
    assert r.data.startswith(b"%PDF") and nb_images(r.data) == 0
    envoyer(dire, png())
    with open(fichier_stocke(app, directeur(app).signature_cle), "wb") as f:
        f.write(b"pas une image")                                                               # fichier corrompu
    r = imprimer(dire, did, "attestation")
    assert r.data.startswith(b"%PDF") and nb_images(r.data) == 0


def test_mention_hostile_ne_casse_ni_la_page_ni_le_pdf(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    envoyer(dire, png(), titre='<b>Directeur</b> & « Ṣ » "x"')
    page = dire.get("/parametres").data.decode()
    assert "<b>Directeur</b>" not in page and "&lt;b&gt;Directeur&lt;/b&gt;" in page
    assert imprimer(dire, did, "certificat").data.startswith(b"%PDF")


# ------------------------------------------------------------------ stockage
def test_lecture_du_stockage_local_et_s3(tmp_path):
    from app.storage import LocalStorage, S3Storage, StorageError

    local = LocalStorage(str(tmp_path))
    local.put("a/b.png", b"octets", "image/png")
    assert local.lire("a/b.png") == b"octets"
    with pytest.raises(StorageError):
        local.lire("a/absent.png")
    with pytest.raises(StorageError):
        local.lire("../dehors")
    from moto import mock_aws

    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bucket-test-signature")
        s3 = S3Storage("bucket-test-signature", None, "k", "s", "us-east-1")
        s3.put("sig/x.png", b"data", "image/png")
        assert s3.lire("sig/x.png") == b"data"
        with pytest.raises(StorageError):
            s3.lire("sig/absent.png")


# ------------------------------------------------------------------ cadre de signature (dessin)
import base64  # noqa: E402


def dessin(contenu=None, prefixe="data:image/png;base64,"):
    return prefixe + base64.b64encode(png() if contenu is None else contenu).decode()


def envoyer_dessin(client, valeur, titre=""):
    return client.post("/parametres/signature", data={"dessin": valeur, "titre": titre}, follow_redirects=False)


def test_page_parametres_propose_le_cadre_de_signature(app, login):
    page = login("dir").get("/parametres").data.decode()
    for attendu in ('id="cadre-signature"', 'id="champ-dessin"', "Dessiner ma signature", "Importer une image", "Tout effacer", "Annuler le dernier trait", "Signez ici", "signature_pad.js"):
        assert attendu in page, attendu
    js = login("dir").get("/static/js/signature_pad.js").data.decode()
    assert "pointerdown" in js and "toDataURL('image/png')" in js and "innerHTML" not in js


def test_enregistrer_une_signature_dessinee(app, login):
    dire = login("dir")
    assert envoyer_dessin(dire, dessin(), titre="Le Directeur général").status_code == 302
    u = directeur(app)
    assert u.signature_cle and u.signature_titre == "Le Directeur général"
    stocke = Image.open(fichier_stocke(app, u.signature_cle))
    assert stocke.format == "PNG" and stocke.mode == "RGBA" and stocke.width < 400 and stocke.height < 200       # recadré sur le tracé
    assert stocke.getchannel("A").getpixel((0, 0)) == 0                                                           # fond transparent conservé
    assert "/parametres/signature?v=" in dire.get("/parametres").data.decode()


def test_le_dessin_est_prioritaire_sur_un_fichier_envoye_en_meme_temps(app, login):
    dire = login("dir")
    dire.post("/parametres/signature", data={"dessin": dessin(), "signature": (io.BytesIO(jpeg_fond_blanc()), "scan.jpg"), "titre": ""}, content_type="multipart/form-data")
    stocke = Image.open(fichier_stocke(app, directeur(app).signature_cle))
    assert stocke.width < 400                                                                                     # c'est le PNG du dessin, pas le JPEG de 800 px


@pytest.mark.parametrize("valeur, attendu", [
    ("data:image/jpeg;base64,AAAA", "n&#39;a pas pu être lu"),                                                    # seul le PNG du cadre est accepté
    ("n'importe quoi", "n&#39;a pas pu être lu"),
    ("data:image/png;base64,@@@pas-du-base64@@@", "n&#39;a pas pu être lu"),
    ("data:image/png;base64," + base64.b64encode(b"pas une image").decode(), "pas une image valide"),
    ("data:image/png;base64," + base64.b64encode(b"<svg><script>alert(1)</script></svg>").decode(), "pas une image valide"),
])
def test_dessins_refuses(app, login, valeur, attendu):
    dire = login("dir")
    envoyer_dessin(dire, valeur)
    assert attendu in dire.get("/parametres").data.decode()
    assert directeur(app).signature_cle is None


def test_dessin_vide_ou_trop_gros_refuse(app, login):
    dire = login("dir")
    vide = io.BytesIO()
    Image.new("RGBA", (800, 300), (0, 0, 0, 0)).save(vide, "PNG")                                                 # cadre sans aucun trait
    envoyer_dessin(dire, dessin(vide.getvalue()))
    assert "aucune signature visible" in dire.get("/parametres").data.decode()
    envoyer_dessin(dire, "data:image/png;base64," + "A" * (3 * 1024 * 1024))
    assert "trop volumineux" in dire.get("/parametres").data.decode()
    assert directeur(app).signature_cle is None


def test_dessin_reserve_au_directeur(app, login):
    for role in ("admin", "dom", "pub", "priv1"):
        assert envoyer_dessin(login(role), dessin()).status_code == 403, role
    assert directeur(app).signature_cle is None


def test_le_dessin_remplace_l_ancienne_signature_et_s_imprime(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    envoyer(dire, png())
    ancien = directeur(app).signature_cle
    envoyer_dessin(dire, dessin(png(trait=(120, 10, 10, 255))), titre="Le Directeur")
    nouveau = directeur(app).signature_cle
    assert nouveau != ancien and not os.path.exists(fichier_stocke(app, ancien)) and os.path.exists(fichier_stocke(app, nouveau))
    assert nb_images(imprimer(dire, did, "certificat").data) == 1


# ------------------------------------------------------------------ l'administrateur consulte les signatures des Directeurs
def compte_directeur(app, username, nom, ile_id=None):
    with app.app_context():
        u = User(nom=nom, username=username, role=C.DIRECTEUR, ile_id=ile_id, doit_changer_mdp=False)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
        return u.id


def test_l_administrateur_voit_les_signatures_de_chaque_directeur(app, login):
    compte_directeur(app, "dir.mwali", "Directeur Mwali", ile_id=3)
    envoyer(login("dir"), png(), titre="Le Directeur général")
    envoyer(connecter(app, "dir.mwali"), png(trait=(120, 10, 10, 255)), titre="Directeur de Mwali")
    page = login("admin").get("/parametres").data.decode()
    assert "Signatures des Directeurs" in page and "2 signatures enregistrées sur 2 Directeurs" in page
    assert "Le Directeur général" in page and "Directeur de Mwali" in page and "Mwali" in page
    assert page.count("/parametres/signature/") == 2                                                       # une image par signature
    # pas de formulaire de dépôt pour l'administrateur
    assert 'id="cadre-signature"' not in page and "Supprimer ma signature" not in page


def test_directeur_sans_signature_et_sans_ile_signales(app, login):
    compte_directeur(app, "dir.nouveau", "Nouveau Directeur", ile_id=2)
    page = login("admin").get("/parametres").data.decode()
    assert "0 signature enregistrée sur 2 Directeurs" in page and page.count("Aucune signature") == 2
    assert "Non attribuée" in page                                                                         # le Directeur historique n'a pas d'île
    assert "Ndzuwani" in page


def test_l_administrateur_recupere_l_image_de_n_importe_quel_directeur(app, login):
    envoyer(login("dir"), png())
    i = directeur(app).id
    r = login("admin").get(f"/parametres/signature/{i}")
    assert r.status_code == 200 and r.mimetype == "image/png" and "no-store" in r.headers["Cache-Control"]
    assert Image.open(io.BytesIO(r.data)).format == "PNG"
    # personne d'autre : ni les autres rôles, ni un autre Directeur
    second_directeur(app)
    for client in (login("dom"), login("pub"), login("priv1"), connecter(app, "dir2"), login("dir"), app.test_client()):
        assert client.get(f"/parametres/signature/{i}").status_code in (302, 403), client
    # compte inexistant, qui n'est pas un Directeur, ou sans signature
    admin = login("admin")
    assert admin.get("/parametres/signature/99999").status_code == 404
    assert admin.get(f"/parametres/signature/{User_id(app, 'dom')}").status_code == 404
    assert admin.get(f"/parametres/signature/{User_id(app, 'dir2')}").status_code == 404


def User_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def test_page_administrateur_sans_directeur_et_mention_echappee(app, login):
    envoyer(login("dir"), png(), titre='<img src=x onerror=alert(1)>')
    page = login("admin").get("/parametres").data.decode()
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    with app.app_context():
        db.session.delete(User.query.filter_by(username="dir").one())                                      # plus aucun Directeur
        db.session.commit()
    assert "Aucun compte Directeur" in login("admin").get("/parametres").data.decode()


# ------------------------------------------------------------------ signature et île
def test_chaque_certificat_porte_la_signature_du_directeur_de_son_ile(app, login):
    """La signature est liée au COMPTE du Directeur ; comme un Directeur limité à son île ne voit ni ne valide que les dossiers de son île,
    chaque certificat porte en pratique la signature du Directeur de son île."""
    from conftest import agir
    from test_partages import cree

    # deux dossiers prêts à être validés, un par île
    pub, dom = login("pub"), login("dom")
    ids = {}
    for ile, commune in (("1", "Moroni"), ("2", "Mutsamudu")):
        d = cree(login, nom=f"Prop{ile}", identite=f"CNI-{ile}", ile=ile, commune=commune, certifier=False)
        agir(pub, d, "terminer_plan")
        agir(pub, d, "saisir_rsp", valeur=f"R{d}")
        agir(dom, d, "saisir_rcp", valeur=f"C{d}")
        ids[ile] = d
    with app.app_context():
        User.query.filter_by(username="dir").one().ile_id = 1
        db.session.commit()
    compte_directeur(app, "dir.anjouan", "Directeur Anjouan", ile_id=2)
    dir1, dir2 = login("dir"), connecter(app, "dir.anjouan")
    envoyer(dir1, png(trait=(10, 30, 120, 255)), titre="Directeur de Ngazidja")
    envoyer(dir2, png(trait=(120, 10, 10, 255)), titre="Directeur d'Anjouan")
    # chaque Directeur ne peut valider que les dossiers de son île
    assert dir1.post(f"/dossiers/{ids['2']}/action/valider").status_code == 404
    assert dir2.post(f"/dossiers/{ids['1']}/action/valider").status_code == 404
    agir(dir1, ids["1"], "valider")
    agir(dir2, ids["2"], "valider")
    cert1 = imprimer(dir1, ids["1"], "certificat").data
    cert2 = imprimer(dir2, ids["2"], "certificat").data
    assert "Directeur de Ngazidja" in texte(cert1) and "Directeur d'Anjouan" not in texte(cert1)
    assert "Directeur d'Anjouan" in texte(cert2) and "Directeur de Ngazidja" not in texte(cert2)
    # et l'administrateur, qui voit toutes les îles, imprime avec la signature du Directeur qui a validé
    admin = login("admin")
    assert "Directeur de Ngazidja" in texte(imprimer(admin, ids["1"], "certificat").data)
    assert "Directeur d'Anjouan" in texte(imprimer(admin, ids["2"], "certificat").data)
