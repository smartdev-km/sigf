import json
import re
import shutil
import subprocess
from datetime import datetime

import pytest

from app import constants as C
from app import stats as St
from app.extensions import db
from app.models import Dossier, EtapeLog, Mutation

from conftest import agir
from test_mutations import benef, derniere_mutation_id, saisir
from test_partages import cree, numero, proprio_id

REF = datetime(2026, 11, 17, 12, 0)


def d(mois, jour):
    return datetime(2026, mois, jour)


def fixer_dates(app, did, dates):
    """Impose la date de création et celle de chaque transition du circuit (dans l'ordre)."""
    with app.app_context():
        dossier = db.session.get(Dossier, did)
        dossier.created_at = dates[0]
        logs = EtapeLog.query.filter_by(dossier_id=did).order_by(EtapeLog.id).all()
        assert len(logs) == len(dates), (len(logs), len(dates))
        for log, quand in zip(logs, dates):
            log.created_at = quand
        db.session.commit()


@pytest.fixture()
def jeu(app, login):
    """6 dossiers aux dates connues, donc aux durées calculables à la main."""
    pub, dom, dire = login("pub"), login("dom"), login("dir")

    def certifie(ile, dates, nom):
        did = cree(login, nom=nom, identite=f"ID-{nom}", ile=ile, certifier=False)
        agir(pub, did, "terminer_plan")
        agir(pub, did, "saisir_rsp", valeur="R")
        agir(dom, did, "saisir_rcp", valeur="C")
        agir(dire, did, "valider")
        fixer_dates(app, did, dates)
        return did

    ids = {
        # plan 3 j, RTC 5 j, RTS 2 j, validation 1 j  -> 11 jours, certifié en septembre
        "A": certifie("1", [d(9, 1), d(9, 4), d(9, 9), d(9, 11), d(9, 12)], "A"),
        # plan 1 j, RTC 7 j, RTS 2 j, validation 8 j  -> 18 jours, certifié en octobre
        "B": certifie("1", [d(10, 2), d(10, 3), d(10, 10), d(10, 12), d(10, 20)], "B"),
        # plan 3 j, RTC 1 j, RTS 2 j, validation 2 j  -> 8 jours, certifié en novembre (île 2)
        "C": certifie("2", [d(11, 2), d(11, 5), d(11, 6), d(11, 8), d(11, 10)], "C"),
    }
    # D : bloqué à la saisie du RTC depuis le 2 août ; E : créé il y a 2 jours
    ids["D"] = cree(login, nom="D", identite="ID-D", certifier=False)
    agir(pub, ids["D"], "terminer_plan")
    fixer_dates(app, ids["D"], [d(8, 1), d(8, 2)])
    ids["E"] = cree(login, nom="E", identite="ID-E", certifier=False)
    fixer_dates(app, ids["E"], [d(11, 15)])

    # une vente validée en octobre sur B
    said = proprio_id(app, ids["B"], "B")
    saisir(dom, ids["B"], "vente", **{f"cede_{said}": "tout"}, **benef(("Acheteur", "1")))
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    with app.app_context():
        db.session.get(Mutation, derniere_mutation_id(app)).decision_at = d(10, 25)
        db.session.commit()
    return ids


# ================================================================= calendrier
def test_fenetre_de_mois_et_etiquettes():
    assert St.fenetre_mois(datetime(2026, 1, 20), 3) == [(2025, 11), (2025, 12), (2026, 1)]
    assert St.fenetre_mois(REF, 6) == [(2026, 6), (2026, 7), (2026, 8), (2026, 9), (2026, 10), (2026, 11)]
    assert St.etiquettes(St.fenetre_mois(datetime(2026, 1, 20), 3)) == ["nov. 25", "déc.", "janv. 26"]   # l'année quand elle change
    assert St.borne_debut(REF, 6) == datetime(2026, 6, 1) and St.borne_fin(REF) == datetime(2026, 12, 1)
    assert St.borne_fin(datetime(2026, 12, 31)) == datetime(2027, 1, 1)


def test_format_compact_et_variations():
    assert [St.compact(n) for n in (0, 999, 1284, 9999, 10000, 12900, 100000)] == ["0", "999", "1 284", "9 999", "10 k", "12,9 k", "100 k"]
    assert St._delta(5, 5, "le mois précédent") == {"texte": "stable vs le mois précédent", "sens": "stable", "bon": None}
    hausse = St._delta(6, 4, "le mois précédent")
    assert hausse["texte"] == "+50 % vs le mois précédent" and hausse["bon"] is True
    assert St._delta(2, 4, "x")["bon"] is False                                    # moins, alors que plus est souhaitable
    assert St._delta(2, 4, "x", haut_est_bon=False)["bon"] is True                 # un délai qui baisse, c'est bien
    assert St._delta(3, 0, "x")["texte"] == "+3 vs x"                              # base nulle : variation absolue


# ================================================================= indicateurs
def test_pipeline_par_etape(app, login, jeu):
    with app.app_context():
        total = {l["statut"]: l["valeur"] for l in St.pipeline(None)}
        assert total == {C.PLAN_CROQUIS: 1, C.ATTENTE_AVIS: 0, C.SAISIE_RSP: 1, C.ENREGISTREMENT_RCP: 0, C.VALIDATION_DIRECTEUR: 0}
        assert all(l["valeur"] == 0 for l in St.pipeline(2))                      # l'île 2 n'a rien en cours


def test_activite_mensuelle(app, jeu):
    with app.app_context():
        a = St.activite_mensuelle(None, 6, REF)
        assert a["mois"] == ["juin 26", "juil.", "août", "sept.", "oct.", "nov."]
        crees, certifies, mutations = (s["valeurs"] for s in a["series"])
        assert crees == [0, 0, 1, 1, 1, 2]          # D en août, A en septembre, B en octobre, C et E en novembre
        assert certifies == [0, 0, 0, 1, 1, 1]
        assert mutations == [0, 0, 0, 0, 1, 0]
        assert [s["nom"] for s in a["series"]] == ["Dossiers créés", "Certificats émis", "Mutations validées"]

        ile2 = St.activite_mensuelle(2, 6, REF)["series"]
        assert [s["valeurs"] for s in ile2] == [[0, 0, 0, 0, 0, 1], [0, 0, 0, 0, 0, 1], [0] * 6]     # filtre par île
        courte = St.activite_mensuelle(None, 3, REF)
        assert len(courte["mois"]) == 3 and courte["series"][0]["valeurs"] == [1, 1, 2]                 # fenêtre de 3 mois


def test_delais_par_etape_calcules_a_la_main(app, jeu):
    with app.app_context():
        debut, fin = St.borne_debut(REF, 6), St.borne_fin(REF)
        etapes = {l["statut"]: l for l in St.delais_par_etape(None, debut, fin)}
        assert etapes[C.PLAN_CROQUIS]["valeur"] == 2.3 and etapes[C.SAISIE_RSP]["valeur"] == 4.3      # (3+1+3)/3 et (5+7+1)/3
        assert etapes[C.ENREGISTREMENT_RCP]["valeur"] == 2.0 and etapes[C.VALIDATION_DIRECTEUR]["valeur"] == 3.7   # (2+2+2)/3 et (1+8+2)/3
        assert all(e["n"] == 3 for e in etapes.values())
        assert C.ATTENTE_AVIS not in etapes                                                            # filière privée seulement
        assert sorted(St.delais(None, debut, fin)) == [8.0, 11.0, 18.0]                                # création -> certificat
        assert St.delais(None, d(9, 1), d(10, 1)) == [11.0]                                            # seul A, certifié en septembre
        assert St.delais(2, debut, fin) == [8.0]


def test_tuiles(app, jeu):
    with app.app_context():
        t = St.tuiles(None, 6, REF)
        assert [x["libelle"] for x in t] == ["Dossiers en cours", "Certificats émis ce mois", "Mutations validées ce mois", "Délai moyen de traitement (6 mois)"]
        assert t[0]["valeur"] == "2" and t[0]["delta"] is None
        assert t[1]["valeur"] == "1" and t[1]["delta"]["sens"] == "stable"                  # 1 en novembre comme en octobre
        assert t[2]["valeur"] == "0" and t[2]["delta"]["sens"] == "baisse" and t[2]["delta"]["bon"] is False   # 0 contre 1 en octobre
        assert len(t[1]["trace"]) == 12 and t[1]["trace"][-3:] == [1, 1, 1] and t[2]["trace"][-2:] == [1, 0]
        assert t[3]["valeur"] == "12,3 j" and t[3]["delta"] is None                         # pas de période précédente à comparer

        un_mois = St.tuiles(None, 1, REF)[3]                                                # novembre seul (8 j) contre octobre (18 j)
        assert un_mois["valeur"] == "8,0 j" and un_mois["delta"]["bon"] is True and un_mois["delta"]["sens"] == "baisse"
        assert St.tuiles(None, 3, datetime(2020, 1, 1))[3]["valeur"] == "—"                 # aucun certificat : pas de moyenne inventée


def test_dossiers_en_retard(app, jeu):
    with app.app_context():
        r = St.en_retard(None, REF)
        assert r["total"] == 1 and r["lignes"][0]["jours"] == 107                            # D : depuis le 2 août
        assert r["lignes"][0]["etape"] == "Saisie du RTC" and r["lignes"][0]["responsable"] == "Topographie publique"
        assert St.en_retard(2, REF)["total"] == 0                                             # filtre par île
        assert St.en_retard(None, REF, seuil=200)["total"] == 0                               # seuil plus large
        assert St.en_retard(None, REF, seuil=1)["total"] == 2                                 # E (2 jours) devient « en retard »
        assert [l["jours"] for l in St.en_retard(None, REF, seuil=1)["lignes"]] == [107, 2]    # le plus ancien d'abord


def test_une_correction_de_numero_ne_remet_pas_le_compteur_a_zero(app, login, jeu):
    """Corriger un RTC n'est pas une avancée : un dossier bloqué reste bloqué."""
    with app.app_context():
        db.session.add(EtapeLog(dossier_id=jeu["D"], de_statut=C.SAISIE_RSP, vers_statut=C.SAISIE_RSP, type="correction", user_id=1, created_at=d(11, 16)))
        db.session.commit()
        assert St.en_retard(None, REF)["lignes"][0]["jours"] == 107


def test_une_fusion_ne_compte_qu_une_mutation(app, login):
    from test_carte import LAT, LON, kml, points, televerser
    from test_partages import fusionner
    from test_mutations import derniere_mutation_id as dernier

    a, b = cree(login), cree(login)
    televerser(login("pub"), a, kml(points(LON, LAT, 50, 100)))
    televerser(login("pub"), b, kml(points(LON + 0.000449, LAT, 50, 100)))
    fusionner(login("dom"), a, [numero(app, b)])
    login("dir").post(f"/mutations/{dernier(app)}/valider")
    with app.app_context():
        assert Mutation.query.filter_by(type=C.MUT_FUSION, statut=C.MS_VALIDEE).count() == 2          # une ligne par parcelle source
        from app.models import maintenant
        mois = St.activite_mensuelle(None, 1, maintenant())["series"][2]["valeurs"]
        assert mois == [1]                                                                              # ... mais une seule opération
        assert [l["valeur"] for l in St.mutations_par_type(None, 1, maintenant())] == [1]


def test_mutations_par_type_et_par_ile(app, jeu):
    with app.app_context():
        assert St.mutations_par_type(None, 6, REF) == [{"nom": "Vente", "valeur": 1, "type": "vente"}]
        assert St.mutations_par_type(None, 1, REF) == []                                            # aucune en novembre
        iles = {l["ile"]: l["valeur"] for l in St.par_ile()}
        assert iles == {1: 4, 2: 1, 3: 0}                                                            # A, B, D, E à Ngazidja ; C à Anjouan


def test_activite_recente(app, jeu):
    with app.app_context():
        evts = St.activite_recente(None, limite=5)
        assert len(evts) == 5 and [e["quand"] for e in evts] == sorted((e["quand"] for e in evts), reverse=True)
        assert all(e["qui"] and e["dossier"].numero for e in evts)
        textes = {e["texte"] for e in St.activite_recente(None, limite=60)}
        assert "a validé : vente" in textes and "a validé et émis le certificat" in textes
        assert {"a ouvert le dossier", "a terminé le plan croquis", "a saisi le numéro RTC", "a saisi le numéro RTS"} <= textes
        assert not any("a ancien" in x or "a plan" in x or "a saisie" in x for x in textes)      # plus de phrases mal construites


# ================================================================= page
def donnees_page(page):
    brut = re.search(r'<script type="application/json" id="donnees-dashboard">(.*?)</script>', page, re.S).group(1)
    return json.loads(brut)


def test_tableau_de_bord_complet(app, login, jeu):
    page = login("dir").get("/").data.decode()
    for attendu in ("Dossiers en cours", "Certificats émis ce mois", "Mutations validées ce mois", "Délai moyen de traitement",
                    "Activité mensuelle", "Dossiers en cours, par étape", "Durée moyenne par étape", "Mutations par type", "Parcelles par île",
                    "Dossiers à débloquer", "À traiter par moi", "Activité récente"):
        assert attendu in page, attendu
    donnees = donnees_page(page)
    assert set(donnees) == {"activite", "pipeline", "iles", "mutations", "delais"}
    assert len(donnees["activite"]["mois"]) == 6 and len(donnees["activite"]["series"]) == 3
    # chaque graphique a sa vue en tableau et son bouton de bascule
    assert page.count('class="viz-tableau"') == 5 and page.count("Voir en tableau") == 5
    assert re.search(r'data-trace data-valeurs="(\d+,){11}\d+"', page)                              # 12 mois de tendance
    assert "alert-warning" in page and "sans avancement depuis plus de 30 jours" in page
    assert "viz.css" in page and "dashboard.js" in page


def test_les_filtres_s_appliquent_a_tout(app, login, jeu):
    dire = login("dir")
    page = dire.get("/?ile=2&periode=3").data.decode()
    donnees = donnees_page(page)
    assert '<option value="3" selected>' in page and "Réinitialiser" in page
    assert '<option value="2" selected>' not in page and 'name="ile"' not in page                    # le sélecteur d'île est réservé à l'administrateur
    assert '<option value="2" selected>' in login("admin").get("/?ile=2&periode=3").data.decode()
    assert donnees["activite"]["series"][1]["valeurs"] == [0, 0, 1]                                  # certificats : C seul (île 2), 3 mois
    assert all(l["valeur"] == 0 for l in donnees["pipeline"])
    # les filtres sont en tête de page : TOUT ce qui suit en dépend, y compris les listes « à faire »
    assert "Aucun dossier bloqué" in page and numero(app, jeu["D"]) not in page                       # D est à l'île 1
    assert "sans avancement depuis plus de 30 jours" not in page.split("Filtres")[1].split("Dossiers à débloquer")[0]  # plus d'alerte
    sans_filtre = dire.get("/").data.decode()
    assert numero(app, jeu["D"]) in sans_filtre and "sans avancement depuis plus de 30 jours" in sans_filtre
    assert all(l["url"].endswith("&statut=" + l["statut"]) or "ile=2" in l["url"] for l in donnees["pipeline"])
    # l'île filtrée reste en évidence, les autres passent en gris (emphase)
    assert {l["nom"]: l["actif"] for l in donnees["iles"]} == {"Ngazidja (Grande Comore)": False, "Ndzuwani (Anjouan)": True, "Mwali (Mohéli)": False}
    # valeurs invalides : retour aux réglages par défaut, jamais d'erreur
    page = dire.get("/?ile=999&periode=7").data.decode()
    assert len(donnees_page(page)["activite"]["mois"]) == 6 and "Réinitialiser" not in page
    assert dire.get("/?ile=abc&periode=xyz").status_code == 200


def test_liens_des_graphiques(app, login, jeu):
    dire = login("dir")
    donnees = donnees_page(dire.get("/").data.decode())
    for ligne in donnees["pipeline"] + donnees["mutations"]:
        assert dire.get(ligne["url"]).status_code == 200, ligne["url"]
    # « Parcelles par île » compte nouveaux ET anciens dossiers : pas de lien vers la liste, qui n'affiche que les nouveaux
    assert all("url" not in ligne for ligne in donnees["iles"])
    assert {l["url"] for l in donnees["pipeline"]} == {f"/dossiers?statut={e}" for e in St.ETAPES}
    avec_ile = donnees_page(dire.get("/?ile=1").data.decode())["pipeline"]
    assert all("ile=1" in l["url"] for l in avec_ile)                                                 # le filtre suit dans la liste


@pytest.mark.parametrize("qui", ["dir", "dom", "pub", "admin"])
def test_tous_les_roles_non_restreints_voient_les_graphiques(app, login, jeu, qui):
    page = login(qui).get("/").data.decode()
    assert "donnees-dashboard" in page and "Activité mensuelle" in page


def test_le_topographe_prive_garde_une_vue_limitee(app, login, jeu):
    priv = login("priv1")
    cree_priv = priv.post("/dossiers/nouveau", data={"proprio_nom": "Prive", "ile_id": "1", "commune": "X"})
    page = priv.get("/").data.decode()
    assert "donnees-dashboard" not in page and "viz-carte" not in page and "viz.css" not in page
    assert "Plan croquis en cours" in page and "Derniers dossiers" in page
    for secret in ("Dossiers à débloquer", "Activité mensuelle", "Parcelles par île", "Mutations par type"):
        assert secret not in page                                                                      # aucune statistique globale


def test_un_nom_hostile_ne_s_execute_pas(app, login):
    hostile = '<img src=x onerror=alert(1)>'
    pub = login("pub")
    r = pub.post("/dossiers/nouveau", data={"proprio_nom": hostile, "ile_id": "1", "commune": "X"})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    agir(pub, did, "terminer_plan")
    for qui in ("pub", "dir"):
        page = login(qui).get("/").data.decode()
        assert "<img src=x" not in page                                                               # jamais une vraie balise
        donnees_page(page)                                                                             # le JSON embarqué reste valide
    # le topographe public a ce dossier dans sa file de travail : le nom y est affiché, mais échappé
    assert "&lt;img src=x onerror=alert(1)&gt;" in login("pub").get("/").data.decode()


def test_script_et_style_sont_sains(app, login, jeu, tmp_path):
    js = open("app/static/js/dashboard.js", encoding="utf-8").read()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js        # libellés = données non fiables
    assert "textContent" in js
    css = open("app/static/css/viz.css", encoding="utf-8").read()
    for couleur in ("#2a78d6", "#eb6834", "#1baf7a"):                                                # palette validée (3 séries)
        assert couleur in css
    assert "dashed" not in css and "border: 1px solid var(--viz-grid" not in css                      # repères pleins, jamais en pointillé
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", "app/static/js/dashboard.js"], capture_output=True, text=True).returncode == 0
    assert login("dir").get("/static/js/dashboard.js").status_code == 200
    assert login("dir").get("/static/css/viz.css").status_code == 200


def test_filtres_et_cartes_sont_en_haut_de_page(app, login, jeu):
    page = login("dir").get("/").data.decode()
    ordre = [page.index(x) for x in ("Tableau de bord</h1>", 'aria-label="Filtres des statistiques"', "Dossiers en cours</div>",
                                      "Activité mensuelle", ">À faire<", "À traiter par moi", "Dossiers à débloquer", "Activité récente")]
    assert ordre == sorted(ordre), ordre                                    # titre, filtres, cartes, graphiques, puis les listes
    # les 4 cartes viennent juste après la rangée de filtres, avant toute alerte ou liste
    avant_cartes = page[: page.index("viz-tuile-libelle")]
    assert "alert-warning" not in avant_cartes and "list-group" not in avant_cartes and "viz-carte" not in avant_cartes
    assert page.count("viz-tuile-libelle") == 4


def test_la_file_de_travail_respecte_le_filtre_d_ile(app, login):
    """Un dossier à valider à l'île 1 disparaît de « À traiter par moi » quand on filtre l'île 2."""
    pub, dom, dire = login("pub"), login("dom"), login("dir")
    a = cree(login, nom="Ile1", identite="I-1", ile="1", certifier=False)
    for etape, qui, kw in (("terminer_plan", pub, {}), ("saisir_rsp", pub, {"valeur": "R"}), ("saisir_rcp", dom, {"valeur": "C"})):
        agir(qui, a, etape, **kw)                                              # le dossier attend maintenant le Directeur
    assert numero(app, a) in dire.get("/").data.decode()
    page = dire.get("/?ile=2").data.decode()
    assert numero(app, a) not in page.split(">À faire<")[1].split("Activité récente")[0]
    assert "Rien en attente" in page
    assert numero(app, a) in dire.get("/?ile=1").data.decode().split(">À faire<")[1]
    # le compteur de mutations à valider suit aussi le filtre
    from test_mutations import benef, saisir, derniere_mutation_id
    b = cree(login, nom="Ile2", identite="I-2", ile="2")
    saisir(dom, b, "vente", **{f"cede_{__import__('test_mutations').proprio_id(app, b, 'Ile2')}": "tout"}, **benef(("X", "1")))
    assert "1 mutation</strong> en attente" in dire.get("/?ile=2").data.decode()
    assert "mutation</strong> en attente" not in dire.get("/?ile=1").data.decode()
    assert "1 mutation</strong> en attente" in dire.get("/").data.decode()
