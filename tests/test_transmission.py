import io
import os
import re
from fractions import Fraction

import pytest

from app import arbre as Ab
from app import constants as C
from app import mutations as M
from app import stats as St
from app import transmission
from app.extensions import db
from app.models import AuditLog, Dossier, Filiation, Geometrie, Mutation, maintenant
from app.storage import LocalStorage, StorageError

from test_carte import LAT, LON, kml, points, televerser
from test_mutations import ACTE, benef, derniere_mutation_id, dossier_certifie, proprio_id, saisir, vendre, vivant
from test_partages import chaine_ok, cree, derniere_id, deux_voisines, diviser, enfants, fusionner, nb_dossiers, numero, statut, valider


def vendre_tout(app, login, did, acheteur="Moussa"):
    return vendre(app, login, did, proprio_id(app, did), "tout", (acheteur, "1"))


def hyp(app, login, did):
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000000")
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    return derniere_mutation_id(app)


# ================================================================= création du nouveau dossier
def test_la_vente_cree_un_nouveau_dossier_certifie(app, login):
    did = dossier_certifie(login)
    mid = vendre_tout(app, login, did)
    nouveau = vivant(app, did)
    assert nouveau != did
    with app.app_context():
        ancien, s = db.session.get(Dossier, did), db.session.get(Dossier, nouveau)
        assert (s.statut, s.origine, s.filiere) == (C.CERTIFIE, C.ORIGINE_NOUVEAU, C.FILIERE_PUBLIC)
        assert s.numero.startswith("SIGF-") and s.numero != ancien.numero
        assert (s.rsp, s.rcp) == (ancien.rsp, ancien.rcp) == ("RTC-1", "RTS-1")      # la parcelle est la même : mêmes numéros
        assert s.valide_par.role == C.DIRECTEUR and s.valide_at is not None
        assert s.parcelle.id != ancien.parcelle.id and (s.parcelle.commune, s.parcelle.ile_id) == (ancien.parcelle.commune, ancien.parcelle.ile_id)
        assert s.proprietaire_label == "Moussa" and ancien.proprietaire_label == "Said Ali"   # l'ancien garde les propriétaires d'avant
        assert ancien.statut == C.CLOTURE
        f = Filiation.query.one()
        assert (f.source_id, f.resultat_id, f.type, f.mutation_id) == (did, nouveau, "vente", mid)
        log = s.etapes[0]
        assert (log.type, log.vers_statut) == ("derive", C.CERTIFIE) and "Créé par vente" in log.commentaire
        assert ancien.etapes[-1].vers_statut == C.CLOTURE and "propriété transférée" in ancien.etapes[-1].commentaire
        assert AuditLog.query.filter_by(action="dossier_cree_par_transfert").count() == 1
        assert M.verifier_chaine(ancien) == (True, None) and M.verifier_chaine(s) == (True, None)
        assert [m.seq for m in M.valides(ancien)] == [1] and M.valides(s) == []


def test_vente_partielle_le_successeur_contient_tous_les_proprietaires(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    nouveau = vivant(app, did)
    with app.app_context():
        assert {p.nom_complet: f for p, f in db.session.get(Dossier, nouveau).proprietaires_actuels} == {"Said Ali": Fraction(1, 2), "Moussa": Fraction(1, 2)}
        assert {p.nom_complet: f for p, f in db.session.get(Dossier, did).proprietaires_actuels} == {"Said Ali": 1}    # historique figé
    dom = login("dom")
    assert saisir(dom, did, "rectification", notes="x").status_code == 403           # l'ancien est clôturé
    assert saisir(dom, nouveau, "rectification", notes="x").status_code == 302       # le nouveau est le dossier vivant


@pytest.mark.parametrize("type_, champs", [
    ("donation", lambda s: {f"cede_{s}": "1/2", **benef(("Donataire", "1/2"))}),
    ("heritage", lambda s: {f"cede_{s}": "tout", **benef(("Heritier A", "1/2"), ("Heritier B", "1/2"))}),
    ("expropriation", lambda s: {f"cede_{s}": "tout", **benef(("Etat", "1"))}),
    ("vente", lambda s: {f"cede_{s}": "tout", **benef(("Acheteur", "1"))}),
])
def test_chaque_transfert_cree_un_dossier(app, login, type_, champs):
    did = dossier_certifie(login)
    saisir(login("dom"), did, type_, **champs(proprio_id(app, did)))
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert vivant(app, did) != did and nb_dossiers(app) == 2
    with app.app_context():
        assert Filiation.query.one().type == type_


def test_les_autres_mutations_restent_dans_le_dossier(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    hyp(app, login, did)
    saisir(dom, did, "rectification", notes="Orthographe")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    saisir(dom, did, "annulation", notes="Erreur")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert vivant(app, did) == did and nb_dossiers(app) == 1 and statut(app, did) == C.CERTIFIE
    with app.app_context():
        assert Filiation.query.count() == 0


def test_mutation_en_attente_sur_l_ancien_dossier_est_rejetee(app, login):
    did = dossier_certifie(login)
    dom = login("dom")
    saisir(dom, did, "rectification", notes="En attente")
    attente = derniere_mutation_id(app)
    vendre_tout(app, login, did)
    with app.app_context():
        m = db.session.get(Mutation, attente)
        assert m.statut == C.MS_REJETEE and "clôturé" in m.motif_rejet


# ================================================================= hypothèques et tracé suivent la parcelle
def test_les_hypotheques_actives_suivent_la_parcelle(app, login):
    did = dossier_certifie(login)
    origine = hyp(app, login, did)
    vendre_tout(app, login, did)
    nouveau = vivant(app, did)
    with app.app_context():
        s = db.session.get(Dossier, nouveau)
        charges = M.etat(s).charges
        assert len(charges) == 1 and charges[0].montant == 1000000 and "reportée" in charges[0].notes
        assert [p.proprietaire.nom_complet for p in charges[0].parties] == ["Banque"]
        assert M.verifier_chaine(s) == (True, None) and M.valides(s)[0].seq == 1
        report = charges[0].id
    page = login("dir").get(f"/dossiers/{nouveau}").data.decode()
    assert "Charges actives" in page and "Banque" in page

    # la charge reportée n'est ni « la dernière mutation » (rien à annuler), ni une nouvelle opération dans les statistiques
    assert saisir(login("dom"), nouveau, "annulation", notes="x").status_code == 400
    with app.app_context():
        par_type = {l["type"]: l["valeur"] for l in St.mutations_par_type(None, 1, maintenant())}
        assert par_type == {"hypotheque": 1, "vente": 1}
    assert len(re.findall(r">(?:Ouvrir|Examiner)<", login("dir").get("/mutations").data.decode())) == 2   # ni doublon dans la liste

    # la mainlevée se fait sur le dossier à jour
    assert saisir(login("dom"), nouveau, "mainlevee", lie_a_id=str(report), reference_acte="Mainlevée n°7").status_code == 302
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    with app.app_context():
        assert M.etat(db.session.get(Dossier, nouveau)).charges == []


def test_le_trace_suit_la_parcelle(app, login):
    pub = login("pub")
    did = dossier_certifie(login)
    televerser(pub, did, kml(points(LON, LAT, 100, 80)))
    vendre_tout(app, login, did)
    nouveau = vivant(app, did)
    with app.app_context():
        g_old = Geometrie.query.filter_by(dossier_id=did).one()
        g_new = Geometrie.query.filter_by(dossier_id=nouveau).one()
        assert (g_new.version, g_new.empreinte, g_new.superficie_m2) == (1, g_old.empreinte, g_old.superficie_m2)
        assert g_new.cle != g_old.cle and "Reprise du tracé" in g_new.motif_remplacement
        racine = app.config["UPLOAD_DIR"]
        assert os.path.exists(os.path.join(racine, g_old.cle)) and os.path.exists(os.path.join(racine, g_new.cle))
        with open(os.path.join(racine, g_old.cle), "rb") as a, open(os.path.join(racine, g_new.cle), "rb") as b:
            assert a.read() == b.read()                                              # vraie copie du fichier
    dire = login("dir")
    page = dire.get(f"/dossiers/{nouveau}").data.decode()
    assert "Aucun chevauchement" in page and "Chevauchement détecté" not in page      # l'ancien dossier (clôturé) ne compte plus comme voisin
    assert dire.get(f"/dossiers/{nouveau}/trace/google-earth.kml").status_code == 200
    assert dire.get(f"/dossiers/{nouveau}/trace/1/telecharger").status_code == 200
    assert dire.get("/carte/donnees.json").get_json()["total"] == 1                   # un seul tracé sur la carte : celui du dossier à jour


def test_panne_du_stockage_pendant_la_copie_tout_ou_rien(app, login, monkeypatch):
    pub, dom = login("pub"), login("dom")
    did = dossier_certifie(login)
    televerser(pub, did, kml(points(LON, LAT, 100, 80)))
    saisir(dom, did, "vente", **{f"cede_{proprio_id(app, did)}": "tout"}, **benef(("Moussa", "1")))
    mid = derniere_mutation_id(app)

    class Panne:
        def copier(self, *a, **k):
            raise StorageError("Stockage indisponible.")

        def put(self, *a, **k):
            raise StorageError("Stockage indisponible.")

        def delete(self, *a, **k):
            pass

    monkeypatch.setattr(transmission, "get_storage", lambda: Panne())
    r = login("dir").post(f"/mutations/{mid}/valider", follow_redirects=True)
    assert "Stockage indisponible" in r.data.decode()
    with app.app_context():
        m = db.session.get(Mutation, mid)
        assert (m.statut, m.seq, m.hash) == (C.MS_ATTENTE, None, None)
        assert Filiation.query.count() == 0 and Dossier.query.count() == 1 and db.session.get(Dossier, did).statut == C.CERTIFIE
    monkeypatch.undo()
    login("dir").post(f"/mutations/{mid}/valider")                                    # le stockage est rétabli : la même validation aboutit
    assert vivant(app, did) != did and nb_dossiers(app) == 2


# ================================================================= accès direct au nouveau dossier
def test_acces_direct_au_nouveau_dossier(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    saisir(dom, did, "vente", **{f"cede_{proprio_id(app, did)}": "tout"}, **benef(("Moussa", "1")), montant="5000000")
    r = dire.post(f"/mutations/{derniere_mutation_id(app)}/valider", follow_redirects=True)
    nouveau = vivant(app, did)
    page = r.data.decode()
    assert f"Un nouveau dossier a été créé : {numero(app, nouveau)}" in page            # le message de validation le dit
    # sur l'ancien dossier : bandeau et bouton vers le dossier à jour
    assert "Propriété transférée (vente)" in page and "Ouvrir le dossier à jour" in page
    assert f'href="/dossiers/{nouveau}"' in page
    # dans l'historique de la mutation : un lien « Nouveau dossier »
    assert "Nouveau dossier :" in page.replace("</div>", "") or "Nouveau dossier" in page
    assert f'<a href="/dossiers/{nouveau}">{numero(app, nouveau)}</a>' in page
    # sur le nouveau : d'où il vient
    neuf = dire.get(f"/dossiers/{nouveau}").data.decode().replace("&#39;", "'")
    assert "issu d'une vente" in neuf and numero(app, did) in neuf and f'href="/dossiers/{did}"' in neuf
    # le topographe privé ne voit aucun de ces dossiers
    assert login("priv1").get(f"/dossiers/{nouveau}").status_code == 404 and app.test_client().get(f"/dossiers/{nouveau}").status_code == 302


# ================================================================= l'arbre de traçabilité
def test_arbre_de_trois_generations_avec_une_division(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    d1 = vivant(app, did)
    vendre(app, login, did, proprio_id(app, did, "Moussa"), "1/4", ("Nadia", "1/4"))
    d2 = vivant(app, did)
    diviser(login("dom"), d2, [{"nom": "Lot A"}, {"nom": "Lot B"}])
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, lot_a), (_, lot_b) = enfants(app, d2)

    page = login("dir").get(f"/dossiers/{d1}").data.decode()
    arbre = page[page.index('<ul class="arbre">'):page.index("Historique de ce dossier")]
    positions = [arbre.index(numero(app, x)) for x in (did, d1, d2, lot_a, lot_b)]
    assert positions == sorted(positions)                                             # racine d'abord, puis chaque génération, dans l'ordre
    assert arbre.count("<ul>") == 3                                                   # les 3 dossiers qui ont des enfants : original, d1, d2
    assert arbre.count("Vous êtes ici") == 1 and arbre.count('aria-current="true"') == 1
    assert arbre.index("Vous êtes ici") > arbre.index(numero(app, d1)) and arbre.index("Vous êtes ici") < arbre.index(numero(app, d2))
    assert "Vente · 01/05/2020 · 5 000 000 KMF" in arbre and "Division · Lot A · 01/05/2020" in arbre and "Division · Lot B" in arbre
    assert "Clôturé" in arbre and "Plan croquis en cours" in arbre                       # états visibles : anciens clôturés, filles en circuit
    assert "déjà affiché plus haut" not in arbre
    for x in (did, d1, d2, lot_a, lot_b):
        assert f'href="/dossiers/{x}#tracabilite"' in arbre                              # chaque nœud mène à son dossier


def test_l_arbre_est_le_meme_depuis_n_importe_quel_dossier(app, login):
    did = dossier_certifie(login)
    vendre_tout(app, login, did)
    d1 = vivant(app, did)
    vendre(app, login, did, proprio_id(app, d1, "Moussa"), "1/2", ("Nadia", "1/2"))
    d2 = vivant(app, did)

    def forme(n):
        return (n["d"].id, [forme(e) for e in n["enfants"]])

    with app.app_context():
        formes = [[forme(r) for r in Ab.construire(db.session.get(Dossier, x))["racines"]] for x in (did, d1, d2)]
        assert formes[0] == formes[1] == formes[2] == [(did, [(d1, [(d2, [])])])]
        assert Ab.construire(db.session.get(Dossier, d2))["nb"] == 3


def test_dossier_sans_histoire(app, login):
    did = dossier_certifie(login)
    page = login("dir").get(f"/dossiers/{did}").data.decode()
    assert "Arbre de la parcelle" in page and "n&#39;a encore connu aucun transfert" in page.replace("'", "&#39;")
    assert page.count("Vous êtes ici") == 1
    with app.app_context():
        assert not Ab.a_une_histoire(Ab.construire(db.session.get(Dossier, did)))


def test_fusion_dans_l_arbre(app, login):
    a, b = deux_voisines(app, login)
    fusionner(login("dom"), a, [numero(app, b)])
    valider(login, derniere_mutation_id(app))
    with app.app_context():
        nid = Filiation.query.filter_by(type="fusion").first().resultat_id
    page = login("dir").get(f"/dossiers/{nid}").data.decode()
    arbre = page[page.index('<ul class="arbre">'):page.index("Historique de ce dossier")]
    assert numero(app, a) in arbre and numero(app, b) in arbre and numero(app, nid) in arbre
    assert arbre.count(numero(app, nid)) == 2 and arbre.count("déjà affiché plus haut (fusion)") == 1     # sous le 1er parent, puis rappelé sous le 2e
    assert arbre.count("Fusion") == 2


def test_arbre_tronque_quand_il_y_a_trop_de_dossiers(app, login, monkeypatch):
    did = dossier_certifie(login)
    vendre_tout(app, login, did, "Un")
    vendre(app, login, did, proprio_id(app, did, "Un"), "tout", ("Deux", "1"))
    vendre(app, login, did, proprio_id(app, did, "Deux"), "tout", ("Trois", "1"))
    monkeypatch.setattr(Ab, "MAX_DOSSIERS", 2)
    page = login("dir").get(f"/dossiers/{vivant(app, did)}").data.decode()
    assert "Arbre partiel" in page


# ================================================================= le reste du système
def test_recherche_et_liste_des_dossiers(app, login):
    did = dossier_certifie(login)
    vendre_tout(app, login, did, "Zoubeiri")
    nouveau = vivant(app, did)
    dire = login("dir")
    defaut = dire.get("/dossiers").data.decode()
    assert numero(app, nouveau) in defaut and numero(app, did) not in defaut          # le dossier clôturé ne double pas la liste
    assert "Zoubeiri" in defaut and "Said Ali" not in defaut
    assert numero(app, did) in dire.get("/dossiers?statut=cloture").data.decode()      # mais on le retrouve avec le statut « Clôturé »...
    assert numero(app, did) in dire.get(f"/dossiers?q={numero(app, did)}").data.decode()   # ... ou en le cherchant
    assert numero(app, nouveau) in dire.get("/dossiers?q=Zoubeiri").data.decode()       # le nouveau propriétaire trouve le nouveau dossier
    assert numero(app, did) in dire.get("/dossiers?q=Said").data.decode()               # et l'ancien propriétaire, l'ancien dossier


def test_statistiques_un_transfert_n_est_ni_un_dossier_cree_ni_un_certificat(app, login):
    did = dossier_certifie(login)
    vendre_tout(app, login, did)
    with app.app_context():
        ref = maintenant()
        a = St.activite_mensuelle(None, 1, ref)["series"]
        assert [s["valeurs"] for s in a] == [[1], [1], [1]]      # 1 dossier créé, 1 certificat (circuit), 1 mutation validée
        assert [l["valeur"] for l in St.par_ile() if l["ile"] == 1] == [1]                   # une seule parcelle existante, pas deux
        assert St.tuiles(None, 6, ref)[0]["brut"] == 0                                       # rien « en cours » : les deux sont certifiés/clôturés


def test_le_tableau_de_bord_et_les_pages_s_affichent(app, login):
    did = dossier_certifie(login)
    vendre_tout(app, login, did)
    for qui in ("dir", "dom", "pub", "admin"):
        for url in ("/", "/dossiers", "/dossiers/anciens", "/mutations", "/carte"):
            assert login(qui).get(url).status_code == 200, (qui, url)


# ================================================================= stockage : copier
def test_stockage_local_copier(tmp_path):
    st = LocalStorage(str(tmp_path))
    st.put("a/source.bin", b"contenu", "application/octet-stream")
    st.copier("a/source.bin", "b/dest.bin")
    assert (tmp_path / "b" / "dest.bin").read_bytes() == b"contenu" and (tmp_path / "a" / "source.bin").exists()
    with pytest.raises(StorageError):
        st.copier("a/absent.bin", "b/x.bin")
    with pytest.raises(StorageError):
        st.copier("a/source.bin", "../evasion.bin")                                  # jamais en dehors de la racine
    assert not (tmp_path.parent / "evasion.bin").exists()


def test_stockage_s3_copier():
    import boto3
    from moto import mock_aws

    from app.storage import S3Storage

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sigf")
        st = S3Storage("sigf", None, "a", "b", "us-east-1")
        st.put("src.kmz", "tracé".encode(), "application/vnd.google-earth.kmz")
        st.copier("src.kmz", "dst.kmz")
        obj = boto3.client("s3", region_name="us-east-1").get_object(Bucket="sigf", Key="dst.kmz")
        assert obj["Body"].read() == "tracé".encode()
        with pytest.raises(StorageError):
            st.copier("absent.kmz", "x.kmz")


def test_la_barre_d_etapes_du_circuit_n_apparait_pas_sur_un_dossier_cloture(app, login):
    did = dossier_certifie(login)
    avant = login("dir").get(f"/dossiers/{did}").data.decode()
    assert 'class="stepper"' in avant                                                   # dossier certifié : barre d'étapes
    vendre_tout(app, login, did)
    assert 'class="stepper"' not in login("dir").get(f"/dossiers/{did}").data.decode()   # clôturé : elle n'a plus de sens
    assert 'class="stepper"' in login("dir").get(f"/dossiers/{vivant(app, did)}").data.decode()
