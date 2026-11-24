"""Titrage : Journal officiel (Fonction publique) → Plan de bornage (Domaine) → Journal de clôture (Fonction publique) → Titré (Domaine)."""
import io
from datetime import date, timedelta

from app import constants as C
from app.extensions import db
from app.models import Dossier, DocumentEmis, EtapeTitrage, User

from conftest import agir, creer_dossier
from test_blocages import blocage_id, lever, signaler
from test_iles import attribuer, compte
from test_partages import cree, numero

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def jour(delta=0):
    """Date ISO : aujourd'hui + delta jours (négatif : passé)."""
    return (date.today() + timedelta(days=delta)).isoformat()


def etape(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).etape_titre


def avancer(client, did, reference="REF-1", date_etape=None, fichiers=None, **extra):
    data = {"reference": reference, "date_etape": date_etape or jour(-5), **extra}
    for nom, contenu in (fichiers or {}).items():
        data[nom] = (io.BytesIO(contenu), "scan.pdf")
    return client.post(f"/dossiers/{did}/titrage/avancer", data=data, content_type="multipart/form-data", follow_redirects=True)


def retour(client, did, motif="Pièce illisible"):
    return client.post(f"/dossiers/{did}/titrage/retour", data={"motif": motif}, follow_redirects=True)


# ------------------------------------------------------------------ entrée et déroulement
def test_la_certification_ouvre_le_titrage(app, login):
    did = cree(login)
    assert etape(app, did) == C.TIT_JO
    en_cours = creer_dossier(login("pub"))
    assert etape(app, en_cours) is None                                   # pas encore certifié
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert 'id="titrage"' in page and "À faire : Journal officiel" in page
    assert 'id="titrage"' not in login("dir").get(f"/dossiers/{en_cours}").data.decode()


def test_parcours_complet_du_titrage(app, login):
    fp, dom, dire = login("fp"), login("dom"), login("dir")
    did = cree(login)
    assert "Étape « Journal officiel » enregistrée" in avancer(fp, did, "JO-2026/12", jour(-30), fichiers={"fichier_0": PDF}).data.decode()
    assert etape(app, did) == C.TIT_BORNAGE
    assert "Étape « Plan de bornage » enregistrée" in avancer(dom, did, "PV-77", jour(-10), fichiers={"fichier_0": PDF, "fichier_1": PDF}).data.decode()
    assert etape(app, did) == C.TIT_JC
    avancer(fp, did, "JC-2026/20", jour(-5))
    assert etape(app, did) == C.TIT_TITRAGE
    page = avancer(dom, did, "TF-0001", jour(-1)).data.decode()
    assert "Le titrage est terminé" in page and etape(app, did) == C.TIT_FIN
    with app.app_context():
        faites = EtapeTitrage.query.filter_by(dossier_id=did, type="fait").order_by(EtapeTitrage.id).all()
        assert [e.etape for e in faites] == [C.TIT_JO, C.TIT_BORNAGE, C.TIT_JC, C.TIT_TITRAGE]
        d = db.session.get(Dossier, did)
        assert sorted(x.type for x in d.documents) == ["Journal officiel", "Plan de bornage", "Procès-verbal de bornage"]
        assert d.statut == C.CERTIFIE                                       # le circuit ne change pas
    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "Titré" in page and "TF-0001" in page and "JO-2026/12" in page
    assert dire.get(f"/dossiers/{did}").status_code == 200
    # plus aucune étape possible
    assert fp.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "x", "date_etape": jour(-1)}).status_code == 403


def test_seul_l_acteur_de_l_etape_peut_agir(app, login):
    pub, dom, dire, adm, fp = login("pub"), login("dom"), login("dir"), login("admin"), login("fp")
    did = cree(login)
    data = {"reference": "X", "date_etape": jour(-1)}
    for client in (dom, dire, pub, login("priv1")):                          # étape 1 : Fonction publique
        assert client.post(f"/dossiers/{did}/titrage/avancer", data=data).status_code in (403, 404)
    assert etape(app, did) == C.TIT_JO
    assert adm.post(f"/dossiers/{did}/titrage/avancer", data=data).status_code == 302                  # l'administrateur peut se substituer
    assert fp.post(f"/dossiers/{did}/titrage/avancer", data=data).status_code == 403                    # étape 2 : Domaine
    assert dom.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "PV", "date_etape": jour(0)}).status_code == 302
    assert etape(app, did) == C.TIT_JC


def test_validations_de_l_etape(app, login):
    fp = login("fp")
    did = cree(login)
    assert "est obligatoire" in avancer(fp, did, "", jour(-1)).data.decode()
    assert "valide" in avancer(fp, did, "R", "31/02/2026").data.decode()
    assert "dans le futur" in avancer(fp, did, "R", jour(3)).data.decode()
    assert "trop long" in avancer(fp, did, "R" * 121, jour(-1)).data.decode()
    assert "Observations" in fp.get(f"/titrage/{did}").data.decode() and etape(app, did) == C.TIT_JO
    avancer(fp, did, "JO", jour(-10))
    page = avancer(login("dom"), did, "PV", jour(-20)).data.decode()                          # bornage avant la parution
    assert "ne peut pas précéder" in page and etape(app, did) == C.TIT_BORNAGE


def test_delai_minimal_entre_journal_officiel_et_bornage(app, login):
    app.config["TITRAGE_DELAI_BORNAGE_JOURS"] = 15
    did = cree(login)
    avancer(login("fp"), did, "JO", jour(-20))
    assert "moins de 15 jours" in avancer(login("dom"), did, "PV", jour(-10)).data.decode()
    assert etape(app, did) == C.TIT_BORNAGE
    avancer(login("dom"), did, "PV", jour(-4))                                                   # 16 jours après la parution
    assert etape(app, did) == C.TIT_JC


def test_numero_de_titre_unique(app, login):
    a, b = cree(login, identite="CNI-A"), cree(login, identite="CNI-B")
    fp, dom = login("fp"), login("dom")
    for did in (a, b):
        avancer(fp, did, "JO", jour(-30)); avancer(dom, did, "PV", jour(-20)); avancer(fp, did, "JC", jour(-10))
    avancer(dom, a, "TF-1", jour(-1))
    assert "déjà attribué" in avancer(dom, b, "TF-1", jour(-1)).data.decode() and etape(app, b) == C.TIT_TITRAGE


def test_scan_invalide_refuse_sans_rien_enregistrer(app, login):
    did = cree(login)
    page = avancer(login("fp"), did, "JO", jour(-3), fichiers={"fichier_0": b"pas un pdf"}).data.decode()
    assert "PDF, JPG, PNG ou TIFF valide" in page and etape(app, did) == C.TIT_JO
    with app.app_context():
        assert EtapeTitrage.query.count() == 0 and not db.session.get(Dossier, did).documents


# ------------------------------------------------------------------ retour en arrière
def test_retour_a_l_etape_precedente(app, login):
    fp, dom = login("fp"), login("dom")
    did = cree(login)
    assert fp.post(f"/dossiers/{did}/titrage/retour", data={"motif": "x"}).status_code == 403          # rien avant le Journal officiel
    avancer(fp, did, "JO-1", jour(-10))
    assert "motif" in retour(dom, did, "").data.decode().lower() and etape(app, did) == C.TIT_BORNAGE
    assert fp.post(f"/dossiers/{did}/titrage/retour", data={"motif": "x"}).status_code == 403          # pas l'acteur de l'étape à faire
    page = retour(dom, did, "Numéro de parution erroné").data.decode()
    assert "Dossier renvoyé à l" in page and "Journal officiel" in page
    assert etape(app, did) == C.TIT_JO
    with app.app_context():
        lignes = EtapeTitrage.query.filter_by(dossier_id=did).order_by(EtapeTitrage.id).all()
        assert [(e.etape, e.type, e.annule_at is not None) for e in lignes] == [(C.TIT_JO, "fait", True), (C.TIT_BORNAGE, "retour", False)]
        assert lignes[1].observations == "Numéro de parution erroné"
    avancer(fp, did, "JO-1-corrigé", jour(-9))                                                          # l'étape est refaite
    assert etape(app, did) == C.TIT_BORNAGE
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "JO-1-corrigé" in page and "Annulée" in page and "Retour" in page


def test_dossier_bloque_ne_peut_pas_avancer(app, login):
    did = cree(login)
    signaler(login("dom"), did)
    assert login("fp").post(f"/dossiers/{did}/titrage/avancer", data={"reference": "x", "date_etape": jour(-1)}).status_code == 403
    lever(login("dir"), blocage_id(app))
    assert avancer(login("fp"), did).status_code == 200 and etape(app, did) == C.TIT_BORNAGE


def test_les_mutations_restent_possibles_pendant_le_titrage(app, login):
    from app.mutations import dossier_mutable

    did = cree(login)
    with app.app_context():
        assert dossier_mutable(db.session.get(Dossier, did))


# ------------------------------------------------------------------ Fonction publique : périmètre et accès
def test_la_fonction_publique_ne_voit_que_les_dossiers_en_titrage(app, login):
    fp = login("fp")
    titre = cree(login, identite="CNI-T")
    circuit = cree(login, identite="CNI-C", certifier=False)
    ancien = cree(login, identite="CNI-A")
    with app.app_context():
        db.session.get(Dossier, ancien).etape_titre = None                  # certifié avant la mise en place du titrage
        db.session.commit()
    page = fp.get("/titrage").data.decode()
    assert numero(app, titre) in page and numero(app, circuit) not in page and numero(app, ancien) not in page
    assert fp.get(f"/titrage/{titre}").status_code == 200
    assert fp.get(f"/titrage/{circuit}").status_code == 404 and fp.get(f"/titrage/{ancien}").status_code == 404
    assert fp.get("/titrage/99999").status_code == 404


def test_acces_limite_de_la_fonction_publique(app, login):
    fp = login("fp")
    assert fp.get("/").status_code == 302 and "/bienvenue" in fp.get("/").headers["Location"]           # pas de tableau de bord
    page = fp.get("/bienvenue").data.decode()
    assert "Ouvrir le titrage" in page and "Mutations" not in page and "Propriétaires" not in page
    did = cree(login)
    for url in ("/proprietaires", "/mutations/", "/carte", "/blocages/", "/utilisateurs/", "/dossiers/anciens", "/dossiers/nouveau", "/parametres/", "/dossiers", f"/dossiers/{did}"):
        assert fp.get(url).status_code in (403, 404), url
    assert fp.get("/titrage").status_code == 200 and fp.get("/profil").status_code == 200 and fp.get("/verifier").status_code == 200
    menu = fp.get("/titrage").data.decode().split('id="menu-lateral"')[1].split("</nav>")[0]
    assert "Titrage" in menu and "Vérifier un document" in menu and 'href="/dossiers"' not in menu and "Mutations" not in menu and "Carte" not in menu


def test_la_fonction_publique_est_limitee_a_son_ile(app, login):
    compte(app, "fp.mwali", C.FONCTION_PUBLIQUE, ile_id=3)
    fp_mwali = login("fp.mwali")
    did = cree(login)                                                       # île 1
    assert fp_mwali.get(f"/titrage/{did}").status_code == 404
    assert numero(app, did) not in fp_mwali.get("/titrage").data.decode()
    assert fp_mwali.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "x", "date_etape": jour(-1)}).status_code == 404


def test_creation_du_compte_fonction_publique(app, login):
    adm = login("admin")
    r = adm.post("/utilisateurs/nouveau", data={"nom": "Agent FP", "username": "agent.fp", "role": "fonction_publique", "ile_id": "1", "password": "motdepasse-provisoire"})
    assert r.status_code == 302
    assert "Fonction publique" in adm.get("/utilisateurs/").data.decode()
    with app.app_context():
        assert User.query.filter_by(username="agent.fp").one().role_label == "Fonction publique"


# ------------------------------------------------------------------ onglets et file de travail
def test_page_titrage_onglets_et_a_traiter(app, login):
    dire, fp, dom = login("dir"), login("fp"), login("dom")
    a, b, c, d4 = (cree(login, identite=f"C{i}") for i in range(4))
    ancien = cree(login, identite="C9")
    with app.app_context():
        db.session.get(Dossier, ancien).etape_titre = None                   # certifié avant la mise en place du titrage
        db.session.commit()
    avancer(fp, b, "JO-B", jour(-9))                                        # b : plan de bornage
    avancer(fp, c, "JO", jour(-9)); avancer(dom, c, "PV", jour(-8)); avancer(fp, c, "JC", jour(-7))        # c : à titrer
    avancer(fp, d4, "JO", jour(-9)); avancer(dom, d4, "PV", jour(-8)); avancer(fp, d4, "JC", jour(-7)); avancer(dom, d4, "TF", jour(-1))  # d4 : titré

    def liste(client, etape):
        return client.get(f"/titrage?etape={etape}").data.decode().split("<tbody>")[1]

    assert numero(app, a) in liste(dire, "journal_officiel") and numero(app, b) not in liste(dire, "journal_officiel")
    assert numero(app, b) in liste(dire, "plan_bornage") and numero(app, c) in liste(dire, "titrage") and numero(app, d4) in liste(dire, "titre")
    assert all(numero(app, ancien) not in liste(dire, e) for e in C.ONGLETS_TITRAGE)                     # hors titrage
    page = dire.get("/titrage?etape=titrage").data.decode()
    tabs = page.split("onglets-statuts")[1].split("</ul>")[0]
    for libelle in ("Journal officiel", "Plan de bornage", "Journal de clôture", "À titrer", "Titrés"):
        assert libelle in tabs
    assert "à traiter" not in tabs                                          # le Directeur n'a aucune étape à accomplir
    # la Fonction publique : 1 dossier au Journal officiel (a) + 1 au Journal de clôture (c) ; le Domaine : bornage (b) + titre (c)
    assert "1 à traiter" in fp.get("/titrage").data.decode().split("onglets-statuts")[1].split("</ul>")[0]
    assert numero(app, a) in liste(fp, "journal_officiel") and ">Traiter<" in liste(fp, "journal_officiel")
    assert ">Traiter<" not in liste(fp, "plan_bornage") and ">Traiter<" in liste(dom, "plan_bornage")
    # sans onglet choisi : celui où ce compte a du travail
    assert numero(app, a) in fp.get("/titrage").data.decode().split("<tbody>")[1]
    assert numero(app, b) in dom.get("/titrage").data.decode().split("<tbody>")[1]
    # recherche, étape inconnue, topographe privé
    assert numero(app, a) in fp.get(f"/titrage?etape=journal_officiel&q={numero(app, a)}").data.decode()
    assert "Aucun dossier ne correspond" in fp.get("/titrage?etape=journal_officiel&q=zzz-introuvable").data.decode()
    assert fp.get("/titrage?etape=n-importe-quoi").status_code == 200
    assert login("priv1").get("/titrage").status_code == 404
    assert "Titrage" in dire.get("/dossiers").data.decode().split('id="menu-lateral"')[1].split("</nav>")[0]
    assert "Titrage" not in login("priv1").get("/dossiers").data.decode().split('id="menu-lateral"')[1].split("</nav>")[0]


def test_la_page_titrage_signale_les_dossiers_bloques_et_les_retards(app, login):
    fp = login("fp")
    did = cree(login)
    signaler(login("dom"), did)
    ligne = fp.get("/titrage?etape=journal_officiel").data.decode().split("<tbody>")[1]
    assert "Bloqué" in ligne and ">Traiter<" not in ligne                    # un dossier bloqué ne se traite pas
    assert "0 à traiter" not in fp.get("/titrage").data.decode()
    assert " j</td>" in ligne


def test_file_de_travail_de_la_fonction_publique_et_du_domaine(app, login):
    from app import workflow as W

    did = cree(login)
    with app.app_context():
        u = {n: User.query.filter_by(username=n).one() for n in ("fp", "dom", "dir")}
        assert [d.id for d in W.dossiers_a_traiter(u["fp"]).all()] == [did] and W.dossiers_a_traiter(u["dom"]).count() == 0
    avancer(login("fp"), did, "JO", jour(-3))
    with app.app_context():
        u = {n: User.query.filter_by(username=n).one() for n in ("fp", "dom")}
        assert W.dossiers_a_traiter(u["fp"]).count() == 0 and [d.id for d in W.dossiers_a_traiter(u["dom"]).all()] == [did]
    assert "1 dossier à traiter" in login("dom").get("/bienvenue").data.decode()


def test_audit_du_titrage(app, login):
    from app.models import AuditLog

    did = cree(login)
    avancer(login("fp"), did, "JO", jour(-3))
    retour(login("dom"), did, "Erreur")
    with app.app_context():
        actions = [a.action for a in AuditLog.query.filter(AuditLog.action.like("titrage%")).order_by(AuditLog.id)]
        assert actions == ["titrage_ouvert", "titrage_etape", "titrage_retour"]


# ------------------------------------------------------------------ page indépendante d'un dossier en titrage
def test_la_page_titrage_ouvre_le_suivi_du_dossier_et_non_sa_fiche(app, login):
    fp = login("fp")
    did = cree(login)
    liste = fp.get("/titrage?etape=journal_officiel").data.decode().split("<tbody>")[1]
    assert f'href="/titrage/{did}"' in liste and f'href="/dossiers/{did}' not in liste
    page = fp.get(f"/titrage/{did}").data.decode()
    assert "Étapes du titrage" in page and "Enregistrer l'étape" in page and "← Titrage" in page
    assert numero(app, did) in page and "Fiche du dossier" not in page          # la Fonction publique n'a pas la fiche complète
    assert "Fiche du dossier" in login("dir").get(f"/titrage/{did}").data.decode()
    assert login("priv1").get(f"/titrage/{did}").status_code == 404


def test_l_etape_enregistree_ramene_a_la_page_titrage(app, login):
    fp = login("fp")
    did = cree(login)
    r = fp.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "JO-9", "date_etape": jour(-2)})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/titrage/{did}")
    page = fp.get(f"/titrage/{did}").data.decode()
    assert "JO-9" in page and "À faire : Plan de bornage" in page and "Enregistrer l'étape « Journal officiel »" not in page


def test_la_fiche_du_dossier_n_a_plus_les_formulaires_du_titrage(app, login):
    did = cree(login)
    page = login("fp") and login("admin").get(f"/dossiers/{did}").data.decode()
    assert 'id="titrage"' in page and "Ouvrir le suivi du titrage" in page
    assert "titrage/avancer" not in page and "titrage/retour" not in page


def test_la_page_titrage_d_un_dossier_montre_les_scans_et_documents(app, login):
    did = cree(login)
    avancer(login("fp"), did, "JO", jour(-3), fichiers={"fichier_0": PDF})
    with app.app_context():
        from app.models import Document

        doc = Document.query.filter_by(dossier_id=did, type="Journal officiel").one()
        doc_id = doc.id
    page = login("dom").get(f"/titrage/{did}").data.decode()
    assert f"/documents/{doc_id}" in page and "Journal officiel" in page
    assert login("fp").get(f"/documents/{doc_id}").status_code == 200


# ------------------------------------------------------------------ blocage depuis les étapes du titrage
DESCRIPTION = "Un voisin conteste la limite nord de la parcelle."


def bloquer(client, did, type_="limites_contestees", description=DESCRIPTION):
    return client.post(f"/dossiers/{did}/titrage/bloquer", data={"type": type_, "description": description}, follow_redirects=True)


def titrer(app, login, did):
    fp, dom = login("fp"), login("dom")
    avancer(fp, did, "JO", jour(-30)); avancer(dom, did, "PV", jour(-20)); avancer(fp, did, "JC", jour(-10)); avancer(dom, did, "TF-9", jour(-1))
    assert etape(app, did) == C.TIT_FIN


def test_on_peut_bloquer_le_dossier_depuis_les_etapes_du_titrage(app, login):
    fp, dom, dire = login("fp"), login("dom"), login("dir")
    did = cree(login)
    page = fp.get(f"/titrage/{did}").data.decode()
    assert "Bloquer le dossier" in page and 'id="form-blocage"' in page
    page = bloquer(fp, did).data.decode()
    assert "Dossier bloqué" in page and DESCRIPTION in page       # on reste sur la page de suivi
    with app.app_context():
        assert db.session.get(Dossier, did).est_bloque
    # figé : aucune étape, ni retour
    assert fp.post(f"/dossiers/{did}/titrage/avancer", data={"reference": "x", "date_etape": jour(-1)}).status_code == 403
    assert "Lever ce blocage" not in page                                    # seul le Directeur lève
    page = dire.get(f"/titrage/{did}").data.decode()
    assert "Lever ce blocage" in page and 'name="depuis" value="titrage"' in page
    r = dire.post(f"/blocages/{blocage_id(app)}/lever", data={"motif": "Jugement du 12/03 : limites confirmées", "depuis": "titrage"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/titrage/{did}")                  # retour sur la page de suivi
    assert avancer(fp, did, "JO", jour(-2)).status_code == 200 and etape(app, did) == C.TIT_BORNAGE


def test_le_blocage_du_titrage_est_valide_et_trace(app, login):
    from app.models import AuditLog, Blocage

    fp = login("fp")
    did = cree(login)
    assert "Choisissez le type" in bloquer(fp, did, type_="").data.decode()
    assert "trop court" in bloquer(fp, did, description="Court").data.decode()
    with app.app_context():
        assert Blocage.query.count() == 0
    bloquer(fp, did)
    with app.app_context():
        b = Blocage.query.one()
        assert b.dossier_id == did and b.signale_par.username == "fp"
        assert AuditLog.query.filter_by(action="blocage_signale").count() == 1
    assert "1 problème actif" in login("dom").get(f"/titrage/{did}").data.decode()
    assert numero(app, did) in login("dir").get("/blocages").data.decode()                                # visible dans la page Blocages


def test_un_dossier_titre_ne_peut_plus_etre_bloque(app, login):
    from app.models import Blocage

    did = cree(login)
    titrer(app, login, did)
    for client in (login("fp"), login("dom"), login("dir"), login("admin")):
        page = client.get(f"/titrage/{did}").data.decode()
        assert 'id="form-blocage"' not in page and "ne peut plus être bloqué" in page
        assert "ne peut plus être bloqué" in bloquer(client, did).data.decode()
    assert "titré" in login("dom").post("/blocages/nouveau", data={"dossier": str(did), "type": "faux", "description": DESCRIPTION}).data.decode()
    assert "Signaler un problème" not in login("dir").get(f"/dossiers/{did}").data.decode()
    with app.app_context():
        assert Blocage.query.count() == 0


def test_blocage_du_titrage_limite_aux_dossiers_visibles(app, login):
    compte(app, "fp.mwali", C.FONCTION_PUBLIQUE, ile_id=3)
    did = cree(login)
    assert bloquer(login("fp.mwali"), did).status_code == 404
    assert login("priv1").post(f"/dossiers/{did}/titrage/bloquer", data={"type": "faux", "description": DESCRIPTION}).status_code == 404
    circuit = cree(login, identite="CNI-Z", certifier=False)
    assert login("fp").post(f"/dossiers/{circuit}/titrage/bloquer", data={"type": "faux", "description": DESCRIPTION}).status_code == 404


# ------------------------------------------------------------------ tout dossier « Certificat émis » est suivi dans « Titrage »
def test_le_dossier_issu_d_une_vente_entre_dans_le_titrage(app, login):
    from test_mutations import dossier_certifie, proprio_id, vendre, vivant

    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    nouveau = vivant(app, did)
    assert nouveau != did and etape(app, nouveau) == C.TIT_JO
    assert numero(app, nouveau) in login("fp").get("/titrage?etape=journal_officiel").data.decode()
    assert login("fp").get(f"/titrage/{nouveau}").status_code == 200


def test_tout_dossier_certifie_apparait_dans_la_page_titrage(app, login):
    certifies = [cree(login, identite=f"CNI-{i}") for i in range(3)]
    circuit = cree(login, identite="CNI-C", certifier=False)
    liste = login("dir").get("/titrage?etape=journal_officiel").data.decode().split("<tbody>")[1]
    assert all(numero(app, d) in liste for d in certifies) and numero(app, circuit) not in liste
    avancer(login("fp"), certifies[0], "JO", jour(-1))
    assert numero(app, certifies[0]) in login("dir").get("/titrage?etape=plan_bornage").data.decode().split("<tbody>")[1]


def test_la_migration_reprend_les_dossiers_deja_certifies(app, login):
    """La migration place à l'étape « Journal officiel » les dossiers certifiés avant la mise en place du titrage."""
    import importlib.util
    import os

    ancien = cree(login, identite="CNI-OLD")
    en_cours = cree(login, identite="CNI-OLD2", certifier=False)
    with app.app_context():
        db.session.get(Dossier, ancien).etape_titre = None                   # état d'avant le titrage
        db.session.commit()
        chemin = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions", "a4d8f2c6e913_titrage_des_dossiers_deja_certifies.py")
        spec = importlib.util.spec_from_file_location("migration_titrage", chemin)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        executes = []
        module.op = type("Op", (), {"execute": staticmethod(lambda sql: db.session.execute(db.text(sql)))})
        module.upgrade()
        db.session.commit()
        assert db.session.get(Dossier, ancien).etape_titre == C.TIT_JO
        assert db.session.get(Dossier, en_cours).etape_titre is None         # un dossier encore dans le circuit n'y entre pas


# ------------------------------------------------------------------ les topographes n'ont pas accès au titrage
def test_les_topographes_n_ont_pas_acces_au_titrage(app, login):
    did = cree(login)
    for nom in ("pub", "priv1", "priv2"):
        client = login(nom)
        assert client.get("/titrage").status_code == 404 and client.get(f"/titrage/{did}").status_code == 404, nom
        assert client.post(f"/dossiers/{did}/titrage/bloquer", data={"type": "faux", "description": DESCRIPTION}).status_code == 404
        assert client.get(f"/dossiers/{did}/titrage/bornage/1/telecharger").status_code == 404
    pub = login("pub")
    menu = pub.get("/dossiers").data.decode().split('id="menu-lateral"')[1].split("</nav>")[0]
    assert "Titrage" not in menu and "/titrage" not in menu
    assert "Titrage" not in pub.get("/bienvenue").data.decode().split("Accès rapides")[1]
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert 'id="titrage"' not in page and "Ouvrir le suivi du titrage" not in page
    assert "À faire : Journal officiel" not in pub.get("/dossiers").data.decode()                   # pas non plus dans la liste
    assert pub.get(f"/dossiers/{did}").status_code == 200                                              # la fiche du dossier reste accessible
    assert nb_a_traiter_est_nul(app, "pub")


def nb_a_traiter_est_nul(app, username):
    from app import titrage as T

    with app.app_context():
        return T.nb_a_traiter(User.query.filter_by(username=username).one()) == 0


def test_les_autres_comptes_gardent_l_acces(app, login):
    did = cree(login)
    for nom in ("admin", "dir", "dom", "fp"):
        client = login(nom)
        assert client.get("/titrage").status_code == 200 and client.get(f"/titrage/{did}").status_code == 200, nom
    for nom in ("admin", "dir", "dom"):
        menu = login(nom).get("/dossiers").data.decode().split('id="menu-lateral"')[1].split("</nav>")[0]
        assert "Titrage" in menu, nom
    assert "À faire : Journal officiel" in login("dir").get("/dossiers").data.decode()
