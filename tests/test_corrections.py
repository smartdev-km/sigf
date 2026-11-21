import json
import re

import pytest

from app import constants as C
from app import corrections as Cor
from app import mutations as M
from app import stats as St
from app.extensions import db
from app.models import AuditLog, DocumentEmis, EtapeLog, Mutation, Proprietaire, maintenant

from conftest import agir
from test_mutations import ACTE, benef, derniere_mutation_id, dossier_certifie, proprio_id, saisir, vendre, vivant
from test_partages import cree, derniere_id, diviser, numero, statut, valider
from test_verification import dernier_code, imprimer


def corriger(client, did, motif="Faute de frappe relevée sur le titre", **champs):
    data = {"motif": motif, **champs}
    return client.post(f"/dossiers/{did}/corriger", data=data)


def parcelle(app, did):
    from app.models import Dossier

    with app.app_context():
        p = db.session.get(Dossier, did).parcelle
        return {"ile": p.ile_id, "commune": p.commune, "village": p.village, "reference": p.reference, "superficie": p.superficie_m2, "titre": p.type_titre}


def en_cours(login, **kw):
    return cree(login, certifier=False, **kw)


def pid(app, did):
    from app.models import Dossier

    with app.app_context():
        return db.session.get(Dossier, did).proprietaire_id


def valider_derniere(app, login):
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider", follow_redirects=True)


# ================================================================= qui peut corriger
def test_droits_selon_le_role_et_l_etat(app, login):
    cours = en_cours(login)
    certif = dossier_certifie(login)
    for qui in ("pub", "dom", "dir", "admin"):
        assert login(qui).get(f"/dossiers/{cours}/corriger").status_code == 200, qui       # en cours : correction directe
    assert login("priv1").get(f"/dossiers/{cours}/corriger").status_code == 404           # pas son dossier

    assert login("dom").get(f"/dossiers/{certif}/corriger").status_code == 200            # certifié : le Domaine propose
    for qui in ("pub", "dir", "admin"):
        assert login(qui).get(f"/dossiers/{certif}/corriger").status_code == 403, qui     # séparation des tâches
    assert app.test_client().get(f"/dossiers/{cours}/corriger").status_code == 302

    page_cours = login("pub").get(f"/dossiers/{cours}").data.decode()
    assert "Corriger</a>" in page_cours and "Demander une correction" not in page_cours
    page_certif = login("dom").get(f"/dossiers/{certif}").data.decode()
    assert "Demander une correction" in page_certif
    for qui in ("pub", "dir", "admin"):                                                      # pas de bouton pour ceux qui ne peuvent pas
        assert "Demander une correction" not in login(qui).get(f"/dossiers/{certif}").data.decode(), qui


def test_topographe_prive_corrige_son_dossier_avant_son_avis(app, login):
    p1, p2 = login("priv1"), login("priv2")
    did = p1.post("/dossiers/nouveau", data={"proprio_nom": "Prive", "ile_id": "1", "commune": "Moroni"})
    did = int(did.headers["Location"].rstrip("/").split("/")[-1])
    assert corriger(p1, did, p_commune="Itsandra").status_code == 302 and parcelle(app, did)["commune"] == "Itsandra"
    assert p2.get(f"/dossiers/{did}/corriger").status_code == 404                          # celui d'un autre
    agir(p1, did, "terminer_plan")
    agir(p1, did, "envoyer_avis", commentaire="OK")
    assert corriger(p1, did, p_commune="Autre").status_code == 403                         # après l'avis, il est sorti du circuit
    assert parcelle(app, did)["commune"] == "Itsandra"


def test_un_dossier_cloture_n_est_plus_modifiable(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    for qui in ("pub", "dom", "dir", "admin"):
        assert login(qui).get(f"/dossiers/{did}/corriger").status_code == 403, qui
    assert corriger(login("dom"), did, p_commune="X").status_code == 403
    assert parcelle(app, did)["commune"] == "Moroni"
    assert login("dom").get(f"/dossiers/{vivant(app, did)}/corriger").status_code == 200   # le dossier à jour, lui, se corrige


# ================================================================= correction directe (dossier en cours)
def test_correction_directe_des_donnees_de_la_parcelle(app, login):
    did = en_cours(login)
    pub = login("pub")
    r = corriger(pub, did, "Erreur de saisie à la création", p_commune="Mitsamiouli", p_village="Bambao", p_reference="P-0099",
                 p_superficie_m2="1 250,5", p_type_titre="Titre foncier", p_ile_id="3")
    assert r.status_code == 302
    assert parcelle(app, did) == {"ile": 3, "commune": "Mitsamiouli", "village": "Bambao", "reference": "P-0099", "superficie": 1250.5, "titre": "Titre foncier"}
    with app.app_context():
        log = EtapeLog.query.filter_by(dossier_id=did, type="correction").one()
        assert log.user.username == "pub" and "Erreur de saisie" in log.commentaire
        assert "Commune : Moroni → Mitsamiouli" in log.commentaire and "Superficie déclarée (m²) : — → 1 250,5 m²" in log.commentaire
        a = AuditLog.query.filter_by(action="correction_dossier").one()
        assert "motif : Erreur de saisie" in a.nouveau and json.loads(a.ancien)["parcelle"]["commune"] == ["Moroni", "Mitsamiouli"]
        assert Mutation.query.count() == 0                                                  # directe : pas de mutation
    page = pub.get(f"/dossiers/{did}").data.decode()
    assert "Mitsamiouli" in page and "Correction" in page and "Erreur de saisie" in page


def test_correction_directe_du_proprietaire(app, login):
    did = en_cours(login)
    p = pid(app, did)
    corriger(login("dom"), did, **{f"o{p}_nom": "Saïd", f"o{p}_prenom": "Aly", f"o{p}_identite": "CNI-9", f"o{p}_telephone": "+269 111", f"o{p}_adresse": "Rue 5"})
    with app.app_context():
        pers = db.session.get(Proprietaire, p)
        assert (pers.nom, pers.prenom, pers.identite, pers.telephone, pers.adresse) == ("Saïd", "Aly", "CNI-9", "+269 111", "Rue 5")
    assert "Saïd Aly" in login("dir").get(f"/dossiers/{did}").data.decode()
    assert "Saïd Aly" in login("dir").get("/dossiers?q=Aly").data.decode()                  # la recherche suit


@pytest.mark.parametrize("champs, attendu", [
    ({"p_commune": ""}, "commune est obligatoire"),
    ({"p_commune": "x" * 121}, "trop long"),
    ({"p_superficie_m2": "abc"}, "doit être un nombre"),
    ({"p_superficie_m2": "-5"}, "nombre positif"),
    ({"p_superficie_m2": "0"}, "nombre positif"),
    ({"p_ile_id": "999"}, "île choisie n'existe pas"),
    ({"p_ile_id": "abc"}, "île choisie n'existe pas"),
    ({"p_type_titre": "Inconnu"}, "Type de titre inconnu"),
    ({"p_reference": "x" * 81}, "trop long"),
    ({"o{P}_nom": ""}, "Nom est obligatoire"),
    ({"o{P}_telephone": "9" * 41}, "trop long"),
    ({"o{P}_identite": "x" * 81}, "trop long"),
    ({"p_commune": "Moroni"}, "Aucune modification"),                                       # même valeur : rien à corriger
])
def test_corrections_invalides_refusees_sans_rien_changer(app, login, champs, attendu):
    did = en_cours(login)
    p = pid(app, did)
    champs = {k.replace("{P}", str(p)): v for k, v in champs.items()}
    avant = parcelle(app, did)
    r = corriger(login("pub"), did, **champs)
    assert r.status_code == 400 and attendu in r.data.decode().replace("&#39;", "'")
    assert parcelle(app, did) == avant
    with app.app_context():
        assert EtapeLog.query.filter_by(type="correction").count() == 0 and db.session.get(Proprietaire, p).nom == "Said"


def test_motif_obligatoire(app, login):
    did = en_cours(login)
    for motif in ("", "   "):
        r = login("pub").post(f"/dossiers/{did}/corriger", data={"p_commune": "Autre", "motif": motif})
        assert r.status_code == 400 and "motif" in r.data.decode().lower()
    assert login("pub").post(f"/dossiers/{did}/corriger", data={"p_commune": "Autre"}).status_code == 400
    assert parcelle(app, did)["commune"] == "Moroni"


def test_on_ne_peut_corriger_ni_les_parts_ni_un_autre_proprietaire(app, login):
    did = en_cours(login)
    autre = en_cours(login, nom="Autre", identite="X-2")
    p_autre = pid(app, autre)
    r = corriger(login("pub"), did, **{f"o{p_autre}_nom": "Pirate", "parts": "1/2", "statut": "certifie", "rsp": "X"})
    assert r.status_code == 400 and "Aucune modification" in r.data.decode()               # les champs étrangers sont ignorés
    with app.app_context():
        assert db.session.get(Proprietaire, p_autre).nom == "Autre"
    assert statut(app, did) == C.PLAN_CROQUIS


def test_les_champs_non_envoyes_ne_sont_pas_touches(app, login):
    did = en_cours(login, superficie="500")
    corriger(login("pub"), did, p_village="Nouveau village")
    p = parcelle(app, did)
    assert p["village"] == "Nouveau village" and p["superficie"] == 500 and p["commune"] == "Moroni"


# ================================================================= correction d'un dossier certifié : le Directeur valide
def test_correction_d_un_dossier_certifie_passe_par_le_directeur(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    p = proprio_id(app, did)
    avant = parcelle(app, did)

    r = corriger(dom, did, "Nom mal orthographié sur l'acte", p_commune="Mitsamiouli", **{f"o{p}_nom": "Saïd"})
    assert r.status_code == 302 and "#tracabilite" in r.headers["Location"]
    mid = derniere_mutation_id(app)
    assert parcelle(app, did) == avant                                                      # rien ne change avant la validation
    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.type, m.statut, m.seq) == (C.MUT_RECTIFICATION, C.MS_ATTENTE, None) and m.notes.startswith("Nom mal")
        assert json.loads(m.details)["corrections"]["parcelle"]["commune"] == ["Moroni", "Mitsamiouli"]
        assert db.session.get(Proprietaire, p).nom == "Said"

    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "Moroni" in page and "Mitsamiouli" in page and "En attente de validation" in page and "Valider" in page
    assert "Correction des données" in page                                                 # titre explicite dans l'historique
    assert "Motif : Nom mal orthographié" in page

    dom.post(f"/mutations/{mid}/valider")                                                    # le Domaine ne valide pas
    assert parcelle(app, did) == avant
    dire.post(f"/mutations/{mid}/valider")
    assert parcelle(app, did)["commune"] == "Mitsamiouli"
    with app.app_context():
        assert db.session.get(Proprietaire, p).nom == "Saïd"
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq) == (C.MS_VALIDEE, 1) and M.verifier_chaine(m.dossier) == (True, None)
        actions = [a.action for a in AuditLog.query.order_by(AuditLog.id)]
        assert "mutation_saisie" in actions and "correction_appliquee" in actions and "mutation_validee" in actions
    assert "Saïd" in dire.get(f"/dossiers/{did}").data.decode()


def test_un_document_imprime_avant_la_correction_devient_non_a_jour(app, login):
    did = dossier_certifie(login)
    imprimer(login("dir"), did, "certificat")
    code = dernier_code(app)
    anonyme = app.test_client()
    assert "Document authentique" in anonyme.get(f"/verifier/{code}").data.decode()
    corriger(login("dom"), did, p_commune="Mitsamiouli")
    valider_derniere(app, login)
    page = anonyme.get(f"/verifier/{code}").data.decode()
    assert "non à jour" in page and "Moroni" in page and "Mitsamiouli" not in page          # le document montre ce qui était imprimé
    imprimer(login("dir"), did, "certificat")                                                # un nouvel exemplaire porte la valeur corrigée
    assert "Mitsamiouli" in anonyme.get(f"/verifier/{dernier_code(app)}").data.decode()


def test_rejet_d_une_correction(app, login):
    did = dossier_certifie(login)
    corriger(login("dom"), did, p_commune="Autre")
    mid = derniere_mutation_id(app)
    login("dir").post(f"/mutations/{mid}/rejeter", data={"motif": "Non justifié"})
    assert parcelle(app, did)["commune"] == "Moroni"
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_REJETEE


def test_deux_corrections_en_conflit(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    corriger(dom, did, p_commune="Itsandra")
    premiere = derniere_mutation_id(app)
    corriger(dom, did, p_commune="Mitsamiouli")
    seconde = derniere_mutation_id(app)
    dire.post(f"/mutations/{premiere}/valider")
    assert parcelle(app, did)["commune"] == "Itsandra"
    r = dire.post(f"/mutations/{seconde}/valider", follow_redirects=True)
    assert "a changé depuis la saisie" in r.data.decode()                                    # la seconde n'est plus applicable telle quelle
    assert parcelle(app, did)["commune"] == "Itsandra"
    with app.app_context():
        assert db.session.get(Mutation, seconde).statut == C.MS_ATTENTE


def test_correction_en_attente_rejetee_si_le_dossier_est_clôturé_entre_temps(app, login):
    did = dossier_certifie(login)
    corriger(login("dom"), did, p_commune="Autre")
    attente = derniere_mutation_id(app)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    with app.app_context():
        m = db.session.get(Mutation, attente)
        assert m.statut == C.MS_REJETEE and "clôturé" in m.motif_rejet
    assert parcelle(app, did)["commune"] == "Moroni"


def test_correction_d_un_ancien_dossier(app, login):
    import io
    dom, dire = login("dom"), login("dir")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": (io.BytesIO(b"%PDF-1.4 x"), "t.pdf")})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    assert corriger(dom, did, p_commune="Mutsamudu").status_code == 302
    assert parcelle(app, did)["commune"] == "Domoni"
    valider_derniere(app, login)
    assert parcelle(app, did)["commune"] == "Mutsamudu"


def test_la_chaine_signee_protege_le_contenu_de_la_correction(app, login):
    did = dossier_certifie(login)
    corriger(login("dom"), did, p_commune="Itsandra")
    valider_derniere(app, login)
    from app.models import Dossier

    with app.app_context():
        assert M.verifier_chaine(db.session.get(Dossier, did)) == (True, None)
        m = Mutation.query.filter_by(type=C.MUT_RECTIFICATION).one()
        m.details = m.details.replace("Itsandra", "Pirateville")                              # on réécrit la correction en base
        db.session.commit()
        assert M.verifier_chaine(db.session.get(Dossier, did)) == (False, 1)


# ================================================================= personne partagée, île, statistiques
def test_une_personne_partagee_est_signalee_et_corrigee_partout(app, login):
    did = dossier_certifie(login)
    diviser(login("dom"), did, [{"nom": "A"}, {"nom": "B"}])
    valider(login, derniere_id(app, C.MUT_DIVISION))
    with app.app_context():
        from app.models import Filiation
        fille = Filiation.query.filter_by(type="division").first().resultat_id
    p = pid(app, fille)
    page = login("pub").get(f"/dossiers/{fille}/corriger").data.decode()
    assert "aussi dans 2 autres dossiers" in page and numero(app, did) in page and "(clôturé)" in page    # la mère (clôturée) et la parcelle sœur
    corriger(login("pub"), fille, **{f"o{p}_nom": "Saïd"})                                  # la fille est en cours : correction directe
    with app.app_context():
        from app.models import Dossier
        assert db.session.get(Dossier, did).proprietaire_label == "Saïd Ali"                # l'historique de la mère suit (même personne)


def test_changer_d_ile_met_a_jour_les_statistiques(app, login):
    did = en_cours(login)
    with app.app_context():
        assert {l["ile"]: l["valeur"] for l in St.par_ile()}[1] == 1
    corriger(login("pub"), did, p_ile_id="2")
    with app.app_context():
        assert {l["ile"]: l["valeur"] for l in St.par_ile()} == {1: 0, 2: 1, 3: 0}


def test_les_pdf_utilisent_les_valeurs_corrigees(app, login):
    from io import BytesIO
    from pypdf import PdfReader

    did = dossier_certifie(login)
    corriger(login("dom"), did, p_commune="Mitsamiouli", p_superficie_m2="777")
    valider_derniere(app, login)
    r = login("dir").get(f"/dossiers/{did}/imprimer/certificat")
    texte = "\n".join(p.extract_text() for p in PdfReader(BytesIO(r.data)).pages)
    assert "Mitsamiouli" in texte and "777" in texte and "Moroni" not in texte


# ================================================================= interface
def test_formulaire_prerempli_et_bandeaux(app, login):
    did = en_cours(login, superficie="1200")
    page = login("pub").get(f"/dossiers/{did}/corriger").data.decode()
    assert 'name="p_commune"' in page and 'value="Moroni"' in page and 'value="1200"' in page
    assert f'name="o{pid(app, did)}_nom"' in page and 'value="Said"' in page and 'name="motif"' in page and "csrf_token" in page
    assert "immédiatement" in page and "Appliquer la correction" in page
    certif = dossier_certifie(login)
    page = login("dom").get(f"/dossiers/{certif}/corriger").data.decode()
    assert "soumise au Directeur" in page and "Soumettre au Directeur" in page and "Appliquer la correction" not in page


def test_en_cas_d_erreur_le_formulaire_garde_la_saisie(app, login):
    did = en_cours(login)
    r = corriger(login("pub"), did, "Mon motif conservé", p_commune="Nouvelle commune", p_superficie_m2="pas un nombre")
    page = r.data.decode()
    assert r.status_code == 400 and "Nouvelle commune" in page and "Mon motif conservé" in page and "pas un nombre" in page


def test_la_carte_correction_de_la_page_mutations(app, login):
    certif = dossier_certifie(login)
    dom = login("dom")
    assert "Correction" in dom.get("/mutations").data.decode() and "/mutations/nouvelle?type=rectification" in dom.get("/mutations").data.decode()
    choix = dom.get("/mutations/nouvelle?type=rectification&q=Said").data.decode()
    assert f"/dossiers/{certif}/corriger" in choix                                          # le lien mène à la correction
    form = dom.get(f"/dossiers/{certif}/mutations/nouvelle").data.decode()
    assert 'value="rectification"' not in form and 'value="vente"' in form                  # plus de « rectification texte » dans le formulaire
    assert saisir(dom, certif, "rectification", notes="Ancienne forme, toujours acceptée").status_code == 302   # historique / compatibilité


def test_la_liste_des_mutations_resume_la_correction(app, login):
    did = dossier_certifie(login)
    corriger(login("dom"), did, p_commune="Itsandra", p_village="Bambao")
    page = login("dir").get("/mutations").data.decode()
    assert "Rectification" in page and "Commune, Village / quartier" in page


def test_noms_hostiles_echappes_dans_la_correction(app, login):
    did = dossier_certifie(login)
    p = proprio_id(app, did)
    corriger(login("dom"), did, **{f"o{p}_nom": "<img src=x onerror=alert(1)>"})
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "<img src=x" not in page and "&lt;img src=x" in page
