import pytest

from app import constants as C
from app import demo
from app import mutations as M
from app.extensions import db
from app.models import Dossier, DocumentEmis, Geometrie, Mutation


def test_les_donnees_de_demonstration_se_creent_et_restent_coherentes(app, login):
    with app.app_context():
        assert demo.deja_present() == 0
        resume = demo.semer()
        assert demo.deja_present() >= 15

        statuts = {}
        for d in Dossier.query.all():
            statuts[d.statut] = statuts.get(d.statut, 0) + 1
            assert M.verifier_chaine(d) == (True, None), d.numero          # toutes les chaînes sont intactes
        # tous les états du circuit sont représentés, nouveaux comme anciens
        assert {C.PLAN_CROQUIS, C.ATTENTE_AVIS, C.SAISIE_RSP, C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR, C.CERTIFIE, C.ARCHIVE, C.CLOTURE} <= set(statuts)
        # clôturés : l'héritage, la division, 2 fusions, 3 dossiers de la chaîne (2 ventes + 1 division), 1 ancien dossier vendu
        assert statuts[C.CLOTURE] == 8 and statuts[C.ARCHIVE] == 4         # (5 anciens dossiers, dont 1 vendu donc clôturé)
        assert Dossier.query.filter_by(filiere=C.FILIERE_PRIVE).count() == 2
        assert {m.type for m in Mutation.query} >= {C.MUT_VENTE, C.MUT_HERITAGE, C.MUT_HYPOTHEQUE, C.MUT_DIVISION, C.MUT_FUSION}
        assert Mutation.query.filter_by(statut=C.MS_ATTENTE).count() == 1   # la vente à faire valider
        assert Geometrie.query.filter_by(remplace_at=None).count() >= 12
        assert DocumentEmis.query.count() == 2                               # exemplaires imprimés avec QR

        # l'arbre : la chaîne « 2 ventes puis division » forme une lignée de 3 générations
        from app.arbre import construire
        from app.models import Filiation
        divisions = Filiation.query.filter_by(type="division").all()
        lot = next(f.resultat for f in divisions if f.libelle == "Lot Nord")
        arbre = construire(lot)
        assert arbre["nb"] == 5 and len(arbre["racines"]) == 1                # origine -> vente -> vente -> 2 lots
        niveau = arbre["racines"][0]
        profondeur = 1
        while niveau["enfants"]:
            niveau = niveau["enfants"][0]
            profondeur += 1
        assert profondeur == 4


def test_la_demo_se_parcourt_sans_erreur_et_la_verification_marche(app, login):
    with app.app_context():
        resume = demo.semer()
        ids = [d.id for d, *_ in resume]
        codes = [c for ligne in resume if len(ligne) > 2 for c in ligne[2].values()]
    dire, anonyme = login("dir"), app.test_client()
    for did in ids:
        assert dire.get(f"/dossiers/{did}").status_code == 200, did
    assert dire.get("/dossiers").status_code == 200 and dire.get("/carte/donnees.json").status_code == 200
    assert dire.get("/").status_code == 200
    for code in codes:
        assert "Document authentique" in anonyme.get(f"/verifier/{code}").data.decode()
    # le chevauchement voulu est bien signalé
    pages = [dire.get(f"/dossiers/{i}").data.decode() for i in ids]
    assert any("Chevauchement détecté" in p for p in pages)
    # les topographes privés ne voient que leurs dossiers
    assert dire.get("/dossiers?statut=attente_avis").data.decode().count("SIGF-") >= 1
    assert login("priv1").get("/dossiers").data.decode().count("SIGF-2026-") == 1


def test_la_demo_ne_se_duplique_pas_sans_le_demander(app, login):
    from click.testing import CliRunner

    runner = app.test_cli_runner()
    r1 = runner.invoke(args=["seed-demo"])
    assert r1.exit_code == 0 and "dossier(s) de démonstration créés" in r1.output and "/verifier/" in r1.output
    r2 = runner.invoke(args=["seed-demo"])
    assert r2.exit_code != 0 and "existent déjà" in r2.output


def test_la_demo_est_interdite_en_production(app, login, monkeypatch):
    monkeypatch.setenv("RENDER", "1")
    r = app.test_cli_runner().invoke(args=["seed-demo"])
    assert r.exit_code != 0 and "production" in r.output
    with app.app_context():
        assert Dossier.query.count() == 0
