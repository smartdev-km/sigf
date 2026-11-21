import io
from datetime import date, timedelta
from fractions import Fraction

import pytest

from app import constants as C
from app import mutations as M
from app.extensions import db
from app.models import AuditLog, Document, Dossier, Filiation, Mutation, Proprietaire, User

from conftest import agir, creer_dossier

ACTE = "2020-05-01"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"


# ----------------------------------------------------------------- aides
def dossier_certifie(login):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    did = creer_dossier(pub)  # propriétaire d'origine : « Said Ali »
    agir(pub, did, "terminer_plan")
    agir(pub, did, "saisir_rsp", valeur="RTC-1")
    agir(dom, did, "saisir_rcp", valeur="RTS-1")
    agir(dire, did, "valider")
    return did


def proprio_id(app, did, nom="Said"):
    with app.app_context():
        return Proprietaire.query.filter_by(nom=nom).first().id


def benef(*lignes):
    """lignes = (nom, part) ou (nom, part, 'existant:ID') -> champs de formulaire parallèles."""
    d = {k: [] for k in ("b_existant", "b_nom", "b_prenom", "b_identite", "b_telephone", "b_part")}
    for ligne in lignes:
        nom, part = ligne[0], ligne[1]
        existant = ligne[2] if len(ligne) > 2 else ""
        d["b_existant"].append(str(existant))
        d["b_nom"].append("" if existant else nom)
        d["b_prenom"].append("")
        d["b_identite"].append("")
        d["b_telephone"].append("")
        d["b_part"].append(part)
    return d


def saisir(client, did, type_="vente", **champs):
    data = {"type": type_, "date_acte": ACTE, "reference_acte": "Acte notarié n°12"}
    data.update(champs)
    return client.post(f"/dossiers/{did}/mutations/nouvelle", data=data)


def derniere_mutation_id(app):
    with app.app_context():
        return Mutation.query.order_by(Mutation.id.desc()).first().id


def vivant(app, did):
    """Dossier À JOUR : un transfert de propriété crée un nouveau dossier et clôture l'ancien, on suit donc la filiation."""
    with app.app_context():
        while True:
            suite = Filiation.query.filter(Filiation.source_id == did, Filiation.type.in_(C.TRANSFERTS)).first()
            if suite is None:
                return did
            did = suite.resultat_id


def vendre(app, login, did, cedant_id, part_cedee, *acheteurs, valider_aussi=True):
    """Vend sur le dossier à jour (celui qui est encore ouvert) et retourne l'id de la mutation."""
    did = vivant(app, did)
    r = saisir(login("dom"), did, "vente", **{f"cede_{cedant_id}": part_cedee}, **benef(*acheteurs), montant="5000000")
    assert r.status_code == 302, r.data.decode()
    mid = derniere_mutation_id(app)
    if valider_aussi:
        login("dir").post(f"/mutations/{mid}/valider")
    return mid


def proprietaires(app, did):
    """Propriétaires actuels de la parcelle (du dossier à jour)."""
    did = vivant(app, did)
    with app.app_context():
        return {p.nom_complet: f for p, f in db.session.get(Dossier, did).proprietaires_actuels}


# ----------------------------------------------------------------- cycle de base
def test_vente_saisie_puis_validee_change_le_proprietaire(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    mid = vendre(app, login, did, said, "tout", ("Moussa", "1"), valider_aussi=False)

    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq, m.hash) == (C.MS_ATTENTE, None, None)
    assert proprietaires(app, did) == {"Said Ali": 1}          # rien ne change tant que non validée

    login("dir").post(f"/mutations/{mid}/valider")
    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq, m.hash_precedent) == (C.MS_VALIDEE, 1, M.GENESE)
        assert len(m.hash) == 64 and m.decision_par.role == C.DIRECTEUR
        assert M.verifier_chaine(db.session.get(Dossier, did)) == (True, None)
    assert proprietaires(app, did) == {"Moussa": 1}
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "Historique intact" in page and "Moussa" in page and "Said Ali cède" in page


def test_heritage_en_indivision(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    r = saisir(login("dom"), did, "heritage", **{f"cede_{said}": "tout"}, **benef(("Amina", "1/3"), ("Bacar", "1/3"), ("Chaka", "1/3")))
    assert r.status_code == 302
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert proprietaires(app, did) == {"Amina": Fraction(1, 3), "Bacar": Fraction(1, 3), "Chaka": Fraction(1, 3)}
    page = login("dom").get(f"/dossiers/{vivant(app, did)}").data.decode()          # le dossier à jour, créé par l'héritage
    assert "Propriétaires actuels" in page and "1/3" in page


def test_ventes_successives_creent_une_chaine_de_dossiers(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    d1 = vivant(app, did)
    assert d1 != did and proprietaires(app, did) == {"Said Ali": Fraction(1, 2), "Moussa": Fraction(1, 2)}
    vendre(app, login, did, proprio_id(app, did, "Moussa"), "1/4", ("Nadia", "1/4"))
    d2 = vivant(app, did)
    assert d2 not in (did, d1)
    assert proprietaires(app, did) == {"Said Ali": Fraction(1, 2), "Moussa": Fraction(1, 4), "Nadia": Fraction(1, 4)}
    with app.app_context():
        assert [db.session.get(Dossier, x).statut for x in (did, d1, d2)] == [C.CLOTURE, C.CLOTURE, C.CERTIFIE]
        assert all(M.verifier_chaine(db.session.get(Dossier, x)) == (True, None) for x in (did, d1, d2))
        liens = [(f.source_id, f.resultat_id, f.type) for f in Filiation.query.order_by(Filiation.id)]
        assert liens == [(did, d1, "vente"), (d1, d2, "vente")]
        # chaque dossier a sa propre chaîne : la vente validée est la n°1 de celui qui la porte
        assert [m.seq for m in Mutation.query.filter_by(type="vente").order_by(Mutation.id)] == [1, 1]


def test_acheteur_deja_connu_du_dossier(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    moussa = proprio_id(app, did, "Moussa")
    vendre(app, login, did, said, "1/4", ("", "1/4", moussa))     # Moussa rachète une part
    assert proprietaires(app, did) == {"Said Ali": Fraction(1, 4), "Moussa": Fraction(3, 4)}
    with app.app_context():
        assert Proprietaire.query.filter_by(nom="Moussa").count() == 1  # pas de doublon


# ----------------------------------------------------------------- règles refusées
@pytest.mark.parametrize(
    "champs, attendu",
    [
        ({"cede_X": "1/2", **benef(("M", "1/4"))}, "total des parts"),                  # parts inégales
        ({"cede_X": "tout"}, "au moins un cédant et un bénéficiaire"),                   # pas de bénéficiaire
        ({**benef(("M", "1"))}, "au moins un cédant"),                                   # pas de cédant
        ({"cede_X": "abc", **benef(("M", "1"))}, "invalide"),
        ({"cede_X": "0", **benef(("M", "1"))}, "supérieure à 0"),
        ({"cede_X": "3/2", **benef(("M", "1"))}, "ne pas dépasser"),
        ({"cede_X": "1/0", **benef(("M", "1"))}, "invalide"),
        ({"cede_X": "tout", **benef(("M", "1/2"), ("N", "1/2", 999999))}, "pas connu"),   # bénéficiaire inconnu
        ({"cede_X": "tout", **benef(("", "1"))}, "nom d'un bénéficiaire"),               # sans nom
        ({"cede_X": "tout", **benef(("M", ""))}, "part d'un bénéficiaire est obligatoire"),
        ({"cede_X": "tout", "reference_acte": "", **benef(("M", "1"))}, "référence de l'acte"),
        ({"cede_X": "tout", "date_acte": (date.today() + timedelta(days=2)).isoformat(), **benef(("M", "1"))}, "futur"),
        ({"cede_X": "tout", "date_acte": "pas-une-date", **benef(("M", "1"))}, "date de l'acte"),
        ({"cede_X": "tout", "montant": "-5", **benef(("M", "1"))}, "entier positif"),
        ({"cede_X": "tout", **benef(("M", "1/2", ""), ("N", "1/2"), ("M", "1/2"))}, "total des parts"),
    ],
)
def test_mutations_invalides_refusees_sans_rien_creer(app, login, champs, attendu):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    champs = {(k.replace("X", str(said)) if k.startswith("cede_") else k): v for k, v in champs.items()}
    r = saisir(login("dom"), did, "vente", **champs)
    assert r.status_code == 400, attendu
    assert attendu in r.data.decode().replace("&#39;", "'")
    with app.app_context():
        assert Mutation.query.count() == 0
        assert Proprietaire.query.count() == 1  # aucun bénéficiaire orphelin créé


def test_cedant_ne_peut_ceder_plus_que_sa_part(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    # la vente porte maintenant sur le dossier à jour (celui créé par la première vente)
    r = saisir(login("dom"), vivant(app, did), "vente", **{f"cede_{said}": "3/4"}, **benef(("Nadia", "3/4")))
    assert r.status_code == 400 and "ne détient pas" in r.data.decode()
    # et l'ancien dossier, clôturé, n'accepte plus aucune mutation
    assert saisir(login("dom"), did, "vente", **{f"cede_{said}": "1/4"}, **benef(("Nadia", "1/4"))).status_code == 403


def test_cedant_et_beneficiaire_ne_peuvent_pas_etre_la_meme_personne(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    r = saisir(login("dom"), did, "vente", **{f"cede_{said}": "1/2"}, **benef(("", "1/2", said)))
    assert r.status_code == 400 and "à la fois cédant et bénéficiaire" in r.data.decode()


# ----------------------------------------------------------------- séparation des rôles
def test_seul_le_domaine_saisit_seul_le_directeur_valide(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    corps = {f"cede_{said}": "tout", **benef(("M", "1"))}
    for qui in ("pub", "dir", "admin"):
        assert saisir(login(qui), did, "vente", **corps).status_code == 403, qui
    assert saisir(login("priv1"), did, "vente", **corps).status_code == 404
    assert saisir(login("dom"), did, "vente", **corps).status_code == 302
    mid = derniere_mutation_id(app)
    for qui in ("dom", "pub", "admin"):
        login(qui).post(f"/mutations/{mid}/valider")
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_ATTENTE
    assert login("priv1").post(f"/mutations/{mid}/valider").status_code == 404


def test_on_ne_valide_pas_sa_propre_saisie(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    saisir(login("dom"), did, "vente", **{f"cede_{said}": "tout"}, **benef(("M", "1")))
    with app.app_context():
        m = Mutation.query.one()
        dire = User.query.filter_by(username="dir").one()
        m.saisi_par_id = dire.id            # cas limite : le Directeur est l'auteur de la saisie
        db.session.commit()
        with pytest.raises(M.MutationError, match="que vous avez saisie"):
            M.valider(dire, m)


def test_dossier_en_cours_de_circuit_sans_mutation(app, login):
    pub = login("pub")
    did = creer_dossier(pub)                      # plan croquis en cours
    assert login("dom").get(f"/dossiers/{did}/mutations/nouvelle").status_code == 403


def test_une_mutation_validee_ne_peut_etre_revalidee_ni_rejetee(app, login):
    did = dossier_certifie(login)
    mid = vendre(app, login, did, proprio_id(app, did), "tout", ("M", "1"))
    dire = login("dir")
    dire.post(f"/mutations/{mid}/valider")
    dire.post(f"/mutations/{mid}/rejeter", data={"motif": "trop tard"})
    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq, m.motif_rejet) == (C.MS_VALIDEE, 1, None)


# ----------------------------------------------------------------- rejet
def test_rejet_exige_un_motif_et_ne_change_rien(app, login):
    did = dossier_certifie(login)
    mid = vendre(app, login, did, proprio_id(app, did), "tout", ("M", "1"), valider_aussi=False)
    dire = login("dir")
    dire.post(f"/mutations/{mid}/rejeter", data={"motif": "  "})
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_ATTENTE
    assert login("dom").post(f"/mutations/{mid}/rejeter", data={"motif": "x"}).status_code in (200, 302)
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_ATTENTE   # le Domaine ne rejette pas
    dire.post(f"/mutations/{mid}/rejeter", data={"motif": "Acte illisible"})
    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq, m.motif_rejet) == (C.MS_REJETEE, None, "Acte illisible")
    assert proprietaires(app, did) == {"Said Ali": 1}
    dire.post(f"/mutations/{mid}/valider")            # une mutation rejetée ne se valide plus
    with app.app_context():
        assert db.session.get(Mutation, mid).statut == C.MS_REJETEE


def test_validation_reverifie_l_etat_actuel(app, login):
    """Deux ventes en attente de la même parcelle entière : la première clôture le dossier, la seconde est rejetée."""
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    m1 = vendre(app, login, did, said, "tout", ("Acheteur A", "1"), valider_aussi=False)
    m2 = vendre(app, login, did, said, "tout", ("Acheteur B", "1"), valider_aussi=False)
    dire = login("dir")
    dire.post(f"/mutations/{m1}/valider")
    dire.post(f"/mutations/{m2}/valider")            # déjà rejetée : sans effet
    with app.app_context():
        assert db.session.get(Mutation, m1).statut == C.MS_VALIDEE
        deuxieme = db.session.get(Mutation, m2)
        assert deuxieme.statut == C.MS_REJETEE and deuxieme.seq is None and "clôturé" in deuxieme.motif_rejet
    assert proprietaires(app, did) == {"Acheteur A": 1}
    with app.app_context():
        assert Dossier.query.count() == 2             # l'original et son successeur : « Acheteur B » n'a rien créé


# ----------------------------------------------------------------- hypothèque / mainlevée
def test_hypotheque_puis_mainlevee(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    r = saisir(dom, did, "hypotheque", creancier_nom="Banque BIC", montant="10000000")
    assert r.status_code == 302
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    hyp_id = derniere_mutation_id(app)
    assert proprietaires(app, did) == {"Said Ali": 1}                       # la propriété ne change pas
    with app.app_context():
        assert [c.id for c in M.etat(db.session.get(Dossier, did)).charges] == [hyp_id]
    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "Charges actives" in page and "Banque BIC" in page and "10 000 000" in page

    r = saisir(dom, did, "mainlevee", lie_a_id=str(hyp_id), reference_acte="Mainlevée n°7")
    assert r.status_code == 302
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    with app.app_context():
        assert M.etat(db.session.get(Dossier, did)).charges == []
    # plus de charge : une 2e mainlevée est refusée
    r = saisir(dom, did, "mainlevee", lie_a_id=str(hyp_id), reference_acte="Mainlevée bis")
    assert r.status_code == 400 and "plus active" in r.data.decode()


def test_hypotheque_exige_creancier_et_montant(app, login):
    did = dossier_certifie(login)
    dom = login("dom")
    assert saisir(dom, did, "hypotheque", montant="100").status_code == 400
    assert saisir(dom, did, "hypotheque", creancier_nom="Banque").status_code == 400
    assert saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="0").status_code == 400


# ----------------------------------------------------------------- annulation (pile)
def test_annulation_de_la_derniere_mutation_seulement(app, login):
    """L'annulation vise la dernière mutation qui reste DANS le dossier (hypothèque, rectification…)."""
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")

    def valider_derniere():
        dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")

    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000000")
    valider_derniere()
    saisir(dom, did, "rectification", notes="Orthographe du nom")
    valider_derniere()

    assert saisir(dom, did, "annulation").status_code == 400                       # motif obligatoire
    assert saisir(dom, did, "annulation", notes="Rectification abusive").status_code == 302   # vise la rectification (la dernière)
    valider_derniere()
    with app.app_context():
        assert len(M.etat(db.session.get(Dossier, did)).charges) == 1              # l'hypothèque, elle, est toujours là

    assert saisir(dom, did, "annulation", notes="Hypothèque inscrite par erreur").status_code == 302   # vise maintenant l'hypothèque
    valider_derniere()
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert M.etat(d).charges == [] and M.verifier_chaine(d) == (True, None)
        assert [m.seq for m in M.valides(d)] == [1, 2, 3, 4]                       # l'historique n'est jamais réécrit
    assert saisir(dom, did, "annulation", notes="Plus rien").status_code == 400     # rien à annuler


def test_un_transfert_ne_s_annule_pas(app, login):
    """Une vente crée un nouveau dossier : on la corrige par une nouvelle opération, pas par une annulation."""
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    nouveau = vivant(app, did)
    dom = login("dom")
    assert saisir(dom, nouveau, "annulation", notes="Erreur de saisie").status_code == 400   # rien à annuler dans le nouveau dossier
    assert saisir(dom, did, "annulation", notes="Erreur de saisie").status_code == 403       # l'ancien est clôturé


def test_une_annulation_ne_cible_que_la_derniere_mutation_active(app, login):
    """Même un formulaire falsifié ne peut pas viser une mutation plus ancienne."""
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000000")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    premiere = derniere_mutation_id(app)
    saisir(dom, did, "rectification", notes="Orthographe")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    rectification = derniere_mutation_id(app)
    saisir(dom, did, "annulation", notes="motif", lie_a_id=str(premiere))              # lie_a_id est ignoré
    with app.app_context():
        annulation = Mutation.query.filter_by(type=C.MUT_ANNULATION).one()
        assert annulation.lie_a_id == rectification != premiere


# ----------------------------------------------------------------- rectification
def test_rectification_exige_une_description(app, login):
    did = dossier_certifie(login)
    dom = login("dom")
    assert saisir(dom, did, "rectification").status_code == 400
    assert saisir(dom, did, "rectification", notes="Orthographe du nom corrigée").status_code == 302


# ----------------------------------------------------------------- intégrité
def test_alteration_de_l_historique_detectee_et_validation_bloquee(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000000")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    saisir(dom, did, "rectification", notes="Orthographe")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    with app.app_context():
        assert M.verifier_chaine(db.session.get(Dossier, did)) == (True, None)

    with app.app_context():       # fraude en base : le montant de l'hypothèque est modifié
        premiere = Mutation.query.filter_by(seq=1).one()
        premiere.montant = 1
        db.session.commit()
        assert M.verifier_chaine(db.session.get(Dossier, did)) == (False, 1)

    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "ALTÉRÉ" in page and "validations sont bloquées" in page

    saisir(dom, did, "rectification", notes="Autre correction")
    en_attente = derniere_mutation_id(app)
    dire.post(f"/mutations/{en_attente}/valider")
    with app.app_context():
        assert db.session.get(Mutation, en_attente).statut == C.MS_ATTENTE       # refusée


def test_changer_un_cedant_ou_une_part_en_base_est_detecte(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    with app.app_context():
        partie = Mutation.query.one().parties[1]
        partie.part_num, partie.part_den = 1, 1       # le bénéficiaire « reçoit » toute la parcelle
        db.session.commit()
        assert M.verifier_chaine(db.session.get(Dossier, did))[0] is False


def test_la_chaine_depend_de_la_cle_secrete(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("M", "1"))
    with app.app_context():
        d = db.session.get(Dossier, did)
        assert M.verifier_chaine(d)[0] is True
        app.config["QR_SECRET"] = "autre-cle"
        assert M.verifier_chaine(d)[0] is False       # sans la clé, impossible de forger une chaîne valide


# ----------------------------------------------------------------- liens avec le reste du système
def test_documents_imprimes_refletent_le_proprietaire_actuel(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    r = dire.get(f"/dossiers/{did}/imprimer/certificat")
    assert r.status_code == 200
    from io import BytesIO
    from pypdf import PdfReader

    def texte(rep):
        return "\n".join(p.extract_text() for p in PdfReader(BytesIO(rep.data)).pages)

    assert "Said Ali" in texte(r)
    from app.models import DocumentEmis
    with app.app_context():
        ancien_code = DocumentEmis.query.one().code
    anonyme = app.test_client()
    assert "Document authentique" in anonyme.get(f"/verifier/{ancien_code}").data.decode()

    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa Nouveau", "1"))
    # l'ancien certificat n'est plus à jour : il indique que la propriété a été transférée, sans dire à qui
    page = anonyme.get(f"/verifier/{ancien_code}").data.decode().replace("&#39;", "'")
    assert "Propriété transférée" in page and "Said Ali" in page and "Moussa Nouveau" not in page
    assert "Document authentique" not in page
    # l'ancien dossier n'imprime plus ; le nouveau imprime au nom du nouveau propriétaire, et son certificat est authentique
    assert dire.get(f"/dossiers/{did}/imprimer/certificat").status_code == 403
    nouveau = vivant(app, did)
    assert "Moussa Nouveau" in texte(dire.get(f"/dossiers/{nouveau}/imprimer/certificat"))
    with app.app_context():
        nouveau_code = DocumentEmis.query.order_by(DocumentEmis.id.desc()).first().code
    assert "Document authentique" in anonyme.get(f"/verifier/{nouveau_code}").data.decode()


def test_justificatifs_joints_a_la_mutation(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    data = {"type": "vente", "date_acte": ACTE, "reference_acte": "Acte 1", f"cede_{said}": "tout", **benef(("M", "1")),
            "type_document": ["Acte (vente, donation, succession…)", "Autre"],
            "libelle_document": ["", "Procuration"],
            "fichiers": [(io.BytesIO(PDF), "acte.pdf"), (io.BytesIO(PDF), "procuration.pdf")]}
    r = login("dom").post(f"/dossiers/{did}/mutations/nouvelle", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        m = Mutation.query.one()
        assert sorted(d.intitule for d in m.documents) == ["Acte (vente, donation, succession…)", "Procuration"]
        assert all(d.mutation_id == m.id and d.dossier_id == did for d in Document.query.all())


def test_justificatif_invalide_annule_toute_la_saisie(app, login):
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    data = {"type": "vente", "date_acte": ACTE, "reference_acte": "Acte 1", f"cede_{said}": "tout", **benef(("M", "1")),
            "type_document": ["Autre"], "libelle_document": ["x"], "fichiers": [(io.BytesIO(b"pas un pdf"), "faux.pdf")]}
    r = login("dom").post(f"/dossiers/{did}/mutations/nouvelle", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    with app.app_context():
        assert Mutation.query.count() == 0 and Proprietaire.query.count() == 1 and Document.query.count() == 0


def test_recherche_par_nom_du_nouveau_proprietaire(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    vendre(app, login, did, proprio_id(app, did), "tout", ("Zoubeiri", "1"))
    assert "SIGF-" in dire.get("/dossiers?q=Zoubeiri").data.decode()          # nouveau propriétaire
    assert "SIGF-" in dire.get("/dossiers?q=Said").data.decode()               # ancien propriétaire (initial)
    liste = dire.get("/dossiers").data.decode()
    assert "Zoubeiri" in liste                                                  # la liste montre le propriétaire actuel


def test_audit_et_visibilite(app, login):
    did = dossier_certifie(login)
    mid = vendre(app, login, did, proprio_id(app, did), "tout", ("M", "1"))
    with app.app_context():
        actions = [a.action for a in AuditLog.query.filter(AuditLog.action.like("mutation_%")).order_by(AuditLog.id)]
        assert actions == ["mutation_saisie", "mutation_validee"]
    # le topographe public voit l'historique mais sans pouvoir agir ; le topo privé ne voit rien de tout cela
    page = login("pub").get(f"/dossiers/{did}").data.decode()
    assert "Traçabilité du terrain" in page and "Nouvelle mutation" not in page and "Valider" not in page.split("Traçabilité")[1].split("Exemplaires")[0]
    priv = login("priv1")
    did2 = creer_dossier(priv)
    assert "Traçabilité du terrain" not in priv.get(f"/dossiers/{did2}").data.decode()
    assert mid


def test_ancien_dossier_a_aussi_des_mutations(app, login):
    dom, dire = login("dom"), login("dir")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": (io.BytesIO(PDF), "t.pdf")})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    vieux = proprio_id(app, did, "Vieux")
    assert saisir(dom, did, "vente", **{f"cede_{vieux}": "tout"}, **benef(("Jeune", "1"))).status_code == 302
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert proprietaires(app, did) == {"Jeune": 1}
    with app.app_context():
        assert db.session.get(Dossier, did).statut == C.CLOTURE and db.session.get(Dossier, did).origine == "ancien"
        assert db.session.get(Dossier, vivant(app, did)).origine == "nouveau"       # le successeur est un dossier numérique


def test_formulaire_de_mutation_s_affiche(app, login):
    did = dossier_certifie(login)
    r = login("dom").get(f"/dossiers/{did}/mutations/nouvelle")
    page = r.data.decode()
    assert r.status_code == 200
    for attendu in ("Vente", "Héritage (succession)", "Hypothèque (inscription)", "Mainlevée d&#39;hypothèque",
                    "Annulation de la dernière mutation", "Said Ali", "ajouter-beneficiaire"):
        assert attendu in page, attendu
    assert 'id="ajouter-doc"' not in page and "Justificatifs" not in page and 'type="file"' not in page      # plus de justificatifs : l'acte est généré par le système
    assert "générera lui-même l'acte" in page
    assert 'enctype="multipart/form-data"' not in page
    assert f'name="cede_{proprio_id(app, did)}"' in page
    assert f'max="{date.today().isoformat()}"' in page


# ------------------------------------------------------------------ superficies en m² dans le formulaire
def test_formulaire_affiche_la_superficie_et_les_parts_en_m2(app, login):
    did = dossier_certifie(login)                                                              # superficie déclarée : 450,5 m²
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert "Superficie de la parcelle" in page and "450,5 m²" in page and "déclarée dans le dossier" in page
    assert "Superficie totale de la parcelle : 450,5 m²" in page                              # rappel dans la carte des cédants
    assert "(détient totalité = 450,5 m²)" in page                                              # part détenue, en m²
    assert 'data-detient="1/1"' in page and 'data-m2="450.5"' in page and "sortie-m2" in page and "bilan-parts" in page
    assert "parseFloat" in page and "lirePart" in page                                          # le calcul en direct est présent


def test_superficie_mesuree_sur_le_trace_et_part_detenue_partielle(app, login):
    from test_carte import LAT, LON, kml, points, televerser

    did = dossier_certifie(login)
    televerser(login("pub"), did, kml(points(LON, LAT, 50, 100)), "t.kml")                     # tracé de ~5 000 m²
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    nouveau = vivant(app, did)
    page = login("dom").get(f"/dossiers/{nouveau}/mutations/nouvelle").data.decode()
    assert "mesurée sur le tracé" in page
    assert "(détient 1/2 =" in page and 'data-detient="1/2"' in page                           # chaque propriétaire : sa part et son équivalent en m²


def test_superficie_inconnue_avertit(app, login):
    from app.models import Dossier

    did = dossier_certifie(login)
    with app.app_context():
        db.session.get(Dossier, did).parcelle.superficie_m2 = None
        db.session.commit()
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert "non renseignée" in page and "superficie-totale" not in page.replace("getElementById('superficie-totale')", "")


def test_les_superficies_en_m2_utilisent_le_meme_calcul_que_le_serveur(app, login):
    from fractions import Fraction

    from app import historique as H

    assert H.formater_m2(450.5) == "450,5 m²" and H.formater_m2(1200.0) == "1 200 m²" and H.formater_m2(171.4285) == "171,43 m²" and H.formater_m2(None) == "-"
    assert H.metres_carres(Fraction(1, 3), 1200) == 400.0 and H.metres_carres(Fraction(1, 7), 1200) == 171.43 and H.metres_carres(Fraction(1, 2), None) is None


# ------------------------------------------------------------------ bénéficiaires : superficie reçue en m²
def benef_m2(*lignes):
    """lignes = (nom, m²) -> champs de formulaire : la superficie reçue est saisie en m² (champ b_m2), sans fraction."""
    d = {k: [] for k in ("b_existant", "b_nom", "b_prenom", "b_identite", "b_telephone", "b_m2")}
    for nom, m2 in lignes:
        d["b_existant"].append("")
        d["b_nom"].append(nom)
        d["b_prenom"].append("")
        d["b_identite"].append("")
        d["b_telephone"].append("")
        d["b_m2"].append(m2)
    return d


def parts_beneficiaires(app, mid):
    from fractions import Fraction

    with app.app_context():
        m = db.session.get(Mutation, mid)
        return {p.proprietaire.nom_complet: Fraction(p.part_num, p.part_den) for p in m.parties if p.role == C.ROLE_BENEFICIAIRE}


def vendre_m2(app, login, did, part_cedee, *acheteurs):
    r = saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": part_cedee}, **benef_m2(*acheteurs), montant="1000000")
    return r


def test_formulaire_demande_la_superficie_recue_en_m2(app, login):
    did = dossier_certifie(login)                                                              # 450,5 m²
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert "Superficie reçue (m²) *" in page and 'name="b_m2"' in page and 'name="b_part"' not in page
    assert "superficie reçue par chacun, en m²" in page and "Attribuer le solde" in page and "data-reste" in page
    assert "Part *" not in page


def test_formulaire_garde_la_fraction_si_la_superficie_est_inconnue(app, login):
    from app.models import Dossier

    did = dossier_certifie(login)
    with app.app_context():
        db.session.get(Dossier, did).parcelle.superficie_m2 = None
        db.session.commit()
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert 'name="b_part"' in page and 'name="b_m2"' not in page and "Part *" in page


def test_vente_avec_superficie_recue_en_m2(app, login):
    did = dossier_certifie(login)                                                              # 450,5 m²
    r = vendre_m2(app, login, did, "1/2", ("Moussa", "225,25"))                                 # la moitié exactement
    assert r.status_code == 302, r.data.decode()
    assert parts_beneficiaires(app, derniere_mutation_id(app)) == {"Moussa": Fraction(1, 2)}
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert proprietaires(app, did) == {"Said Ali": Fraction(1, 2), "Moussa": Fraction(1, 2)}


def test_plusieurs_beneficiaires_en_m2(app, login):
    did = dossier_certifie(login)
    r = vendre_m2(app, login, did, "tout", ("Moussa", "225,25 m²"), ("Fatima", "112.625"), ("Ali", "112,625"))
    assert r.status_code == 302, r.data.decode()
    assert parts_beneficiaires(app, derniere_mutation_id(app)) == {"Moussa": Fraction(1, 2), "Fatima": Fraction(1, 4), "Ali": Fraction(1, 4)}


def test_l_ecart_d_arrondi_est_absorbe_par_le_dernier_beneficiaire(app, login):
    did = dossier_certifie(login)
    # 1/3 de 450,5 m² = 150,1666… m² : saisi au centième (150,17 m²), l'écart d'arrondi ne bloque pas la mutation
    saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": "1/3"}, **benef_m2(("Moussa", "150,17")), montant="1")
    assert parts_beneficiaires(app, derniere_mutation_id(app)) == {"Moussa": Fraction(1, 3)}      # la part enregistrée est exactement le tiers cédé


def test_total_des_m2_different_du_total_cede_refuse(app, login):
    did = dossier_certifie(login)
    r = vendre_m2(app, login, did, "1/2", ("Moussa", "200"))                                    # 25 m² d'écart
    assert r.status_code == 400 and "doit être égal au total des parts reçues" in r.data.decode()
    assert vendre_m2(app, login, did, "1/2", ("Moussa", "225,25"), ("Fatima", "100")).status_code == 400


@pytest.mark.parametrize("valeur, attendu", [
    ("abc", "est invalide"),
    ("-5", "invalide"),
    ("0", "supérieure à 0"),
    ("500", "dépasse celle de la parcelle"),                                                     # plus que 450,5 m²
    ("12,3,4", "invalide"),
])
def test_superficies_invalides_refusees(app, login, valeur, attendu):
    did = dossier_certifie(login)
    r = vendre_m2(app, login, did, "tout", ("Moussa", valeur))
    assert r.status_code == 400 and attendu in r.data.decode(), valeur
    with app.app_context():
        assert Mutation.query.count() == 0


def test_m2_sans_superficie_connue_refuse(app, login):
    from app.models import Dossier

    did = dossier_certifie(login)
    with app.app_context():
        db.session.get(Dossier, did).parcelle.superficie_m2 = None
        db.session.commit()
    r = vendre_m2(app, login, did, "tout", ("Moussa", "100"))
    assert r.status_code == 400 and "superficie de la parcelle est inconnue" in r.data.decode()


def test_les_parts_en_fraction_fonctionnent_toujours(app, login):
    did = dossier_certifie(login)
    r = saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": "1/2"}, **benef(("Moussa", "1/2")), montant="1")
    assert r.status_code == 302                                                                  # compatibilité : b_part reste accepté par le serveur


def test_tiers_en_m2_apres_une_premiere_vente(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    nouveau = vivant(app, did)
    with app.app_context():
        moussa = Proprietaire.query.filter_by(nom="Moussa").one().id
    r = saisir(login("dom"), nouveau, "vente", **{f"cede_{moussa}": "tout"}, **benef_m2(("Anli", "225,25")), montant="1")      # Moussa détient 225,25 m²
    assert r.status_code == 302, r.data.decode()
    assert parts_beneficiaires(app, derniere_mutation_id(app)) == {"Anli": Fraction(1, 2)}


# ------------------------------------------------------------------ le prix n'existe que pour une vente
def test_le_prix_n_est_propose_que_pour_une_vente(app, login):
    did = dossier_certifie(login)
    page = login("dom").get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert "Prix de vente (KMF)" in page and "si vente" not in page
    bloc = page.split("Prix de vente (KMF)")[0].rsplit('<div data-types="', 1)[1].split('"')[0]
    assert bloc == "vente"                                                                       # affiché seulement quand le type « Vente » est choisi
    assert "vente: ['vente']" in page.replace('"', "'")                                          # le script le rattache au type « vente »
    for autre in ("donation", "heritage", "expropriation"):
        assert autre not in bloc


@pytest.mark.parametrize("type_", ["donation", "heritage", "expropriation"])
def test_un_prix_envoye_pour_donation_heritage_expropriation_est_ignore(app, login, type_):
    did = dossier_certifie(login)
    r = saisir(login("dom"), did, type_, **{f"cede_{proprio_id(app, did)}": "tout"}, **benef(("Moussa", "1")), montant="5000000")
    assert r.status_code == 302, r.data.decode()
    with app.app_context():
        assert Mutation.query.order_by(Mutation.id.desc()).first().montant is None
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "5 000 000" not in page and "Montant :" not in page


def test_le_prix_d_une_vente_est_conserve(app, login):
    did = dossier_certifie(login)
    r = saisir(login("dom"), did, "vente", **{f"cede_{proprio_id(app, did)}": "tout"}, **benef(("Moussa", "1")), montant="5 000 000")
    assert r.status_code == 302
    with app.app_context():
        assert Mutation.query.order_by(Mutation.id.desc()).first().montant == 5000000
    assert "5 000 000 KMF" in login("dir").get(f"/dossiers/{did}").data.decode()
