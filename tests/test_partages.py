import io
import re
from datetime import date

import pytest

from app import constants as C
from app import mutations as M
from app.extensions import db
from app.models import AuditLog, DocumentEmis, EtapeLog, Filiation, Geometrie, Mutation, Proprietaire

from conftest import agir
from test_carte import LAT, LON, kml, points, televerser
from test_mutations import ACTE, benef, proprio_id, saisir, vendre, vivant

# ----------------------------------------------------------------- aides
from app.models import Dossier


def cree(login, nom="Said", prenom="Ali", identite="CNI-1", ile="1", commune="Moroni", superficie="", certifier=True):
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    data = {"proprio_nom": nom, "proprio_prenom": prenom, "proprio_identite": identite, "ile_id": ile, "commune": commune, "superficie": superficie}
    r = pub.post("/dossiers/nouveau", data=data)
    assert r.status_code == 302, r.data.decode()
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    if certifier:
        agir(pub, did, "terminer_plan")
        agir(pub, did, "saisir_rsp", valeur=f"R{did}")
        agir(dom, did, "saisir_rcp", valeur=f"C{did}")
        agir(dire, did, "valider")
    return did


def pid_dossier(app, did):
    """Fiche du propriétaire D'ORIGINE de ce dossier (plusieurs dossiers peuvent avoir un « Said »)."""
    with app.app_context():
        return db.session.get(Dossier, did).proprietaire_id


def numero(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).numero


def vide():
    return (io.BytesIO(b""), "")


def donnees_division(lots, date_acte=ACTE, reference="Plan de division 2020", **extra):
    """lots : liste de dicts {nom, superficie?, alloc?: {pid: part}, trace?: bytes}."""
    d = {"date_acte": date_acte, "reference_acte": reference, "e_libelle": [], "e_superficie": [], "e_fichier": []}
    pids = sorted({pid for lot in lots for pid in lot.get("alloc", {})})
    for pid in pids:
        d[f"e_alloc_{pid}"] = []
    for lot in lots:
        d["e_libelle"].append(lot["nom"])
        d["e_superficie"].append(str(lot.get("superficie", "")))
        d["e_fichier"].append((io.BytesIO(lot["trace"]), f"{lot['nom']}.kml") if lot.get("trace") else vide())
        for pid in pids:
            d[f"e_alloc_{pid}"].append(lot.get("alloc", {}).get(pid, ""))
    d.update(extra)
    return d


def diviser(client, did, lots, **extra):
    return client.post(f"/dossiers/{did}/division/nouvelle", data=donnees_division(lots, **extra), content_type="multipart/form-data")


def fusionner(client, did, numeros, trace=None, **extra):
    data = {"date_acte": ACTE, "reference_acte": "Acte de fusion 2020", "source_numero": list(numeros)}
    if trace:
        data["fichier_trace"] = (io.BytesIO(trace), "fusion.kml")
    data.update(extra)
    return client.post(f"/dossiers/{did}/fusion/nouvelle", data=data, content_type="multipart/form-data")


def derniere_id(app, type_):
    with app.app_context():
        return Mutation.query.filter_by(type=type_).order_by(Mutation.id.desc()).first().id


def valider(login, mid):
    login("dir").post(f"/mutations/{mid}/valider")


def statut(app, did):
    with app.app_context():
        return db.session.get(Dossier, did).statut


def enfants(app, did):
    with app.app_context():
        return [(f.libelle, f.resultat_id) for f in Filiation.query.filter_by(source_id=did, type="division").order_by(Filiation.id)]


def proprietaires(app, did):
    with app.app_context():
        return {p.nom_complet: f for p, f in M.etat(db.session.get(Dossier, did)).parts}


def chaine_ok(app, did):
    with app.app_context():
        return M.verifier_chaine(db.session.get(Dossier, did)) == (True, None)


def nb_dossiers(app):
    with app.app_context():
        return Dossier.query.count()


LOTS2 = [{"nom": "Lot 1"}, {"nom": "Lot 2"}]


# ================================================================= DIVISION
def test_division_heritage_par_defaut(app, login):
    did = cree(login, superficie="1000")
    dom, dire = login("dom"), login("dir")
    r = diviser(dom, did, [{"nom": "Lot 1", "superficie": "400"}, {"nom": "Lot 2", "superficie": "600"}])
    assert r.status_code == 302

    # tant que non validée : rien ne change
    assert statut(app, did) == C.CERTIFIE and nb_dossiers(app) == 1
    valider(login, derniere_id(app, C.MUT_DIVISION))

    assert statut(app, did) == C.CLOTURE and chaine_ok(app, did)
    filles = enfants(app, did)
    assert [lib for lib, _ in filles] == ["Lot 1", "Lot 2"] and nb_dossiers(app) == 3
    with app.app_context():
        for (lib, fid), decl in zip(filles, (400, 600)):
            f = db.session.get(Dossier, fid)
            assert (f.statut, f.filiere, f.origine, f.parcelle.superficie_m2) == (C.PLAN_CROQUIS, C.FILIERE_PUBLIC, C.ORIGINE_NOUVEAU, decl)
            assert f.numero.startswith("SIGF-") and f.parcelle.commune == "Moroni" and lib in f.parcelle.reference
            assert {p.nom_complet: x for p, x in M.etat(f).parts} == {"Said Ali": 1}      # hérite de la mère
            assert f.etapes[0].commentaire.startswith("Créé par division")
        m = Mutation.query.filter_by(type=C.MUT_DIVISION).one()
        assert (m.statut, m.seq) == (C.MS_VALIDEE, 1)
        actions = {a.action for a in AuditLog.query}
        assert {"division_saisie", "mutation_validee", "dossier_cree_par_division"} <= actions

    # la mère n'est plus modifiable ni imprimable
    assert saisir(dom, did, "rectification", notes="x").status_code == 403
    assert dire.get(f"/dossiers/{did}/imprimer/plan_croquis").status_code == 403
    page = dire.get(f"/dossiers/{did}").data.decode()
    assert "Parcelle clôturée" in page and "Divisée" in page and numero(app, filles[0][1]) in page
    fille = dire.get(f"/dossiers/{filles[0][1]}").data.decode()
    assert "issu d'une division" in fille.replace("&#39;", "'") and numero(app, did) in fille
    # les filles suivent le circuit normal : elles apparaissent dans la file de travail du topographe public
    from app import workflow as W
    from app.models import User

    with app.app_context():
        file_pub = {x.id for x in W.dossiers_a_traiter(User.query.filter_by(username="pub").one()).all()}
        assert {fid for _, fid in filles} <= file_pub and did not in file_pub


def test_les_filles_entrent_dans_le_circuit_complet(app, login):
    did = cree(login)
    diviser(login("dom"), did, LOTS2)
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, fid), _ = enfants(app, did)
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    agir(pub, fid, "terminer_plan")
    agir(pub, fid, "saisir_rsp", valeur="R-fille")
    agir(dom, fid, "saisir_rcp", valeur="C-fille")
    agir(dire, fid, "valider")
    assert statut(app, fid) == C.CERTIFIE
    assert dire.get(f"/dossiers/{fid}/imprimer/certificat").status_code == 200
    assert "Said Ali" in dire.get(f"/dossiers/{fid}").data.decode()


def test_partage_entre_coproprietaires(app, login):
    did = cree(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    did = vivant(app, did)                                  # la vente a créé un nouveau dossier : on divise celui-là
    moussa = proprio_id(app, did, "Moussa")
    r = diviser(login("dom"), did, [{"nom": "Lot A", "alloc": {said: "1"}}, {"nom": "Lot B", "alloc": {moussa: "1"}}])
    assert r.status_code == 302
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, a), (_, b) = enfants(app, did)
    assert proprietaires(app, a) == {"Said Ali": 1} and proprietaires(app, b) == {"Moussa": 1}


def test_parts_partagees_au_sein_d_une_fille(app, login):
    did = cree(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    did = vivant(app, did)
    moussa = proprio_id(app, did, "Moussa")
    diviser(login("dom"), did, [{"nom": "Lot A", "alloc": {said: "1/2", moussa: "1/2"}}, {"nom": "Lot B", "alloc": {said: "1/2", moussa: "1/2"}}])
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, a), _ = enfants(app, did)
    assert proprietaires(app, a) == {"Said Ali": M.Fraction(1, 2), "Moussa": M.Fraction(1, 2)}


@pytest.mark.parametrize("lots_fn, attendu", [
    (lambda s, m: [{"nom": "A", "alloc": {s: "1/2"}}, {"nom": "B", "alloc": {m: "1"}}], "doivent faire 1"),                # somme ≠ 1
    (lambda s, m: [{"nom": "A", "alloc": {s: "1"}}, {"nom": "B", "alloc": {}}], "aucun propriétaire"),                      # fille sans propriétaire
    (lambda s, m: [{"nom": "A", "alloc": {s: "1"}}, {"nom": "B", "alloc": {s: "1"}}], "oubliés : Moussa"),                  # copropriétaire oublié
    (lambda s, m: [{"nom": "A", "alloc": {s: "abc"}}, {"nom": "B", "alloc": {m: "1"}}], "invalide"),
    (lambda s, m: [{"nom": "A", "alloc": {s: "2"}}, {"nom": "B", "alloc": {m: "1"}}], "ne pas dépasser"),
])
def test_repartition_invalide_refusee(app, login, lots_fn, attendu):
    did = cree(login)
    said = proprio_id(app, did)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))
    did = vivant(app, did)
    moussa = proprio_id(app, did, "Moussa")
    avant = nb_dossiers(app)
    r = diviser(login("dom"), did, lots_fn(said, moussa))
    assert r.status_code == 400 and attendu in r.data.decode()
    with app.app_context():
        assert Mutation.query.filter_by(type=C.MUT_DIVISION).count() == 0
    assert nb_dossiers(app) == avant


@pytest.mark.parametrize("lots, extra, attendu", [
    ([{"nom": "Seul"}], {}, "entre 2 et 20"),
    ([{"nom": f"L{i}"} for i in range(21)], {}, "entre 2 et 20"),
    ([{"nom": "A"}, {"nom": ""}], {}, "doit avoir un nom"),
    ([{"nom": "A"}, {"nom": "a"}], {}, "même nom"),
    ([{"nom": "A", "superficie": "-5"}, {"nom": "B"}], {}, "nombre positif"),
    ([{"nom": "A"}, {"nom": "B"}], {"reference": ""}, "référence de l"),
    ([{"nom": "A"}, {"nom": "B"}], {"date_acte": "2999-01-01"}, "futur"),
])
def test_division_donnees_invalides(app, login, lots, extra, attendu):
    did = cree(login)
    r = diviser(login("dom"), did, lots, **extra)
    assert r.status_code == 400 and attendu in r.data.decode().replace("&#39;", "'")
    with app.app_context():
        assert Mutation.query.count() == 0


def test_division_droits(app, login):
    did = cree(login)
    for qui in ("pub", "dir", "admin"):
        assert diviser(login(qui), did, LOTS2).status_code == 403, qui
        assert login(qui).get(f"/dossiers/{did}/division/nouvelle").status_code == 403
    assert diviser(login("priv1"), did, LOTS2).status_code == 404
    assert app.test_client().get(f"/dossiers/{did}/division/nouvelle").status_code == 302
    pub = login("pub")
    en_cours = pub.post("/dossiers/nouveau", data={"proprio_nom": "X", "ile_id": "1", "commune": "Y"})
    did2 = int(en_cours.headers["Location"].rstrip("/").split("/")[-1])
    assert diviser(login("dom"), did2, LOTS2).status_code == 403            # pas encore certifié


def test_division_bloquee_par_hypotheque_ou_mutation_en_attente(app, login):
    did = cree(login)
    dom = login("dom")
    saisir(dom, did, "hypotheque", creancier_nom="Banque", montant="1000000")
    assert "mutation en attente" in diviser(dom, did, LOTS2).data.decode()          # hypothèque pas encore validée
    valider(login, derniere_id(app, C.MUT_HYPOTHEQUE))
    assert "hypothèque active" in diviser(dom, did, LOTS2).data.decode()
    with app.app_context():
        assert Mutation.query.filter_by(type=C.MUT_DIVISION).count() == 0


def test_rejet_de_la_division_ne_cree_rien(app, login):
    did = cree(login)
    diviser(login("dom"), did, LOTS2)
    mid = derniere_id(app, C.MUT_DIVISION)
    login("dir").post(f"/mutations/{mid}/rejeter", data={"motif": "Plan incomplet"})
    assert statut(app, did) == C.CERTIFIE and nb_dossiers(app) == 1 and enfants(app, did) == []
    login("dir").post(f"/mutations/{mid}/valider")
    assert nb_dossiers(app) == 1                                                  # rejetée : ne se valide plus


def test_mutation_saisie_apres_la_division_est_rejetee_automatiquement(app, login):
    did = cree(login)
    dom = login("dom")
    diviser(dom, did, LOTS2)
    division = derniere_id(app, C.MUT_DIVISION)
    saisir(dom, did, "rectification", notes="Correction de dernière minute")     # saisie possible pendant l'attente
    rect = derniere_id(app, C.MUT_RECTIFICATION)
    valider(login, division)
    with app.app_context():
        m = db.session.get(Mutation, rect)
        assert m.statut == C.MS_REJETEE and "clôturé" in m.motif_rejet


def test_une_vente_validee_entre_temps_rejette_la_division_en_attente(app, login):
    did = cree(login)
    said = proprio_id(app, did)
    dom = login("dom")
    diviser(dom, did, [{"nom": "A", "alloc": {said: "1"}}, {"nom": "B", "alloc": {said: "1"}}])
    division = derniere_id(app, C.MUT_DIVISION)
    vendre(app, login, did, said, "1/2", ("Moussa", "1/2"))          # validée : crée un nouveau dossier, clôture celui-ci
    valider(login, division)                                           # sans effet : la division a été rejetée d'office
    assert statut(app, did) == C.CLOTURE and enfants(app, did) == []
    with app.app_context():
        m = db.session.get(Mutation, division)
        assert m.statut == C.MS_REJETEE and "clôturé" in m.motif_rejet
    assert nb_dossiers(app) == 2                                       # l'original et le successeur de la vente, aucune fille


def test_division_valide_aussi_pour_un_ancien_dossier(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Vieux", "ile_id": "1", "commune": "Domoni", "type_document": ["Titre foncier"], "libelle_document": [""],
        "fichiers": (io.BytesIO(b"%PDF-1.4 x"), "t.pdf")})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    assert diviser(dom, did, LOTS2).status_code == 302
    valider(login, derniere_id(app, C.MUT_DIVISION))
    assert statut(app, did) == C.CLOTURE
    with app.app_context():
        assert all(db.session.get(Dossier, f).statut == C.PLAN_CROQUIS for _, f in enfants(app, did))


# ----------------------------------------------------------------- division et tracés
def moitie(cote):
    """Deux moitiés d'une parcelle de 100 m x 100 m."""
    return points(LON + (0 if cote == 0 else 0.000459), LAT, 50, 100)


def mere_avec_trace(login, app):
    did = cree(login)
    televerser(login("pub"), did, kml(points(LON, LAT, 100, 100)))
    return did


def test_division_avec_traces_valides(app, login, tmp_path):
    did = mere_avec_trace(login, app)
    lots = [{"nom": "Ouest", "trace": kml(points(LON, LAT, 49.5, 100))}, {"nom": "Est", "trace": kml(points(LON + 0.000455, LAT, 49.5, 100))}]
    r = diviser(login("dom"), did, lots)
    assert r.status_code == 302, r.data.decode()
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, a), (_, b) = enfants(app, did)
    with app.app_context():
        for fid in (a, b):
            g = db.session.get(Dossier, fid).geometrie_courante
            assert g is not None and g.version == 1 and g.superficie_m2 == pytest.approx(4950, rel=0.03)
            import os
            assert os.path.exists(os.path.join(app.config["UPLOAD_DIR"], g.cle))                   # fichier d'origine conservé
    # la mère clôturée ne compte plus comme voisine : aucune alerte de chevauchement sur les filles
    page = login("dir").get(f"/dossiers/{a}").data.decode()
    assert "Aucun chevauchement" in page and "Chevauchement détecté" not in page
    carte = login("dir").get("/carte/donnees.json").get_json()
    assert carte["total"] == 2 and {f["properties"]["numero"] for f in carte["features"]} == {numero(app, a), numero(app, b)}


def test_fille_hors_de_la_mere_ou_filles_qui_se_chevauchent_refusees(app, login):
    did = mere_avec_trace(login, app)
    dom = login("dom")
    dehors = [{"nom": "A", "trace": kml(points(LON, LAT, 40, 100))}, {"nom": "B", "trace": kml(points(LON + 0.002, LAT, 40, 100))}]
    r = diviser(dom, did, dehors)
    assert r.status_code == 400 and "dépasse de la parcelle mère" in r.data.decode()
    chevauche = [{"nom": "A", "trace": kml(points(LON, LAT, 70, 100))}, {"nom": "B", "trace": kml(points(LON + 0.00035, LAT, 60, 100))}]
    r = diviser(dom, did, chevauche)
    assert r.status_code == 400 and "se chevauchent" in r.data.decode()
    with app.app_context():
        assert Mutation.query.filter_by(type=C.MUT_DIVISION).count() == 0


def test_une_fille_sans_trace_est_acceptee(app, login):
    did = mere_avec_trace(login, app)
    r = diviser(login("dom"), did, [{"nom": "Ouest", "trace": kml(points(LON, LAT, 49.5, 100))}, {"nom": "Reste"}])
    assert r.status_code == 302


def test_fichier_de_trace_invalide_annule_toute_la_division(app, login):
    did = cree(login)
    import os
    lots = [{"nom": "A", "trace": kml(points(LON, LAT, 40, 40))}, {"nom": "B", "trace": b"pas du xml"}]
    r = diviser(login("dom"), did, lots)
    assert r.status_code == 400 and "« B »" in r.data.decode()
    with app.app_context():
        assert Mutation.query.count() == 0
    assert not os.path.exists(os.path.join(app.config["UPLOAD_DIR"], "dossiers"))                # rien de stocké


# ----------------------------------------------------------------- intégrité, QR
def test_chaine_et_details_signes(app, login):
    did = cree(login)
    diviser(login("dom"), did, LOTS2)
    valider(login, derniere_id(app, C.MUT_DIVISION))
    assert chaine_ok(app, did)
    with app.app_context():                                                      # fraude : on change le contenu de la division
        m = Mutation.query.filter_by(type=C.MUT_DIVISION).one()
        m.details = m.details.replace("Lot 2", "Lot X")
        db.session.commit()
    assert not chaine_ok(app, did)
    assert "ALTÉRÉ" in login("dir").get(f"/dossiers/{did}").data.decode()


def test_le_certificat_de_la_mere_indique_que_la_parcelle_n_existe_plus(app, login):
    did = cree(login)
    dire = login("dir")
    dire.get(f"/dossiers/{did}/imprimer/certificat")
    with app.app_context():
        code = DocumentEmis.query.one().code
    anonyme = app.test_client()
    assert "Document authentique" in anonyme.get(f"/verifier/{code}").data.decode()
    diviser(login("dom"), did, LOTS2)
    valider(login, derniere_id(app, C.MUT_DIVISION))
    page = anonyme.get(f"/verifier/{code}").data.decode().replace("&#39;", "'")
    assert "Parcelle divisée ou fusionnée" in page and "n'existe plus" in page and "Document authentique" not in page
    assert "Parcelle clôturée" in dire.get(f"/dossiers/{did}").data.decode()


def test_formulaires_s_affichent_et_javascript_valide(app, login, tmp_path):
    import shutil, subprocess
    did = cree(login)
    dom = login("dom")
    pages = [dom.get(f"/dossiers/{did}/division/nouvelle"), dom.get(f"/dossiers/{did}/fusion/nouvelle"), dom.get(f"/dossiers/{did}")]
    assert [p.status_code for p in pages] == [200, 200, 200]
    detail = pages[2].data.decode()
    assert "Diviser" in detail and "Fusionner" in detail
    assert "cfg-division" in pages[0].data.decode() and "source_numero" in pages[1].data.decode()
    if shutil.which("node"):
        n = 0
        for p in pages[:2]:
            for js in re.findall(r"<script>(.*?)</script>", p.data.decode(), re.S):
                f = tmp_path / f"s{n}.js"
                f.write_text(js)
                res = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
                assert res.returncode == 0, res.stderr
                n += 1
        assert n >= 2
    # un formulaire ne doit plus proposer division/fusion dans la mutation générique
    generique = dom.get(f"/dossiers/{did}/mutations/nouvelle").data.decode()
    assert 'value="division"' not in generique and 'value="fusion"' not in generique
    assert "propre formulaire" in saisir(dom, did, "division").data.decode()


# ================================================================= FUSION
def deux_voisines(app, login, **kw):
    a = cree(login, **kw)
    b = cree(login, **kw)
    pub = login("pub")
    televerser(pub, a, kml(points(LON, LAT, 50, 100)))
    televerser(pub, b, kml(points(LON + 0.000449, LAT, 50, 100)))                    # accolée à l'est
    return a, b


def test_fusion_complete(app, login):
    a, b = deux_voisines(app, login, superficie="5000")
    dom = login("dom")
    r = fusionner(dom, a, [numero(app, b)])
    assert r.status_code == 302, r.data.decode()
    assert statut(app, a) == statut(app, b) == C.CERTIFIE and nb_dossiers(app) == 2
    valider(login, derniere_id(app, C.MUT_FUSION))

    assert statut(app, a) == statut(app, b) == C.CLOTURE
    assert chaine_ok(app, a) and chaine_ok(app, b)
    with app.app_context():
        # chaque parcelle d'origine porte la fusion dans sa propre chaîne signée
        mut_a = Mutation.query.filter_by(dossier_id=a, type=C.MUT_FUSION).one()
        mut_b = Mutation.query.filter_by(dossier_id=b, type=C.MUT_FUSION).one()
        assert (mut_a.statut, mut_a.seq, mut_b.statut, mut_b.seq) == (C.MS_VALIDEE, 1, C.MS_VALIDEE, 1)
        assert mut_a.hash != mut_b.hash and mut_b.decision_par.role == C.DIRECTEUR

        nouveau = db.session.get(Filiation, 1).resultat
        assert nouveau.id not in (a, b) and Dossier.query.count() == 3
        assert (nouveau.statut, nouveau.filiere, nouveau.parcelle.superficie_m2) == (C.PLAN_CROQUIS, C.FILIERE_PUBLIC, 10000)
        assert {p.nom_complet: x for p, x in M.etat(nouveau).parts} == {"Said Ali": 1}
        assert {(f.source_id, f.type) for f in Filiation.query.filter_by(resultat_id=nouveau.id)} == {(a, "fusion"), (b, "fusion")}
        g = nouveau.geometrie_courante                                              # union calculée automatiquement
        assert g is not None and g.superficie_m2 == pytest.approx(10000, rel=0.03)
        assert db.session.get(Dossier, a).etapes[-1].commentaire.startswith("fusionné dans")
        assert AuditLog.query.filter_by(action="dossier_cree_par_fusion").count() == 1
    dire = login("dir")
    page = dire.get(f"/dossiers/{a}").data.decode()
    assert "Parcelle clôturée" in page and "Fusionnée" in page
    with app.app_context():
        nid = Filiation.query.first().resultat_id
    assert "issu d'une fusion" in dire.get(f"/dossiers/{nid}").data.decode().replace("&#39;", "'")
    assert dire.get(f"/dossiers/{b}/imprimer/plan_croquis").status_code == 403


def test_fusion_avec_trace_fourni_et_ancien_dossier(app, login):
    a, b = deux_voisines(app, login)
    dom = login("dom")
    assert "écart" in fusionner(dom, a, [numero(app, b)], trace=kml(points(LON, LAT, 150, 100))).data.decode()     # 15 000 m² ≠ 10 000
    r = fusionner(dom, a, [numero(app, b)], trace=kml(points(LON, LAT, 99.5, 100)))
    assert r.status_code == 302
    valider(login, derniere_id(app, C.MUT_FUSION))
    with app.app_context():
        nid = Filiation.query.first().resultat_id
        g = db.session.get(Dossier, nid).geometrie_courante
        assert g.nom_original == "fusion.kml" and g.superficie_m2 == pytest.approx(9950, rel=0.03)


@pytest.mark.parametrize("preparer, attendu", [
    ("aucune", "au moins deux parcelles"),
    ("inconnue", "introuvable"),
    ("soi_meme", "deux fois"),
    ("autre_ile", "même île"),
    ("autres_proprietaires", "mêmes propriétaires"),
    ("sans_identite", "pièce d'identité"),
    ("autres_parts", "mêmes propriétaires"),
    ("hypotheque", "hypothèque active"),
    ("mutation_en_attente", "en attente"),
    ("non_contigues", "ne se touchent pas"),
])
def test_fusion_refusee(app, login, preparer, attendu):
    a = cree(login)
    dom = login("dom")
    if preparer == "aucune":
        numeros = []
    elif preparer == "inconnue":
        numeros = ["SIGF-1999-99999"]
    elif preparer == "soi_meme":
        numeros = [numero(app, a)]
    else:
        kw = {"autre_ile": {"ile": "2"}, "autres_proprietaires": {"nom": "Autre", "identite": "CNI-2"}, "sans_identite": {"identite": "CNI-9"}}.get(preparer, {})
        b = cree(login, **kw)
        numeros = [numero(app, b)]
        if preparer == "sans_identite":  # un dossier ancien, créé avant que la pièce soit obligatoire
            with app.app_context():
                db.session.get(Dossier, b).proprietaire.identite = None
                db.session.commit()
        if preparer == "autres_parts":
            vendre(app, login, b, pid_dossier(app, b), "1/2", ("Moussa", "1/2"))
        if preparer == "hypotheque":
            saisir(dom, b, "hypotheque", creancier_nom="Banque", montant="100")
            valider(login, derniere_id(app, C.MUT_HYPOTHEQUE))
        if preparer == "mutation_en_attente":
            saisir(dom, b, "rectification", notes="en cours")
        if preparer == "non_contigues":
            televerser(login("pub"), a, kml(points(LON, LAT, 50, 100)))
            televerser(login("pub"), b, kml(points(LON + 0.01, LAT, 50, 100)))
    avant = nb_dossiers(app)
    r = fusionner(dom, a, numeros)
    assert r.status_code == 400 and attendu in r.data.decode().replace("&#39;", "'"), r.data.decode()[:300]
    with app.app_context():
        assert Mutation.query.filter_by(type=C.MUT_FUSION).count() == 0
    assert nb_dossiers(app) == avant


def test_fusion_avec_un_dossier_deja_cloture_refusee(app, login):
    a = cree(login)
    b = cree(login)
    c = cree(login)
    dom = login("dom")
    diviser(dom, c, LOTS2)
    valider(login, derniere_id(app, C.MUT_DIVISION))
    r = fusionner(dom, a, [numero(app, c)])
    assert r.status_code == 400 and "ne peut pas être fusionné" in r.data.decode()


def test_fusion_valide_a_nouveau_les_conditions(app, login):
    a, b = deux_voisines(app, login)
    dom = login("dom")
    fusionner(dom, a, [numero(app, b)])
    fusion = derniere_id(app, C.MUT_FUSION)
    # entre la saisie et la validation, la parcelle B est vendue pour moitié : les propriétaires diffèrent
    vendre(app, login, b, pid_dossier(app, b), "1/2", ("Moussa", "1/2"))
    valider(login, fusion)
    # B a été clôturé par la vente (son successeur porte les nouveaux propriétaires) : la fusion ne peut plus aboutir
    assert statut(app, a) == C.CERTIFIE and statut(app, b) == C.CLOTURE and nb_dossiers(app) == 3
    with app.app_context():
        assert db.session.get(Mutation, fusion).statut == C.MS_ATTENTE


def test_historique_altere_sur_une_source_bloque_la_fusion(app, login):
    a, b = deux_voisines(app, login)
    dom = login("dom")
    saisir(dom, b, "rectification", notes="Orthographe du nom")                  # donne une chaîne à B sans changer ses propriétaires
    valider(login, derniere_id(app, C.MUT_RECTIFICATION))
    assert chaine_ok(app, b)
    assert fusionner(dom, a, [numero(app, b)]).status_code == 302                 # saisie possible...
    fusion = derniere_id(app, C.MUT_FUSION)
    with app.app_context():                                                      # ... puis fraude sur l'historique de B
        Mutation.query.filter_by(dossier_id=b, type=C.MUT_RECTIFICATION).one().notes = "Texte falsifié"
        db.session.commit()
    valider(login, fusion)
    assert statut(app, a) == statut(app, b) == C.CERTIFIE and nb_dossiers(app) == 2
    with app.app_context():
        assert db.session.get(Mutation, fusion).statut == C.MS_ATTENTE


def test_fusion_droits(app, login):
    a, b = deux_voisines(app, login)
    for qui in ("pub", "dir", "admin"):
        assert fusionner(login(qui), a, [numero(app, b)]).status_code == 403, qui
    assert fusionner(login("priv1"), a, [numero(app, b)]).status_code == 404
    assert login("priv1").get(f"/dossiers/{a}/fusion/nouvelle").status_code == 404


def test_fusion_un_ancien_dossier_avec_un_nouveau(app, login):
    dom = login("dom")
    r = dom.post("/dossiers/ancien/nouveau", content_type="multipart/form-data", data={
        "proprio_nom": "Said", "proprio_prenom": "Ali", "proprio_identite": "CNI-1", "ile_id": "1", "commune": "Moroni",
        "type_document": ["Titre foncier"], "libelle_document": [""], "fichiers": (io.BytesIO(b"%PDF-1.4 x"), "t.pdf")})
    ancien = int(r.headers["Location"].rstrip("/").split("/")[-1])
    nouveau = cree(login)
    assert fusionner(dom, nouveau, [numero(app, ancien)]).status_code == 302
    valider(login, derniere_id(app, C.MUT_FUSION))
    assert statut(app, ancien) == statut(app, nouveau) == C.CLOTURE and chaine_ok(app, ancien) and chaine_ok(app, nouveau)


def test_validation_tout_ou_rien_si_le_stockage_tombe_en_panne(app, login, monkeypatch):
    from app import partages
    from app.storage import StorageError

    a, b = deux_voisines(app, login)
    fusionner(login("dom"), a, [numero(app, b)])             # sans tracé fourni : l'union sera écrite au stockage
    fusion = derniere_id(app, C.MUT_FUSION)

    class Panne:
        def put(self, *args, **kwargs):
            raise StorageError("Stockage indisponible.")

        def delete(self, *args, **kwargs):
            pass

    monkeypatch.setattr(partages, "get_storage", lambda: Panne())
    r = login("dir").post(f"/mutations/{fusion}/valider", follow_redirects=True)
    assert "Stockage indisponible" in r.data.decode()

    # rien n'a bougé : ni dossier créé, ni clôture, ni mutation scellée, ni mutation « système » sur la source B
    assert statut(app, a) == statut(app, b) == C.CERTIFIE and nb_dossiers(app) == 2
    with app.app_context():
        m = db.session.get(Mutation, fusion)
        assert (m.statut, m.seq, m.hash) == (C.MS_ATTENTE, None, None)
        assert Mutation.query.filter_by(dossier_id=b, type=C.MUT_FUSION).count() == 0
        assert Filiation.query.count() == 0
    # une fois le stockage rétabli, la même validation aboutit
    monkeypatch.undo()
    valider(login, fusion)
    assert statut(app, a) == statut(app, b) == C.CLOTURE and nb_dossiers(app) == 3
