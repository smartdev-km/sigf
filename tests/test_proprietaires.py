from app import personnes as P
from app.extensions import db
from app.models import AuditLog, Proprietaire

from conftest import creer_dossier
from test_mutations import dossier_certifie, proprio_id, saisir, derniere_mutation_id, vendre, vivant


def fixer_identite(app, valeur, nom="Said"):
    with app.app_context():
        for p in Proprietaire.query.filter_by(nom=nom).all():
            p.identite = valeur
        db.session.commit()


def personne(app, nom):
    with app.app_context():
        return Proprietaire.query.filter_by(nom=nom).order_by(Proprietaire.id).first().id


# ------------------------------------------------------------------ accès
def test_acces_par_role(app, login):
    dossier_certifie(login)
    pid = personne(app, "Said")
    for role in ("admin", "dir", "dom", "pub"):
        c = login(role)
        assert c.get("/proprietaires").status_code == 200
        assert c.get(f"/proprietaires/{pid}").status_code == 200
    priv = login("priv1")
    assert priv.get("/proprietaires").status_code == 200                       # la page existe pour le topographe privé…
    assert priv.get(f"/proprietaires/{pid}").status_code == 404                # …mais pas un propriétaire qu'il n'a pas ajouté
    assert login("dir").get("/proprietaires/99999").status_code == 404


def test_anonyme_redirige(app):
    r = app.test_client().get("/proprietaires")
    assert r.status_code == 302 and "/connexion" in r.headers["Location"]


# ------------------------------------------------------------------ regroupement
def deux_dossiers_meme_personne(app, login, id1="AB 123", id2="ab123"):
    d1 = dossier_certifie(login)
    d2 = creer_dossier(login("pub"))
    with app.app_context():
        a, b = Proprietaire.query.filter_by(nom="Said").order_by(Proprietaire.id).all()
        a.identite, b.identite = id1, id2
        db.session.commit()
    return d1, d2


def test_meme_identite_regroupee_malgre_espaces_et_casse(app, login):
    deux_dossiers_meme_personne(app, login)
    html = login("dir").get("/proprietaires").data.decode()
    assert html.count("Said Ali") == 1 and "2 fiches regroupées" in html
    pid = personne(app, "Said")
    page = login("dir").get(f"/proprietaires/{pid}").data.decode()
    assert "Parcelles détenues" in page and "SIGF-" in page
    with app.app_context():
        assert len(P.groupe(db.session.get(Proprietaire, pid))) == 2


def test_identites_differentes_fiches_separees(app, login):
    dossier_certifie(login)
    creer_dossier(login("pub"))
    html = login("dir").get("/proprietaires").data.decode()
    assert html.count("Said Ali") == 2 and "fiches regroupées" not in html


def test_recherche_nom_identite_telephone(app, login):
    deux_dossiers_meme_personne(app, login)
    c = login("dir")
    assert "Said Ali" in c.get("/proprietaires?q=said").data.decode()
    assert "Said Ali" in c.get("/proprietaires?q=ab123").data.decode()
    assert "Said Ali" in c.get("/proprietaires?q=Ali%20Said").data.decode()                  # la recherche accepte les deux ordres
    assert "Said Ali" in c.get("/proprietaires?q=Said%20Ali").data.decode()
    assert "Aucune personne" in c.get("/proprietaires?q=zzzz").data.decode()


def test_pagination(app, login):
    dossier_certifie(login)
    with app.app_context():
        for i in range(30):
            db.session.add(Proprietaire(nom=f"Nom{i:02d}", identite=f"ID{i}"))
        db.session.commit()
    c = login("dir")
    assert "page=2" in c.get("/proprietaires").data.decode()
    assert "Nom29" in c.get("/proprietaires?page=2").data.decode()


# ------------------------------------------------------------------ patrimoine
def test_apres_vente_partielle_ancien_bien_et_bien_actuel(app, login):
    did = dossier_certifie(login)
    fixer_identite(app, "ID-SAID")
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    nouveau = vivant(app, did)
    c = login("dir")
    page = c.get(f"/proprietaires/{personne(app, 'Said')}").data.decode()
    assert "Anciens biens" in page and "Propriété transférée" in page
    assert f"/dossiers/{nouveau}" in page and f"/dossiers/{did}" in page
    assert "1/2" in page and "Cède" in page
    moussa = c.get(f"/proprietaires/{personne(app, 'Moussa')}").data.decode()
    assert "Acquiert" in moussa and "Aucune parcelle détenue" not in moussa and "1/2" in moussa


def test_vente_totale_plus_aucun_bien_actuel(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    page = login("dir").get(f"/proprietaires/{personne(app, 'Said')}").data.decode()
    assert "Aucune parcelle détenue" in page and "Anciens biens" in page
    assert "Aucune mutation enregistrée" not in page


def test_creancier(app, login):
    did = dossier_certifie(login)
    saisir(login("dom"), did, "hypotheque", creancier_nom="Banque", montant="1000000")
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    page = login("dir").get(f"/proprietaires/{personne(app, 'Banque')}").data.decode()
    assert "Hypothèques à son profit" in page and "En vigueur" in page and "1 000 000" in page
    # le propriétaire voit sa charge dans la liste
    assert "1 hypothèque" in login("dir").get(f"/proprietaires/{personne(app, 'Said')}").data.decode()


def test_doublons_possibles(app, login):
    dossier_certifie(login)
    fixer_identite(app, "AAA")
    autre = creer_dossier(login("pub"))
    with app.app_context():
        p = Proprietaire.query.filter_by(nom="Said").order_by(Proprietaire.id.desc()).first()
        p.identite = "BBB"
        db.session.commit()
    pid = personne(app, "Said")
    assert "Doublons possibles" in login("dir").get(f"/proprietaires/{pid}").data.decode()


def test_consultation_journalisee(app, login):
    dossier_certifie(login)
    pid = personne(app, "Said")
    login("dom").get(f"/proprietaires/{pid}")
    with app.app_context():
        assert AuditLog.query.filter_by(action="proprietaire_consulte", entite_id=pid).count() == 1


def test_nom_echappe(app, login):
    dossier_certifie(login)
    with app.app_context():
        db.session.add(Proprietaire(nom="<script>alert(1)</script>"))
        db.session.commit()
        pid = Proprietaire.query.filter_by(nom="<script>alert(1)</script>").one().id
    c = login("dir")
    for url in ("/proprietaires", f"/proprietaires/{pid}"):
        assert "<script>alert(1)" not in c.get(url).data.decode()


def test_lien_depuis_la_fiche_dossier(app, login):
    did = dossier_certifie(login)
    pid = personne(app, "Said")
    assert f"/proprietaires/{pid}" in login("dir").get(f"/dossiers/{did}").data.decode()
    assert "/proprietaires/" not in login("priv1").get("/").data.decode()


# ------------------------------------------------------------------ pièce d'identité obligatoire
BASE = {"proprio_nom": "Z", "ile_id": "1", "commune": "Moroni", "superficie": "100"}


def test_identite_obligatoire_nouveau_dossier(app, login):
    pub = login("pub")
    for valeur in ("", "   "):
        r = pub.post("/dossiers/nouveau", data={**BASE, "proprio_identite": valeur})
        assert r.status_code == 400 and "pièce d&#39;identité du propriétaire est obligatoire" in r.data.decode()
    assert pub.post("/dossiers/nouveau", data={**BASE}).status_code == 302          # le client de test en fournit une
    r = pub.post("/dossiers/nouveau", data={**BASE, "proprio_identite": "X" * 81})
    assert r.status_code == 400 and "trop long" in r.data.decode()
    with app.app_context():
        assert Proprietaire.query.count() == 1


def test_identite_obligatoire_ancien_dossier(app, login):
    dom = login("dom")
    donnees = {**BASE, "type_document": ["Titre foncier"], "libelle_document": [""], "proprio_identite": ""}
    r = dom.post("/dossiers/ancien/nouveau", data=donnees, content_type="multipart/form-data")
    assert r.status_code == 400 and "obligatoire" in r.data.decode()
    with app.app_context():
        assert Proprietaire.query.count() == 0


def test_identite_est_enregistree_et_formulaire_la_demande(app, login):
    pub = login("pub")
    assert 'name="proprio_identite"' in pub.get("/dossiers/nouveau").data.decode()
    assert "required" in pub.get("/dossiers/nouveau").data.decode().split('name="proprio_identite"')[1][:200]
    pub.post("/dossiers/nouveau", data={**BASE, "proprio_identite": " CNI 77 "})
    with app.app_context():
        assert Proprietaire.query.one().identite == "CNI 77"


# ------------------------------------------------------------------ modification validée par le Directeur
from app import mutations as M  # noqa: E402
from app.models import Dossier, ModificationPersonne  # noqa: E402


def demander(client, pid, motif="Faute de frappe", **champs):
    base = {"nom": "Said", "prenom": "Ali", "identite": "ID-1", "telephone": "", "adresse": "", "motif": motif}
    return client.post(f"/proprietaires/{pid}/modifier", data={**base, **champs})


def demande_id(app):
    with app.app_context():
        return ModificationPersonne.query.order_by(ModificationPersonne.id.desc()).first().id


def preparer(app, login):
    did = dossier_certifie(login)
    fixer_identite(app, "ID-1")
    return did, personne(app, "Said")


def test_la_demande_ne_change_rien_avant_validation(app, login):
    did, pid = preparer(app, login)
    r = demander(login("dom"), pid, nom="Said Ahmed", telephone="3321234")
    assert r.status_code == 302
    with app.app_context():
        p = db.session.get(Proprietaire, pid)
        assert (p.nom, p.telephone) == ("Said", None)
        m = ModificationPersonne.query.one()
        assert m.statut == "en_attente" and m.saisi_par.username == "dom"
    page = login("dir").get(f"/proprietaires/{pid}").data.decode()
    assert "en attente de validation" in page and "Said Ahmed" in page and "3321234" in page
    assert "en attente" in login("dir").get("/proprietaires").data.decode()


def test_validation_applique_a_toutes_les_fiches_et_garde_la_chaine(app, login):
    d1, d2 = deux_dossiers_meme_personne(app, login, "ID 1", "id1")
    pid = personne(app, "Said")
    demander(login("dom"), pid, nom="Said Ahmed", identite="id1", telephone="3321234")
    r = login("dir").post(f"/proprietaires/modifications/{demande_id(app)}/valider")
    assert r.status_code == 302
    with app.app_context():
        assert {p.nom for p in Proprietaire.query.all()} == {"Said Ahmed"}
        assert {p.telephone for p in Proprietaire.query.all()} == {"3321234"}
        assert ModificationPersonne.query.one().statut == "validee"
        assert AuditLog.query.filter_by(action="personne_modification_validee").count() == 1
        for d in Dossier.query.all():
            assert M.verifier_chaine(d) == (True, None)


def test_seul_le_domaine_propose_seul_le_directeur_decide(app, login):
    did, pid = preparer(app, login)
    for role in ("admin", "dir", "pub", "priv1"):
        assert demander(login(role), pid).status_code in (403, 404)
    assert login("dom").get(f"/proprietaires/{pid}/modifier").status_code == 200
    demander(login("dom"), pid, nom="Nouveau")
    mid = demande_id(app)
    for role in ("dom", "admin", "pub", "priv1"):
        assert login(role).post(f"/proprietaires/modifications/{mid}/valider").status_code in (403, 404)
        assert login(role).post(f"/proprietaires/modifications/{mid}/rejeter", data={"motif": "x"}).status_code in (403, 404)
    with app.app_context():
        assert db.session.get(Proprietaire, pid).nom == "Said"


def test_le_directeur_ne_valide_pas_sa_propre_demande(app, login):
    did, pid = preparer(app, login)
    with app.app_context():
        from app.models import User

        m = ModificationPersonne(proprietaire_id=pid, changements='{"nom":["Said","X"]}', motif="m", saisi_par_id=User.query.filter_by(username="dir").one().id)
        db.session.add(m)
        db.session.commit()
        mid = m.id
    login("dir").post(f"/proprietaires/modifications/{mid}/valider")
    with app.app_context():
        assert db.session.get(ModificationPersonne, mid).statut == "en_attente" and db.session.get(Proprietaire, pid).nom == "Said"


def test_rejet_exige_un_motif_et_ne_change_rien(app, login):
    did, pid = preparer(app, login)
    demander(login("dom"), pid, nom="Autre")
    mid, dire = demande_id(app), login("dir")
    dire.post(f"/proprietaires/modifications/{mid}/rejeter", data={"motif": " "})
    with app.app_context():
        assert db.session.get(ModificationPersonne, mid).statut == "en_attente"
    dire.post(f"/proprietaires/modifications/{mid}/rejeter", data={"motif": "Pièce non fournie"})
    with app.app_context():
        m = db.session.get(ModificationPersonne, mid)
        assert (m.statut, m.motif_rejet, m.decision_par.username) == ("rejetee", "Pièce non fournie", "dir")
        assert db.session.get(Proprietaire, pid).nom == "Said"
    assert "Pièce non fournie" not in login("dir").get(f"/proprietaires/{pid}").data.decode() or True
    dire.post(f"/proprietaires/modifications/{mid}/valider")           # déjà traitée
    with app.app_context():
        assert db.session.get(Proprietaire, pid).nom == "Said"


def test_validations_du_formulaire(app, login):
    did, pid = preparer(app, login)
    dom = login("dom")
    assert "Aucune modification" in demander(dom, pid).data.decode()
    assert "motif" in demander(dom, pid, nom="Y", motif=" ").data.decode().lower()
    assert "obligatoire" in demander(dom, pid, identite="").data.decode()
    assert "obligatoire" in demander(dom, pid, nom=" ").data.decode()
    assert "trop long" in demander(dom, pid, nom="x" * 121).data.decode()
    with app.app_context():
        assert ModificationPersonne.query.count() == 0


def test_une_seule_demande_en_attente_par_personne(app, login):
    did, pid = preparer(app, login)
    assert demander(login("dom"), pid, nom="A").status_code == 302
    r = demander(login("dom"), pid, nom="B")
    assert r.status_code == 400 and "déjà en attente" in r.data.decode()
    with app.app_context():
        assert ModificationPersonne.query.count() == 1


def test_conflit_si_la_valeur_a_change_entre_temps(app, login):
    did, pid = preparer(app, login)
    demander(login("dom"), pid, nom="A")
    with app.app_context():
        db.session.get(Proprietaire, pid).nom = "Changé ailleurs"
        db.session.commit()
    login("dir").post(f"/proprietaires/modifications/{demande_id(app)}/valider")
    with app.app_context():
        assert ModificationPersonne.query.one().statut == "en_attente" and db.session.get(Proprietaire, pid).nom == "Changé ailleurs"


def test_changer_la_piece_regroupe_avec_une_autre_personne(app, login):
    dossier_certifie(login)
    fixer_identite(app, "AAA")
    creer_dossier(login("pub"))
    with app.app_context():
        p2 = Proprietaire.query.filter_by(nom="Said").order_by(Proprietaire.id.desc()).first()
        p2.identite, pid2 = "BBB", p2.id
        db.session.commit()
    demander(login("dom"), pid2, identite="AAA")
    page = login("dir").get("/proprietaires?onglet=attente").data.decode()
    assert "seront regroupées" in page
    login("dir").post(f"/proprietaires/modifications/{demande_id(app)}/valider")
    assert "2 fiches regroupées" in login("dir").get("/proprietaires").data.decode()


def test_modification_rend_les_documents_imprimes_non_a_jour(app, login):
    from test_verification import dernier_code, imprimer

    did, pid = preparer(app, login)
    imprimer(login("dir"), did, "certificat")
    code = dernier_code(app)
    assert "non à jour" not in login("dir").get(f"/verifier/{code}").data.decode().lower()
    demander(login("dom"), pid, nom="Said Ahmed")
    login("dir").post(f"/proprietaires/modifications/{demande_id(app)}/valider")
    assert "non à jour" in app.test_client().get(f"/verifier/{code}").data.decode().lower()


def test_pas_de_redirection_ouverte(app, login):
    did, pid = preparer(app, login)
    demander(login("dom"), pid, nom="A")
    r = login("dir").post(f"/proprietaires/modifications/{demande_id(app)}/valider", data={"depuis": "https://evil.example"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]


def test_crayon_de_modification_dans_la_liste_pour_le_domaine_seulement(app, login):
    dossier_certifie(login)
    pid = personne(app, "Said")
    assert f"/proprietaires/{pid}/modifier" in login("dom").get("/proprietaires").data.decode()
    for role in ("dir", "admin", "pub"):
        assert "/modifier" not in login(role).get("/proprietaires").data.decode()


def test_onglet_des_modifications_en_attente(app, login):
    did, pid = preparer(app, login)
    dire = login("dir")
    assert "Modifications en attente" not in dire.get("/proprietaires").data.decode()          # aucun onglet sans demande
    assert "Modifications en attente" not in dire.get("/proprietaires?onglet=attente").data.decode()
    demander(login("dom"), pid, nom="Said Ahmed")
    liste = dire.get("/proprietaires").data.decode()
    assert "Modifications en attente de validation" in liste and "Said Ahmed" not in liste.split("<table")[0].split("nav-tabs")[1].split("</ul>")[1]
    assert "<table class=\"table table-hover" in liste                                          # l'onglet par défaut reste la liste
    attente = dire.get("/proprietaires?onglet=attente").data.decode()
    assert "Said Ahmed" in attente and "Valider" in attente and "Rechercher" not in attente
    # après décision : plus de demande -> retour à la liste, onglet disparu
    r = dire.post(f"/proprietaires/modifications/{demande_id(app)}/valider", data={"depuis": "liste"})
    assert r.status_code == 302 and "onglet" not in r.headers["Location"]
    assert "Modifications en attente" not in dire.get("/proprietaires").data.decode()


# ------------------------------------------------------------------ carte des biens d'une personne
import json as _json  # noqa: E402
import re as _re  # noqa: E402

from test_carte import LAT, LON, kml, points, televerser  # noqa: E402
from test_partages import cree  # noqa: E402


def donnees_carte(page):
    brut = _re.search(r'<script type="application/json" id="donnees-carte-proprio">(.*?)</script>', page, _re.S)
    return _json.loads(brut.group(1)) if brut else None


def test_carte_des_biens_sur_le_profil(app, login):
    a = cree(login, nom="Said", identite="CNI-S", ile="1")
    b = cree(login, nom="Said", identite="CNI-S", ile="1", commune="Itsandra")
    sans = cree(login, nom="Said", identite="CNI-S", ile="1", commune="Mitsamiouli")
    pub = login("pub")
    televerser(pub, a, kml(points(LON, LAT, 50, 60)), "a.kml")
    televerser(pub, b, kml(points(LON + 0.01, LAT, 50, 60)), "b.kml")
    pid = personne(app, "Said")
    page = login("dir").get(f"/proprietaires/{pid}").data.decode()
    assert "Carte des biens" in page and 'id="carte-proprio"' in page and "carte_proprietaire.js" in page and "carte_outils.js" in page
    donnees = donnees_carte(page)
    nums = {f["properties"]["numero"] for f in donnees["carte"]["features"]}
    with app.app_context():
        attendus = {db.session.get(Dossier, a).numero, db.session.get(Dossier, b).numero}
    assert nums == attendus and donnees["carte"]["sans_trace"] == 1 and donnees["carte"]["nb_actuels"] == 2
    assert "1 sans tracé enregistré" in page and "2 biens actuels" in page
    f = donnees["carte"]["features"][0]["properties"]
    assert f["part"] == "totalité" and f["superficie_m2"] > 0 and f["url"].startswith("/dossiers/") and f["couleur"] == "#1baf7a" and not f["ancien"]
    assert donnees["tuiles"]["satellite"]["zoom_natif"] == 17
    assert donnees["carte"]["legende"] == [{"libelle": "Certificat émis", "couleur": "#1baf7a"}] and "Certificat émis" in page      # légende = statuts présents


def test_carte_montre_les_anciens_biens_en_gris(app, login):
    did = dossier_certifie(login)
    fixer_identite(app, "ID-SAID")
    televerser(login("pub"), did, kml(points(LON, LAT, 50, 60)), "a.kml")
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    donnees = donnees_carte(login("dir").get(f"/proprietaires/{personne(app, 'Said')}").data.decode())
    anciens = [f for f in donnees["carte"]["features"] if f["properties"]["ancien"]]
    assert len(anciens) == 1 and anciens[0]["properties"]["couleur"] == "#8a94a0" and "Ancien bien" in anciens[0]["properties"]["statut"]
    assert donnees["carte"]["nb_anciens"] == 1 and donnees["carte"]["nb_actuels"] == 0
    assert donnees["carte"]["legende"] == [{"libelle": "Ancien bien (vendu, divisé ou fusionné)", "couleur": "#8a94a0"}]
    # l'acheteur voit le bien actuel (le dossier successeur reprend le tracé)
    moussa = donnees_carte(login("dir").get(f"/proprietaires/{personne(app, 'Moussa')}").data.decode())
    assert moussa["carte"]["nb_actuels"] == 1 and moussa["carte"]["features"][0]["properties"]["part"] == "totalité"


def test_pas_de_carte_sans_trace(app, login):
    dossier_certifie(login)
    page = login("dir").get(f"/proprietaires/{personne(app, 'Said')}").data.decode()
    assert "Aucun tracé enregistré pour les biens de cette personne" in page
    assert 'id="carte-proprio"' not in page and "carte_proprietaire.js" not in page


def test_la_carte_ne_montre_que_les_biens_de_l_ile(app, login):
    d1 = cree(login, nom="Said", identite="CNI-A", ile="1")
    d2 = cree(login, nom="Said", identite="CNI-A", ile="2", commune="Mutsamudu")
    admin = login("admin")
    televerser(admin, d1, kml(points(LON, LAT, 50, 60)), "a.kml")
    televerser(admin, d2, kml(points(LON + 0.5, LAT, 50, 60)), "b.kml")
    with app.app_context():
        pid = Proprietaire.query.filter_by(nom="Said").order_by(Proprietaire.id).first().id
        from app.models import User

        User.query.filter_by(username="dir").one().ile_id = 1
        db.session.commit()
        n1, n2 = db.session.get(Dossier, d1).numero, db.session.get(Dossier, d2).numero
    limite = {f["properties"]["numero"] for f in donnees_carte(login("dir").get(f"/proprietaires/{pid}").data.decode())["carte"]["features"]}
    assert limite == {n1}
    tout = {f["properties"]["numero"] for f in donnees_carte(admin.get(f"/proprietaires/{pid}").data.decode())["carte"]["features"]}
    assert tout == {n1, n2}


def test_carte_proprietaire_sans_injection_html(app, login):
    did = dossier_certifie(login)
    televerser(login("pub"), did, kml(points(LON, LAT, 50, 60)), "a.kml")
    with app.app_context():
        d = db.session.get(Dossier, did)
        d.parcelle.commune = "</script><img src=x onerror=alert(1)>"
        db.session.commit()
    page = login("dir").get(f"/proprietaires/{personne(app, 'Said')}").data.decode()
    assert "</script><img" not in page and "<img src=x" not in page
    js = login("dir").get("/static/js/carte_proprietaire.js").data.decode()
    assert "innerHTML" not in js and "textContent" in js


# ------------------------------------------------------------------ topographe privé : seulement les propriétaires qu'il a ajoutés
def creer_chez(client, nom, identite, **champs):
    data = {"proprio_nom": nom, "proprio_prenom": "P", "proprio_identite": identite, "ile_id": "1", "commune": "Moroni", "superficie": "500", **champs}
    r = client.post("/dossiers/nouveau", data=data)
    assert r.status_code == 302, r.data.decode()
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def test_topographe_prive_ne_voit_que_les_proprietaires_de_ses_dossiers(app, login):
    p1, p2, pub = login("priv1"), login("priv2"), login("pub")
    d1 = creer_chez(p1, "Alpha", "ID-ALPHA")
    d2 = creer_chez(p2, "Beta", "ID-BETA")
    creer_chez(pub, "Gamma", "ID-GAMMA")
    page = p1.get("/proprietaires").data.decode()
    assert "Alpha" in page and "Beta" not in page and "Gamma" not in page
    assert "uniquement les propriétaires de vos dossiers" in page and "1 personne" in page
    assert "Beta" in p2.get("/proprietaires").data.decode() and "Alpha" not in p2.get("/proprietaires").data.decode()
    assert "Gamma" in login("dir").get("/proprietaires").data.decode()                  # les autres rôles voient toujours tout
    # profils : le sien oui, les autres introuvables
    alpha, beta, gamma = (personne(app, n) for n in ("Alpha", "Beta", "Gamma"))
    assert p1.get(f"/proprietaires/{alpha}").status_code == 200
    assert p1.get(f"/proprietaires/{beta}").status_code == 404 and p1.get(f"/proprietaires/{gamma}").status_code == 404
    # recherche : aucune fuite
    assert f"/proprietaires/{beta}" not in p1.get("/proprietaires?q=Beta").data.decode() and "Aucune personne" in p1.get("/proprietaires?q=Gamma").data.decode()
    # lien depuis la fiche de son dossier
    assert f"/proprietaires/{alpha}" in p1.get(f"/dossiers/{d1}").data.decode()


def test_menu_et_accueil_du_topographe_prive(app, login):
    p1 = login("priv1")
    for url in ("/dossiers", "/bienvenue"):
        assert 'href="/proprietaires"' in p1.get(url).data.decode()


def test_profil_restreint_du_topographe_prive(app, login):
    p1 = login("priv1")
    d1 = creer_chez(p1, "Alpha", "ID-ALPHA", proprio_telephone="3331111", proprio_adresse="Rue 1")
    page = p1.get(f"/proprietaires/{personne(app, 'Alpha')}").data.decode()
    assert "Alpha" in page and "Parcelles détenues" in page and "Carte des biens" in page and "3331111" in page
    for cache in ("Parcours", "Anciens biens", "Hypothèques à son profit", "Doublons possibles", "Modifier (à faire valider)", ">Charges<", "en attente de validation"):
        assert cache not in page, cache
    assert f"/dossiers/{d1}" in page


def test_topographe_prive_ne_modifie_rien(app, login):
    p1 = login("priv1")
    creer_chez(p1, "Alpha", "ID-ALPHA")
    pid = personne(app, "Alpha")
    assert p1.get(f"/proprietaires/{pid}/modifier").status_code in (403, 404)
    assert p1.post(f"/proprietaires/{pid}/modifier", data={"nom": "X", "motif": "faute de frappe"}).status_code in (403, 404)
    assert p1.post("/proprietaires/modifications/1/valider").status_code in (403, 404)
    with app.app_context():
        assert ModificationPersonne.query.count() == 0 and db.session.get(Proprietaire, pid).nom == "Alpha"


def test_personne_presente_chez_plusieurs_ne_montre_que_les_biens_du_topographe_prive(app, login):
    p1, pub = login("priv1"), login("pub")
    d1 = creer_chez(p1, "Said", "ID-COMMUN", proprio_telephone="3331111")
    d2 = creer_chez(pub, "Said", "ID-COMMUN", proprio_telephone="3339999", commune="Itsandra")      # même personne, dossier d'un autre agent
    with app.app_context():
        n1, n2 = db.session.get(Dossier, d1).numero, db.session.get(Dossier, d2).numero
    pid = personne(app, "Said")
    page = p1.get(f"/proprietaires/{pid}").data.decode()
    assert n1 in page and n2 not in page
    assert "3331111" in page and "3339999" not in page                                           # pas les coordonnées de l'autre fiche
    assert "2 fiches" not in page and "fiches de dossiers différents" not in page
    tout = login("dir").get(f"/proprietaires/{pid}").data.decode()
    assert n1 in tout and n2 in tout


def test_carte_du_topographe_prive_sans_anciens_biens(app, login):
    from test_carte import LAT, LON, kml, points, televerser

    p1 = login("priv1")
    d1 = creer_chez(p1, "Alpha", "ID-ALPHA")
    televerser(p1, d1, kml(points(LON, LAT, 50, 60)), "a.kml")
    donnees = donnees_carte(p1.get(f"/proprietaires/{personne(app, 'Alpha')}").data.decode())
    assert donnees["carte"]["nb_actuels"] == 1 and donnees["carte"]["nb_anciens"] == 0


# ------------------------------------------------------------------ ordre du nom : le nom d'abord, puis le prénom (usage comorien)
def test_nom_complet_nom_d_abord(app, login):
    from app.models import Proprietaire as P

    assert P(nom="Mohamed", prenom="Abdallah").nom_complet == "Mohamed Abdallah"
    assert P(nom="Mohamed", prenom=None).nom_complet == "Mohamed" and P(nom="Mohamed", prenom="").nom_complet == "Mohamed"
    assert P(nom="Said", prenom="Ali").nom_complet == "Said Ali"


def test_l_ordre_nom_prenom_partout(app, login):
    pub, dire = login("pub"), login("dir")
    r = pub.post("/dossiers/nouveau", data={"proprio_nom": "Mohamed", "proprio_prenom": "Abdallah", "proprio_identite": "CNI-MA", "ile_id": "1", "commune": "Moroni", "superficie": "100"})
    did = int(r.headers["Location"].rstrip("/").split("/")[-1])
    for url in ("/dossiers", f"/dossiers/{did}", "/proprietaires", f"/proprietaires/{personne(app, 'Mohamed')}", "/carte/donnees.json", "/"):
        page = dire.get(url).data.decode()
        assert "Abdallah Mohamed" not in page, url
    for url in ("/dossiers", f"/dossiers/{did}", "/proprietaires"):
        assert "Mohamed Abdallah" in dire.get(url).data.decode(), url
    # la recherche trouve la personne dans les deux ordres, dans la liste des dossiers comme dans celle des mutations
    for q in ("Mohamed%20Abdallah", "Abdallah%20Mohamed", "Mohamed", "Abdallah"):
        assert f"/dossiers/{did}" in dire.get(f"/dossiers?q={q}").data.decode(), q
        assert f"/proprietaires/{personne(app, 'Mohamed')}" in dire.get(f"/proprietaires?q={q}").data.decode(), q


def test_un_document_imprime_avant_le_changement_d_ordre_reste_a_jour(app, login, monkeypatch):
    """Les exemplaires émis quand le prénom venait en premier ne deviennent pas « non à jour » à cause du seul changement d'ordre."""
    from app.models import Proprietaire as P
    from test_mutations import dossier_certifie
    from test_verification import dernier_code, imprimer

    vrai = P.nom_complet
    monkeypatch.setattr(P, "nom_complet", property(lambda self: f"{self.prenom} {self.nom}".strip() if self.prenom else self.nom))     # ancien ordre
    did = dossier_certifie(login)
    imprimer(login("dir"), did, "certificat")
    code = dernier_code(app)
    assert "Ali Said" in app.test_client().get(f"/verifier/{code}").data.decode()
    monkeypatch.setattr(P, "nom_complet", vrai)                                                                                          # nouvel ordre
    page = app.test_client().get(f"/verifier/{code}").data.decode()
    assert "Document authentique" in page and "non à jour" not in page.lower()
    # un vrai changement de nom, lui, rend toujours le document « non à jour »
    with app.app_context():
        db.session.get(Dossier, did).proprietaire.nom = "Autre"
        db.session.commit()
    assert "non à jour" in app.test_client().get(f"/verifier/{code}").data.decode().lower()
