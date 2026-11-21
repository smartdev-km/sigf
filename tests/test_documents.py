import io
import os

from markupsafe import escape

from app import constants as C
from app.extensions import db
from app.models import AuditLog, Document, Dossier

from conftest import agir, creer_dossier

PDF = b"%PDF-1.4\n%fake but valid header\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def fichier(contenu, nom="scan.pdf"):
    return (io.BytesIO(contenu), nom)


def televerser(client, did, contenu=PDF, nom="scan.pdf", type_doc="Plan croquis"):
    return client.post(
        f"/dossiers/{did}/documents",
        data={"type_document": type_doc, "fichiers": fichier(contenu, nom)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def ouvrir_documents(app, did):
    """Ouvre la section « Documents » d'un nouveau dossier : elle n'apparaît qu'après un premier document généré (plan croquis imprimé)."""
    from app import verification as V
    from app.models import User

    with app.app_context():
        V.emettre(db.session.get(Dossier, did), "plan_croquis", User.query.filter_by(username="dir").one())
        db.session.commit()


def docs(app, did):
    with app.app_context():
        return [(d.id, d.nom_original, d.mime, d.retire_at, d.sha256) for d in Document.query.filter_by(dossier_id=did)]


# ---------------- validation ----------------
def test_televersement_valide_et_stockage(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    televerser(pub, did, PDF, "plan.pdf")
    [(doc_id, nom, mime, _, sha)] = docs(app, did)
    assert (nom, mime) == ("plan.pdf", "application/pdf")
    assert len(sha) == 64
    r = pub.get(f"/documents/{doc_id}")
    assert r.status_code == 200 and r.data == PDF and r.mimetype == "application/pdf"
    with app.app_context():
        assert AuditLog.query.filter_by(action="document_ajoute").count() == 1
        assert AuditLog.query.filter_by(action="document_consulte").count() == 1


def test_refus_faux_pdf_mauvaise_extension_et_vide(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    # extension pdf mais contenu HTML (tentative d'injection)
    televerser(pub, did, b"<html><script>alert(1)</script></html>", "faux.pdf")
    # contenu valide mais extension interdite
    televerser(pub, did, PDF, "virus.exe")
    televerser(pub, did, PDF, "page.html")
    televerser(pub, did, b"", "vide.pdf")
    assert docs(app, did) == []


def test_depasse_taille_maximale(app, login):
    app.config["MAX_FICHIER_MO"] = 1
    pub = login("pub")
    did = creer_dossier(pub)
    televerser(pub, did, PDF + b"0" * (1024 * 1024 + 10), "gros.pdf")
    assert docs(app, did) == []


def test_nom_de_fichier_dangereux_est_neutralise(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    televerser(pub, did, PNG, "../../etc/passwd.png")
    [(_, nom, mime, _, _)] = docs(app, did)
    assert nom == "passwd.png" and mime == "image/png"
    with app.app_context():
        cle = Document.query.first().cle
        assert cle.startswith(f"dossiers/{did}/") and ".." not in cle and "passwd" not in cle


def test_aucun_fichier_ou_type_invalide(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    pub.post(f"/dossiers/{did}/documents", data={"type_document": "Plan croquis"}, content_type="multipart/form-data")
    televerser(pub, did, PDF, "a.pdf", type_doc="Type inventé")
    assert docs(app, did) == []


# ---------------- droits ----------------
def test_topo_prive_n_a_pas_acces_aux_documents(app, login):
    p1, p2, pub, dom = login("priv1"), login("priv2"), login("pub"), login("dom")
    did = creer_dossier(p1)
    ouvrir_documents(app, did)
    televerser(pub, did, PDF, "plan-public.pdf")   # le topo public (ou le Domaine) dépose ; le topo privé ne voit rien
    [(doc_id, *_)] = docs(app, did)
    assert pub.get(f"/documents/{doc_id}").status_code == 200
    for qui in (p1, p2):
        assert qui.get(f"/documents/{doc_id}").status_code == 404
        assert qui.get(f"/documents/{doc_id}?telecharger=1").status_code == 404
    page = p1.get(f"/dossiers/{did}").data.decode()
    assert "plan-public.pdf" not in page and "Documents (" not in page and "Enregistrer les documents" not in page
    assert "Documents (" in dom.get(f"/dossiers/{did}").data.decode()                 # les autres rôles la voient toujours


def test_topo_prive_ne_peut_pas_deposer_de_document(app, login):
    p1 = login("priv1")
    did = creer_dossier(p1)
    assert televerser(p1, did).status_code == 403
    assert televerser(login("priv2"), did).status_code == 404
    assert docs(app, did) == []


def test_documents_exigent_connexion(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    televerser(pub, did)
    [(doc_id, *_)] = docs(app, did)
    anonyme = app.test_client()
    assert anonyme.get(f"/documents/{doc_id}").status_code == 302


def test_retrait_reserve_admin_directeur_avec_motif(app, login):
    pub, dire = login("pub"), login("dir")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    televerser(pub, did)
    [(doc_id, *_)] = docs(app, did)

    assert pub.post(f"/documents/{doc_id}/retirer", data={"motif": "x"}).status_code == 403
    dire.post(f"/documents/{doc_id}/retirer", data={"motif": "  "})
    assert docs(app, did)[0][3] is None                                  # motif obligatoire
    dire.post(f"/documents/{doc_id}/retirer", data={"motif": "Mauvais scan"})
    assert docs(app, did)[0][3] is not None
    assert pub.get(f"/documents/{doc_id}").status_code == 404            # plus accessible
    with app.app_context():                                              # mais conservé
        cle = Document.query.first().cle
        assert os.path.exists(os.path.join(app.config["UPLOAD_DIR"], cle))


# ---------------- anciens dossiers ----------------
def donnees_ancien(**extra):
    base = {
        "proprio_nom": "Mohamed",
        "proprio_prenom": "Abdou",
        "ile_id": "2",
        "commune": "Mutsamudu",
        "reference_archive": "A-1987/45",
        "annee": "1987",
        "type_document": ["Titre foncier", "Pièce d'identité"],
        "fichiers": [fichier(PDF, "titre.pdf"), fichier(JPG, "cni.jpg")],
    }
    base.update(extra)
    return base


def test_import_ancien_dossier(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(), content_type="multipart/form-data")
    assert r.status_code == 302
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert (d.statut, d.origine, d.reference_archive, d.annee_archive) == (C.ARCHIVE, "ancien", "A-1987/45", 1987)
        # chaque fichier garde son propre type
        assert {x.nom_original: x.type for x in d.documents} == {"titre.pdf": "Titre foncier", "cni.jpg": "Pièce d'identité"}
        assert d.etapes[0].commentaire.startswith("Reprise de l'existant")
    page = dom.get(f"/dossiers/{did}").data.decode()
    assert "Dossier papier numérisé" in page and "titre.pdf" in page
    assert "Historique du circuit" in page and "À faire sur ce dossier" not in page


def test_import_ancien_refuse_selon_role(app, login):
    for qui in ("pub", "priv1"):
        r = login(qui).post("/dossiers/ancien/nouveau", data=donnees_ancien(), content_type="multipart/form-data")
        assert r.status_code == 403
    for qui in ("dom", "dir", "admin"):
        r = login(qui).get("/dossiers/ancien/nouveau")
        assert r.status_code == 200


def test_import_ancien_atomique_si_fichier_invalide(app, login):
    dom = login("dom")
    data = donnees_ancien(fichiers=[fichier(PDF, "ok.pdf"), fichier(b"pas un pdf", "mauvais.pdf")])
    r = dom.post("/dossiers/ancien/nouveau", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    with app.app_context():
        assert Dossier.query.count() == 0 and Document.query.count() == 0
    assert not os.path.exists(os.path.join(app.config["UPLOAD_DIR"], "dossiers"))


def test_import_ancien_exige_scan_et_annee_valide(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(fichiers=[], type_document=[]), content_type="multipart/form-data")
    assert r.status_code == 400
    r = dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(annee="12"), content_type="multipart/form-data")
    assert r.status_code == 400
    with app.app_context():
        assert Dossier.query.count() == 0


def test_anciens_et_nouveaux_dans_des_pages_separees(app, login):
    dom, priv, dire = login("dom"), login("priv1"), login("dir")
    dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(), content_type="multipart/form-data")
    creer_dossier(login("pub"))

    assert "Mohamed" not in priv.get("/dossiers").data.decode()                 # le topo privé ne voit jamais les anciens
    assert priv.get("/dossiers/anciens").status_code == 404
    nouveaux = dire.get("/dossiers").data.decode()
    assert "Said" in nouveaux and "Mohamed" not in nouveaux                      # « Dossiers » : nouveaux seulement
    anciens = dire.get("/dossiers/anciens").data.decode()
    assert "Mohamed" in anciens and "Said" not in anciens                        # « Anciens dossiers » : anciens seulement
    # recherche par n° de dossier papier, commune, propriétaire (dans la page des anciens)
    for q in ("A-1987", "Mutsamudu", "Mohamed"):
        assert "Mohamed" in dire.get(f"/dossiers/anciens?q={q}").data.decode(), q
    assert "Mohamed" not in dire.get("/dossiers?q=A-1987").data.decode()          # la liste des nouveaux ne les trouve pas

def test_import_ancien_types_incoherents_ou_invalides(app, login):
    dom = login("dom")
    # un seul type pour deux fichiers
    r = dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(type_document=["Titre foncier"]), content_type="multipart/form-data")
    assert r.status_code == 400
    # type inventé
    r = dom.post("/dossiers/ancien/nouveau", data=donnees_ancien(type_document=["Titre foncier", "Truc"]), content_type="multipart/form-data")
    assert r.status_code == 400
    with app.app_context():
        assert Dossier.query.count() == 0 and Document.query.count() == 0


def test_formulaire_ancien_propose_ajout_ligne_par_ligne(app, login):
    page = login("dom").get("/dossiers/ancien/nouveau").data.decode()
    assert 'id="ajouter-doc"' in page and 'id="modele-doc"' in page
    assert "Type des documents joints" not in page


def test_depot_ligne_par_ligne_sur_dossier_existant(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    r = pub.post(
        f"/dossiers/{did}/documents",
        data={"type_document": ["Plan croquis", "Avis"], "fichiers": [fichier(PDF, "plan.pdf"), fichier(PNG, "avis.png")]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    with app.app_context():
        types = {x.nom_original: x.type for x in Document.query.filter_by(dossier_id=did)}
    assert types == {"plan.pdf": "Plan croquis", "avis.png": "Avis"}
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert 'id="ajouter-doc"' in page and 'id="modele-doc"' in page


def test_depot_types_incoherents_rien_enregistre(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    pub.post(
        f"/dossiers/{did}/documents",
        data={"type_document": ["Plan croquis"], "fichiers": [fichier(PDF, "a.pdf"), fichier(PDF, "b.pdf")]},
        content_type="multipart/form-data",
    )
    assert docs(app, did) == []


# ---------------- liste complète + « Autre » nommé ----------------
def soumission_navigateur(remplis, autres=()):
    """Reproduit ce qu'envoie le formulaire : une ligne par type (vide si pas de fichier),
    puis les lignes « Autre » ajoutées avec leur nom."""
    types, libelles, fichiers = [], [], []
    for t in [x for x in C.TYPES_DOCUMENT if x != C.TYPE_AUTRE]:
        types.append(t)
        libelles.append("")
        fichiers.append(remplis[t] if t in remplis else (io.BytesIO(b""), ""))
    for nom, contenu, fichier_nom in autres:
        types.append(C.TYPE_AUTRE)
        libelles.append(nom)
        fichiers.append(fichier(contenu, fichier_nom))
    return {"type_document": types, "libelle_document": libelles, "fichiers": fichiers}


def test_formulaire_affiche_tous_les_types_et_garde_le_bouton(app, login):
    for url in ("/dossiers/ancien/nouveau",):
        page = login("dom").get(url).data.decode()
        for t in C.TYPES_DOCUMENT:
            if t != C.TYPE_AUTRE:
                assert f'value="{escape(t)}"' in page, t   # un emplacement par type
        assert 'id="ajouter-doc"' in page                  # bouton pour les documents « Autre »
        assert "Nom du document" in page
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert 'id="ajouter-doc"' in page and 'value="Titre foncier"' not in page                      # nouveau dossier : seulement « Ajouter un document »


def test_lignes_vides_ignorees_et_chaque_type_conserve(app, login):
    dom = login("dom")
    data = donnees_ancien()
    data.update(soumission_navigateur({"Titre foncier": fichier(PDF, "titre.pdf"), "Plan croquis": fichier(PNG, "plan.png")}))
    r = dom.post("/dossiers/ancien/nouveau", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.data.decode()
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert {x.nom_original: x.type for x in d.documents} == {"titre.pdf": "Titre foncier", "plan.png": "Plan croquis"}


def test_document_autre_doit_etre_nomme(app, login):
    dom = login("dom")
    # sans nom : refus, rien n'est créé
    data = donnees_ancien()
    data.update(soumission_navigateur({"Titre foncier": fichier(PDF, "t.pdf")}, autres=[("", PDF, "inconnu.pdf")]))
    assert dom.post("/dossiers/ancien/nouveau", data=data, content_type="multipart/form-data").status_code == 400
    with app.app_context():
        assert Dossier.query.count() == 0 and Document.query.count() == 0

    # avec nom : enregistré et affiché
    data = donnees_ancien()
    data.update(soumission_navigateur({"Titre foncier": fichier(PDF, "t.pdf")}, autres=[("Plan de bornage 1987", PDF, "scan001.pdf")]))
    r = dom.post("/dossiers/ancien/nouveau", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        autre = Document.query.filter_by(dossier_id=did, type=C.TYPE_AUTRE).one()
        assert autre.libelle == "Plan de bornage 1987" and autre.intitule == "Plan de bornage 1987"
    assert "Plan de bornage 1987" in dom.get(f"/dossiers/{did}").data.decode()


def test_nom_trop_long_et_libelle_ignore_hors_autre(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    ouvrir_documents(app, did)
    trop_long = "x" * 121
    pub.post(f"/dossiers/{did}/documents", content_type="multipart/form-data",
             data={"type_document": ["Autre"], "libelle_document": [trop_long], "fichiers": [fichier(PDF, "a.pdf")]})
    assert docs(app, did) == []
    # un nom fourni pour un type ordinaire est ignoré
    pub.post(f"/dossiers/{did}/documents", content_type="multipart/form-data",
             data={"type_document": ["Avis"], "libelle_document": ["intrus"], "fichiers": [fichier(PDF, "b.pdf")]})
    with app.app_context():
        doc = Document.query.filter_by(dossier_id=did).one()
        assert doc.type == "Avis" and doc.libelle is None


def test_depot_sans_aucun_fichier_refuse(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    pub.post(f"/dossiers/{did}/documents", content_type="multipart/form-data", data=soumission_navigateur({}))
    assert docs(app, did) == []


# ================= section « Documents » des nouveaux dossiers : générés par le système =================
from conftest import MDP, connecter  # noqa: E402


def dossier_complet(login):
    """Nouveau dossier complet (RTC et RTS saisis) : le plan croquis est imprimable mais pas encore imprimé."""
    pub, dom = login("pub"), login("dom")
    did = creer_dossier(pub)
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="RTC-7")
    agir(dom, did, "saisir_rcp", valeur="RTS-8")
    return did


def test_section_documents_cachee_tant_que_le_plan_croquis_n_est_pas_imprime(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    for qui in ("pub", "dom", "dir", "admin"):
        page = login(qui).get(f"/dossiers/{did}").data.decode()
        assert 'id="documents"' not in page and "Documents (" not in page and "Ajouter des documents" not in page, qui
    assert televerser(pub, did).status_code == 403 or docs(app, did) == []                 # le dépôt est aussi refusé côté serveur
    assert docs(app, did) == []
    # dossier complet (RTC + RTS) mais plan croquis non imprimé : toujours rien
    did2 = dossier_complet(login)
    assert 'id="documents"' not in login("dom").get(f"/dossiers/{did2}").data.decode()
    assert televerser(login("dom"), did2).status_code == 403 and docs(app, did2) == []


def test_la_section_apparait_apres_l_impression_du_plan_croquis(app, login):
    did = dossier_complet(login)
    pub = login("pub")
    r = pub.get(f"/dossiers/{did}/imprimer/plan_croquis")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' in page and "Documents (1)" in page and "Générés par le système" in page
    assert "Plan croquis" in page.split('id="documents"')[1] and "Ajouter un document" in page                      # l'option d'ajout est conservée
    assert televerser(pub, did, PDF, "manuel.pdf").status_code == 200
    assert [x[1] for x in docs(app, did)] == ["manuel.pdf"]
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "Documents (2)" in page and "Documents ajoutés" in page and "manuel.pdf" in page


def test_plus_de_bouton_generer_dans_la_section_documents(app, login):
    did = dossier_complet(login)
    pub = login("pub")
    pub.get(f"/dossiers/{did}/imprimer/plan_croquis")
    login("dir").post(f"/dossiers/{did}/action/valider")
    for qui in ("pub", "dom", "dir", "admin"):
        page = login(qui).get(f"/dossiers/{did}").data.decode().split('id="documents"')[1].split("Documents ajoutés")[0]
        assert "Générer : " not in page, qui                                           # les documents sont générés par le système, pas à la main


def test_documents_generes_archives_et_rouverts_a_l_identique(app, login):
    from app.models import DocumentEmis

    did = dossier_complet(login)
    pub = login("pub")
    pdf = pub.get(f"/dossiers/{did}/imprimer/plan_croquis").data
    with app.app_context():
        rec = DocumentEmis.query.filter_by(dossier_id=did).one()
        rid, cle, sha = rec.id, rec.pdf_cle, rec.pdf_sha256
    assert cle.startswith(f"documents-emis/{did}/") and cle.endswith(".pdf") and len(sha) == 64
    assert os.path.exists(os.path.join(app.config["UPLOAD_DIR"], cle))
    r = pub.get(f"/emis/{rid}/pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data == pdf                              # exactement le PDF imprimé
    assert "no-store" in r.headers["Cache-Control"]
    assert f"/emis/{rid}/pdf" in pub.get(f"/dossiers/{did}").data.decode()                                         # bouton « Ouvrir »
    with app.app_context():
        assert AuditLog.query.filter_by(action="document_consulte", entite="document_emis", entite_id=rid).count() == 1


def test_acces_a_un_exemplaire_archive(app, login):
    did = dossier_complet(login)
    login("pub").get(f"/dossiers/{did}/imprimer/plan_croquis")
    from app.models import DocumentEmis

    with app.app_context():
        rid = DocumentEmis.query.one().id
    for qui in ("pub", "dom", "dir", "admin"):
        assert login(qui).get(f"/emis/{rid}/pdf").status_code == 200, qui
    assert login("priv1").get(f"/emis/{rid}/pdf").status_code == 404                  # un topographe privé n'a pas ces documents
    assert app.test_client().get(f"/emis/{rid}/pdf").status_code == 302
    assert login("dir").get("/emis/99999/pdf").status_code == 404
    # un compte limité à une autre île ne le voit pas
    with app.app_context():
        from app.models import User

        User.query.filter_by(username="dom").one().ile_id = 2                           # le dossier est à l'île 1
        db.session.commit()
    assert login("dom").get(f"/emis/{rid}/pdf").status_code == 404


def test_exemplaire_sans_archive_ou_perdu(app, login):
    from app.models import DocumentEmis

    did = dossier_complet(login)
    pub = login("pub")
    pub.get(f"/dossiers/{did}/imprimer/plan_croquis")
    with app.app_context():
        rec = DocumentEmis.query.one()
        rid, cle = rec.id, rec.pdf_cle
    os.remove(os.path.join(app.config["UPLOAD_DIR"], cle))                              # fichier perdu
    assert pub.get(f"/emis/{rid}/pdf").status_code == 404
    with app.app_context():                                                              # exemplaire imprimé avant l'archivage
        DocumentEmis.query.one().pdf_cle = None
        db.session.commit()
    assert pub.get(f"/emis/{rid}/pdf").status_code == 404
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "non archivé" in page and f"/emis/{rid}/pdf" not in page


def test_un_echec_d_archivage_n_empeche_pas_l_impression(app, login, monkeypatch):
    from app import generation
    from app.models import DocumentEmis
    from app.storage import StorageError

    class Panne:
        def put(self, *a, **k):
            raise StorageError("disque plein")

    did = dossier_complet(login)
    monkeypatch.setattr(generation, "get_storage", lambda: Panne())
    r = login("pub").get(f"/dossiers/{did}/imprimer/plan_croquis")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    with app.app_context():
        rec = DocumentEmis.query.one()
        assert rec.pdf_cle is None and rec.pdf_sha256 is None                            # imprimé, non archivé : la section s'ouvre quand même
    assert "Générés par le système" in login("pub").get(f"/dossiers/{did}").data.decode()


def test_les_anciens_dossiers_gardent_toujours_leur_section(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "proprio_identite": "CNI-V", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": [(io.BytesIO(PDF), "titre.pdf")]})
    assert r.status_code == 302
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = dom.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' in page and "Ajouter des documents" in page and "Générés par le système" not in page     # pas de documents du système pour un ancien dossier
    assert televerser(dom, did, PDF, "autre.pdf").status_code == 200 and len(docs(app, did)) == 2


def test_un_dossier_qui_contient_deja_des_documents_garde_sa_section(app, login):
    pub = login("pub")
    did = creer_dossier(pub)
    with app.app_context():                                                              # justificatif déjà présent (dépôt d'avant la règle)
        from app.models import User

        db.session.add(Document(dossier_id=did, type="Autre", libelle="Ancien justificatif", nom_original="j.pdf", cle="x/j.pdf", mime="application/pdf", taille=10, sha256="0" * 64, uploaded_by_id=User.query.filter_by(username="pub").one().id))
        db.session.commit()
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' in page and "j.pdf" in page


def test_topographe_prive_ne_voit_pas_la_section_meme_apres_impression(app, login):
    priv = login("priv1")
    did = creer_dossier(priv)
    ouvrir_documents(app, did)
    page = priv.get(f"/dossiers/{did}").data.decode()
    assert 'id="documents"' not in page and "Générés par le système" not in page


def test_nouveau_dossier_sans_emplacements_par_type(app, login):
    """Les documents d'un nouveau dossier sont générés par le système : plus de liste « Plan croquis / Avis / Titre foncier… »."""
    did = dossier_complet(login)
    login("pub").get(f"/dossiers/{did}/imprimer/plan_croquis")
    for qui in ("pub", "dom", "dir", "admin"):
        zone = login(qui).get(f"/dossiers/{did}").data.decode().split('id="documents"')[1].split("Localisation et tracé")[0]
        for t in [x for x in C.TYPES_DOCUMENT if x != C.TYPE_AUTRE]:
            assert f'value="{escape(t)}"' not in zone, (qui, t)
            assert f'for="doc-' not in zone
        assert zone.count('name="fichiers"') == 1                                                              # seulement le modèle caché de « Ajouter un document » : aucune ligne de fichier par défaut
        assert 'id="ajouter-doc"' in zone and "Nom du document" in zone                                        # reste : un document « Autre », avec son nom
        assert "générés automatiquement par le système" in zone and "déposez chaque document sur sa ligne" not in zone
    # le dépôt d'un document nommé fonctionne toujours
    dom = login("dom")
    r = dom.post(f"/dossiers/{did}/documents", data={"type_document": [C.TYPE_AUTRE], "libelle_document": ["Courrier du notaire"], "fichiers": [fichier(PDF, "courrier.pdf")]}, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200 and "Courrier du notaire" in r.data.decode()
    assert [x[1] for x in docs(app, did)] == ["courrier.pdf"]


def test_anciens_dossiers_gardent_tous_leurs_emplacements(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "proprio_identite": "CNI-V", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": [fichier(PDF, "titre.pdf")]})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = dom.get(f"/dossiers/{did}").data.decode()
    for t in C.TYPES_DOCUMENT:
        if t != C.TYPE_AUTRE:
            assert f'value="{escape(t)}"' in page, t
    assert "déposez chaque document sur sa ligne" in page
    assert 'value="Titre foncier"' in dom.get("/dossiers/ancien/nouveau").data.decode()                       # formulaire de création d'un ancien dossier


def test_commande_verifier_stockage(app):
    runner = app.test_cli_runner()
    r = runner.invoke(args=["verifier-stockage"])
    assert r.exit_code == 0 and "Stockage OK" in r.output and "disque local" in r.output
