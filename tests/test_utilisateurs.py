from app import constants as C
from app.extensions import db
from app.models import AuditLog, User

from conftest import MDP, connecter, creer_dossier


def uid(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id


def creer(app, username, role, nom="Compte Test"):
    with app.app_context():
        u = User(nom=nom, username=username, role=role, doit_changer_mdp=False)
        u.set_password(MDP)
        db.session.add(u)
        db.session.commit()
        return u.id


# ------------------------------------------------------------------ onglets
def test_onglets_par_type_de_compte(app, login):
    admin = login("admin")
    page = admin.get("/utilisateurs/").data.decode()
    for libelle in ("Tous", "Admin", "Directeurs", "Topo Public", "Topo Prive", "Domaine", "Fonct Pub"):
        assert libelle in page
    privés = admin.get("/utilisateurs/?role=topo_prive").data.decode()
    assert "priv1" in privés and "priv2" in privés and ">dom<" not in privés and ">dir<" not in privés
    assert "dom" in admin.get("/utilisateurs/?role=domaine").data.decode()
    assert "priv1" in admin.get("/utilisateurs/?role=inconnu").data.decode()      # rôle inconnu : tous les comptes


def test_le_directeur_ne_voit_que_les_topographes_prives(app, login):
    dire = login("dir")
    page = dire.get("/utilisateurs/").data.decode()
    assert "priv1" in page and "role=admin" not in page
    assert ">admin<" not in dire.get("/utilisateurs/?role=admin").data.decode()


# ------------------------------------------------------------------ modification
def test_modifier_nom_identifiant_et_role(app, login):
    admin, i = login("admin"), creer(app, "nouveau.compte", C.DOMAINE)
    r = admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "Autre Nom", "username": "Autre.Compte", "role": C.TOPO_PUBLIC, "ile_id": "1", "password": ""})
    assert r.status_code == 302
    with app.app_context():
        u = db.session.get(User, i)
        assert (u.nom, u.username, u.role) == ("Autre Nom", "autre.compte", C.TOPO_PUBLIC)
        assert u.check_password(MDP) and not u.doit_changer_mdp
        assert AuditLog.query.filter_by(action="modification_utilisateur", entite_id=i).count() == 1
    connecter(app, "autre.compte")


def test_reinitialiser_le_mot_de_passe(app, login):
    admin, i = login("admin"), creer(app, "x.user", C.DOMAINE)
    assert "au moins" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "password": "court"}).data.decode()
    admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "ile_id": "1", "password": "nouveau-mdp-solide"})
    with app.app_context():
        u = db.session.get(User, i)
        assert u.check_password("nouveau-mdp-solide") and u.doit_changer_mdp


def test_validations_modification(app, login):
    admin, i = login("admin"), creer(app, "x.user", C.DOMAINE)
    assert "déjà utilisé" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "dir"}).data.decode()
    assert "Identifiant invalide" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "a b"}).data.decode()
    assert "obligatoire" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": " ", "username": "x.user"}).data.decode()
    assert "non autorisé" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "role": "roi"}).data.decode()


def test_role_inchange_pour_soi_meme_et_dernier_admin(app, login):
    admin = login("admin")
    me = uid(app, "admin")
    r = admin.post(f"/utilisateurs/{me}/modifier", data={"nom": "Admin", "username": "admin", "role": C.DOMAINE})
    assert r.status_code == 400
    autre = creer(app, "admin2", C.ADMIN)
    with app.app_context():
        db.session.get(User, me).actif = False
        db.session.commit()
    # admin2 est maintenant le seul administrateur actif
    a2 = connecter(app, "admin2")
    assert a2.post(f"/utilisateurs/{autre}/modifier", data={"nom": "A", "username": "admin2", "role": C.DOMAINE}).status_code == 400


def test_topographe_rattache_a_des_dossiers_garde_son_role(app, login):
    creer_dossier(login("priv1"))
    r = login("admin").post(f"/utilisateurs/{uid(app, 'priv1')}/modifier", data={"nom": "P", "username": "priv1", "role": C.DOMAINE})
    assert r.status_code == 400 and "rattaché" in r.data.decode()


def test_le_directeur_modifie_un_topo_prive_mais_pas_son_role_ni_les_autres(app, login):
    dire = login("dir")
    p = uid(app, "priv2")
    assert dire.post(f"/utilisateurs/{p}/modifier", data={"nom": "Nouveau Nom", "username": "priv2", "role": C.ADMIN}).status_code == 400
    assert dire.post(f"/utilisateurs/{p}/modifier", data={"nom": "Nouveau Nom", "username": "priv2", "ile_id": "1"}).status_code == 302
    with app.app_context():
        u = db.session.get(User, p)
        assert (u.nom, u.role) == ("Nouveau Nom", C.TOPO_PRIVE)
    assert dire.get(f"/utilisateurs/{uid(app, 'dom')}/modifier").status_code == 404
    assert dire.post(f"/utilisateurs/{uid(app, 'dom')}/supprimer").status_code == 404


def test_autres_roles_sans_acces(app, login):
    i = uid(app, "priv2")
    for role in ("dom", "pub", "priv1"):
        c = login(role)
        assert c.get(f"/utilisateurs/{i}/modifier").status_code == 403
        assert c.post(f"/utilisateurs/{i}/supprimer").status_code == 403


# ------------------------------------------------------------------ suppression
def test_suppression_d_un_compte_sans_activite(app, login):
    admin, i = login("admin"), creer(app, "jetable", C.DOMAINE)
    assert admin.post(f"/utilisateurs/{i}/supprimer").status_code == 302
    with app.app_context():
        assert db.session.get(User, i) is None
        assert AuditLog.query.filter_by(action="suppression_utilisateur", entite_id=i).count() == 1
    assert "jetable" not in admin.get("/utilisateurs/").data.decode()


def test_suppression_refusee_si_activite(app, login):
    creer_dossier(login("pub"))
    admin = login("admin")
    admin.post(f"/utilisateurs/{uid(app, 'pub')}/supprimer")
    with app.app_context():
        assert User.query.filter_by(username="pub").count() == 1


def test_suppression_refusee_soi_meme_et_dernier_admin(app, login):
    admin = login("admin")
    admin.post(f"/utilisateurs/{uid(app, 'admin')}/supprimer")
    with app.app_context():
        assert User.query.filter_by(username="admin").count() == 1
    autre = creer(app, "admin2", C.ADMIN)
    with app.app_context():
        db.session.get(User, uid(app, "admin")).actif = False
        db.session.commit()
    connecter(app, "admin2").post(f"/utilisateurs/{autre}/supprimer")
    with app.app_context():
        assert User.query.filter_by(username="admin2").count() == 1


def test_suppression_detache_les_comptes_qu_il_a_crees(app, login):
    admin2 = creer(app, "admin2", C.ADMIN)
    c = connecter(app, "admin2")
    c.post("/utilisateurs/nouveau", data={"nom": "Fils", "username": "fils", "role": C.DOMAINE, "ile_id": "1", "password": "motdepasse-provisoire"})
    with app.app_context():
        assert User.query.filter_by(username="fils").one().cree_par_id == admin2
    with app.app_context():
        for a in AuditLog.query.filter_by(user_id=admin2).all():
            db.session.delete(a)          # journal de l'exemple vidé : on teste seulement le rattachement
        db.session.commit()
    login("admin").post(f"/utilisateurs/{admin2}/supprimer")
    with app.app_context():
        assert db.session.get(User, admin2) is None and User.query.filter_by(username="fils").one().cree_par_id is None


# ------------------------------------------------------------------ téléphone et île attribuée
def nouveau_compte(client, **champs):
    base = {"nom": "Aïcha Ali", "username": "aicha", "role": C.DOMAINE, "ile_id": "2", "telephone": "+269 333 20 20", "password": "motdepasse-provisoire"}
    return client.post("/utilisateurs/nouveau", data={**base, **champs})


def test_nouveau_compte_avec_telephone_et_ile(app, login):
    admin = login("admin")
    page = admin.get("/utilisateurs/nouveau").data.decode()
    assert 'name="telephone"' in page and 'name="ile_id"' in page and "Île attribuée" in page
    assert nouveau_compte(admin).status_code == 302
    with app.app_context():
        u = User.query.filter_by(username="aicha").one()
        assert (u.telephone, u.ile_id, u.role) == ("+269 333 20 20", 2, C.DOMAINE)
        assert AuditLog.query.filter_by(action="creation_utilisateur", entite_id=u.id).one().nouveau.endswith("île Ndzuwani (Anjouan)") or "île" in AuditLog.query.filter_by(action="creation_utilisateur", entite_id=u.id).one().nouveau
    page = admin.get("/utilisateurs/").data.decode()
    assert "+269 333 20 20" in page and "Ndzuwani" in page


def test_ile_obligatoire_sauf_pour_l_administrateur(app, login):
    admin = login("admin")
    r = nouveau_compte(admin, ile_id="")
    assert r.status_code == 400 and "île attribuée est obligatoire" in r.data.decode()
    assert nouveau_compte(admin, ile_id="99").status_code == 400
    assert nouveau_compte(admin, role=C.ADMIN, ile_id="", username="admin3").status_code == 302      # l'administrateur n'a jamais d'île
    assert nouveau_compte(admin, role=C.ADMIN, ile_id="2", username="admin4").status_code == 302
    with app.app_context():
        assert User.query.filter_by(username="admin3").one().ile_id is None
        assert User.query.filter_by(username="admin4").one().ile_id is None                       # ignorée pour un administrateur
        assert User.query.filter_by(username="aicha").count() == 0


def test_telephone_du_nouveau_compte_valide(app, login):
    admin = login("admin")
    assert "Numéro de téléphone invalide" in nouveau_compte(admin, telephone="abc").data.decode()
    assert nouveau_compte(admin, telephone="").status_code == 302                                  # facultatif


def test_le_directeur_cree_des_topographes_sur_son_ile(app, login):
    with app.app_context():
        User.query.filter_by(username="dir").one().ile_id = 3
        db.session.commit()
    dire = login("dir")
    page = dire.get("/utilisateurs/nouveau").data.decode()
    assert "Le compte est créé sur votre île" in page and "Mwali" in page and 'name="ile_id"' not in page
    r = dire.post("/utilisateurs/nouveau", data={"nom": "Topo", "username": "topo.mwali", "ile_id": "1", "password": "motdepasse-provisoire"})   # l'île envoyée est ignorée
    assert r.status_code == 302
    with app.app_context():
        assert User.query.filter_by(username="topo.mwali").one().ile_id == 3


def test_modifier_telephone_et_ile(app, login):
    admin, i = login("admin"), creer(app, "x.user", C.DOMAINE)
    page = admin.get(f"/utilisateurs/{i}/modifier").data.decode()
    assert 'name="telephone"' in page and 'name="ile_id"' in page
    # un compte ancien sans île : l'administrateur doit en attribuer une pour enregistrer
    r = admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "telephone": "3332020"})
    assert r.status_code == 400 and "île attribuée est obligatoire" in r.data.decode()
    assert admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "telephone": "3332020", "ile_id": "3"}).status_code == 302
    with app.app_context():
        u = db.session.get(User, i)
        assert (u.telephone, u.ile_id) == ("3332020", 3)
    assert admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "telephone": "", "ile_id": "1"}).status_code == 302
    with app.app_context():
        u = db.session.get(User, i)
        assert (u.telephone, u.ile_id) == (None, 1)
    assert "Numéro de téléphone invalide" in admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "telephone": "xx", "ile_id": "1"}).data.decode()


def test_passer_en_administrateur_efface_l_ile(app, login):
    admin, i = login("admin"), creer(app, "x.user", C.DOMAINE)
    admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "N", "username": "x.user", "ile_id": "2", "role": C.ADMIN})
    with app.app_context():
        u = db.session.get(User, i)
        assert (u.role, u.ile_id) == (C.ADMIN, None)


def test_changer_l_ile_d_un_topographe_rattache_a_des_dossiers_refuse(app, login):
    creer_dossier(login("priv1"))
    admin = login("admin")
    i = uid(app, "priv1")
    with app.app_context():
        db.session.get(User, i).ile_id = 1
        db.session.commit()
    r = admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "priv1", "username": "priv1", "ile_id": "2"})
    assert r.status_code == 400 and "rattaché" in r.data.decode()
    assert admin.post(f"/utilisateurs/{i}/modifier", data={"nom": "priv1", "username": "priv1", "ile_id": "1", "telephone": "3332020"}).status_code == 302


def test_directeur_limite_a_son_ile_ne_gere_que_les_topographes_de_son_ile(app, login):
    with app.app_context():
        User.query.filter_by(username="dir").one().ile_id = 1
        User.query.filter_by(username="priv1").one().ile_id = 1
        User.query.filter_by(username="priv2").one().ile_id = 2
        db.session.commit()
    dire = login("dir")
    page = dire.get("/utilisateurs/").data.decode()
    assert "priv1" in page and "priv2" not in page
    assert dire.get(f"/utilisateurs/{uid(app, 'priv2')}/modifier").status_code == 404
    assert dire.post(f"/utilisateurs/{uid(app, 'priv2')}/supprimer").status_code == 404
    r = dire.post(f"/utilisateurs/{uid(app, 'priv1')}/modifier", data={"nom": "Nouveau", "username": "priv1", "ile_id": "2"})   # ne peut pas déplacer le compte
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(User, uid(app, "priv1")).ile_id == 1
    assert 'name="ile_id"' not in dire.get(f"/utilisateurs/{uid(app, 'priv1')}/modifier").data.decode() or "disabled" in dire.get(f"/utilisateurs/{uid(app, 'priv1')}/modifier").data.decode()


def test_la_liste_signale_les_comptes_sans_ile(app, login):
    page = login("admin").get("/utilisateurs/").data.decode()
    assert "Non attribuée" in page and "Toutes" in page                                           # comptes d'avant la règle et administrateur


# ------------------------------------------------------------------ bouton « Voir » : mouvements d'un compte
def _journal(app, user_id, lignes):
    from datetime import datetime

    from app.models import AuditLog as A

    with app.app_context():
        for quand, action, entite, entite_id in lignes:
            db.session.add(A(user_id=user_id, action=action, entite=entite, entite_id=entite_id, ip="10.0.0.7", nouveau="détail de test", created_at=datetime.strptime(quand, "%Y-%m-%d %H:%M:%S")))
        db.session.commit()


def test_bouton_voir_dans_la_liste(app, login):
    page = login("admin").get("/utilisateurs/").data.decode()
    assert f"/utilisateurs/{uid(app, 'dom')}/activite" in page and "Voir" in page
    assert f"/utilisateurs/{uid(app, 'admin')}/activite" in page                      # y compris son propre compte


def test_page_des_mouvements_et_filtres(app, login):
    i = uid(app, "dom")
    _journal(app, i, [
        ("2026-03-01 08:00:00", "connexion", "user", i),
        ("2026-03-01 14:30:00", "mutation_saisie", "mutation", 3),
        ("2026-03-02 09:15:00", "impression", "dossier", None),
        ("2026-03-05 23:59:30", "deconnexion", "user", i),
    ])
    admin = login("admin")
    page = admin.get(f"/utilisateurs/{i}/activite").data.decode()
    assert "Mouvements de dom" in page and "4 mouvements" in page
    for libelle in ("Connexion", "Mutation saisie", "Document imprimé", "Déconnexion"):                # tout y est, même les connexions
        assert libelle in page
    assert "10.0.0.7" in page and "détail de test" in page
    # filtre par date et heure (bornes incluses à la minute)
    jour = admin.get(f"/utilisateurs/{i}/activite?du=2026-03-01T00:00&au=2026-03-01T23:59").data.decode()
    assert "2 mouvements sur la sélection" in jour and "02/03/2026" not in jour
    heure = admin.get(f"/utilisateurs/{i}/activite?du=2026-03-01T12:00&au=2026-03-02T09:15").data.decode()
    assert "2 mouvements" in heure and "01/03/2026 14:30" in heure and "01/03/2026 08:00" not in heure
    assert "1 mouvement" in admin.get(f"/utilisateurs/{i}/activite?du=2026-03-05T23:59").data.decode()
    # filtre par type
    assert "1 mouvement sur la sélection" in admin.get(f"/utilisateurs/{i}/activite?action=impression").data.decode()
    assert "Aucun mouvement pour cette sélection" in admin.get(f"/utilisateurs/{i}/activite?du=2030-01-01T00:00").data.decode()
    # filtre invalide : ignoré avec un message
    assert "invalide" in admin.get(f"/utilisateurs/{i}/activite?du=zzz").data.decode()


def test_mouvements_pagination_et_compte_sans_mouvement(app, login):
    i = uid(app, "dom")
    assert "Aucun mouvement enregistré pour ce compte" in login("admin").get(f"/utilisateurs/{i}/activite").data.decode()
    _journal(app, i, [(f"2026-04-{1 + n // 24:02d} {n % 24:02d}:00:00", "creation_dossier", "dossier", None) for n in range(120)])
    admin = login("admin")
    p1, p3 = admin.get(f"/utilisateurs/{i}/activite").data.decode(), admin.get(f"/utilisateurs/{i}/activite?page=3").data.decode()
    assert p1.count("<td>Dossier créé</td>") == 50 and p3.count("<td>Dossier créé</td>") == 20 and "120 mouvements" in p1 and "page=2" in p1


def test_mouvements_d_un_seul_compte(app, login):
    _journal(app, uid(app, "dir"), [("2026-03-01 08:00:00", "impression", "dossier", None)])
    assert "Document imprimé" not in login("admin").get(f"/utilisateurs/{uid(app, 'dom')}/activite").data.decode()


def test_acces_a_la_page_des_mouvements(app, login):
    i = uid(app, "priv2")
    assert login("admin").get(f"/utilisateurs/{i}/activite").status_code == 200
    assert login("dir").get(f"/utilisateurs/{i}/activite").status_code == 200                # topographe privé : gérable par le Directeur
    assert login("dir").get(f"/utilisateurs/{uid(app, 'dom')}/activite").status_code == 200   # le Directeur voit les mouvements des comptes de son île
    assert login("dir").get(f"/utilisateurs/{uid(app, 'admin')}/activite").status_code == 404   # jamais ceux d'un administrateur
    for role in ("dom", "pub", "priv1"):
        assert login(role).get(f"/utilisateurs/{i}/activite").status_code == 403
    assert app.test_client().get(f"/utilisateurs/{i}/activite").status_code == 302


def test_directeur_limite_a_son_ile_ne_voit_que_les_comptes_de_son_ile(app, login):
    with app.app_context():
        User.query.filter_by(username="dir").one().ile_id = 1
        User.query.filter_by(username="priv1").one().ile_id = 1
        User.query.filter_by(username="priv2").one().ile_id = 2
        db.session.commit()
    dire = login("dir")
    assert dire.get(f"/utilisateurs/{uid(app, 'priv1')}/activite").status_code == 200
    assert dire.get(f"/utilisateurs/{uid(app, 'priv2')}/activite").status_code == 404


def test_lien_vers_le_dossier_seulement_si_visible(app, login):
    from conftest import creer_dossier

    did = creer_dossier(login("pub"))
    pub_id = uid(app, "pub")
    _journal(app, pub_id, [("2026-03-01 08:00:00", "creation_dossier", "dossier", did)])
    assert f"/dossiers/{did}" in login("admin").get(f"/utilisateurs/{pub_id}/activite").data.decode()


# ------------------------------------------------------------------ le Directeur voit tous les comptes de son île (sauf les administrateurs)
def _iles(app, **iles):
    with app.app_context():
        for username, ile in iles.items():
            User.query.filter_by(username=username).one().ile_id = ile
        db.session.commit()


def test_directeur_voit_tous_les_comptes_de_son_ile_sauf_les_administrateurs(app, login):
    creer(app, "dir.autre", C.DIRECTEUR)
    _iles(app, dir=1, dom=1, pub=1, priv1=1, priv2=2, **{"dir.autre": 1})
    dire = login("dir")
    page = dire.get("/utilisateurs/").data.decode()
    for visible in ("dom", "pub", "priv1", "dir.autre", ">dir<"):
        assert visible in page, visible                                       # Domaine, topographes, autre Directeur et lui-même
    assert "priv2" not in page                                                # autre île
    assert ">admin<" not in page                                              # jamais les administrateurs
    for onglet in ("Directeurs", "Topo Public", "Topo Prive", "Domaine"):
        assert onglet in page
    assert "role=admin" not in page                                           # pas d'onglet « Admin »
    assert ">admin<" not in dire.get("/utilisateurs/?role=admin").data.decode()
    assert "dom" in dire.get("/utilisateurs/?role=domaine").data.decode()


def test_directeur_ne_peut_agir_que_sur_les_topographes_prives(app, login):
    creer(app, "dir.autre", C.DIRECTEUR)
    _iles(app, dir=1, dom=1, pub=1, priv1=1, **{"dir.autre": 1})
    dire = login("dir")
    page = dire.get("/utilisateurs/").data.decode()
    assert f"/utilisateurs/{uid(app, 'priv1')}/modifier" in page and f"/utilisateurs/{uid(app, 'priv1')}/supprimer" in page
    for autre in ("dom", "pub", "dir.autre", "dir"):                               # les autres comptes : « Voir » seulement
        i = uid(app, autre)
        assert f"/utilisateurs/{i}/activite" in page
        assert f"/utilisateurs/{i}/modifier" not in page and f"/utilisateurs/{i}/supprimer" not in page and f"/utilisateurs/{i}/activer" not in page
        assert dire.get(f"/utilisateurs/{i}/modifier").status_code == 404
        assert dire.post(f"/utilisateurs/{i}/activer").status_code == 404
        assert dire.post(f"/utilisateurs/{i}/supprimer").status_code == 404
        assert dire.get(f"/utilisateurs/{i}/activite").status_code == 200
    with app.app_context():
        assert User.query.filter_by(username="dom").one().actif


def test_directeur_ne_voit_pas_les_comptes_d_une_autre_ile(app, login):
    _iles(app, dir=1, dom=2, pub=2)
    dire = login("dir")
    page = dire.get("/utilisateurs/").data.decode()
    assert ">dom<" not in page and ">pub<" not in page
    assert dire.get(f"/utilisateurs/{uid(app, 'dom')}/activite").status_code == 404


def test_directeur_sans_ile_voit_tous_les_comptes_non_administrateurs(app, login):
    _iles(app, dom=1, pub=2, priv1=3)
    page = login("dir").get("/utilisateurs/").data.decode()
    for visible in ("dom", "pub", "priv1", "priv2"):
        assert visible in page
    assert ">admin<" not in page


def test_l_administrateur_voit_et_gere_tout(app, login):
    page = login("admin").get("/utilisateurs/").data.decode()
    assert "role=admin" in page and "Admin <span" in page and ">admin<" in page
    for u in ("dom", "pub", "priv1", "dir"):
        assert f"/utilisateurs/{uid(app, u)}/modifier" in page


def test_bouton_modifier_le_compte_dans_les_mouvements_reserve_a_l_administrateur(app, login):
    i = uid(app, "priv1")
    assert f"/utilisateurs/{i}/modifier" in login("admin").get(f"/utilisateurs/{i}/activite").data.decode()
    page = login("dir").get(f"/utilisateurs/{i}/activite").data.decode()
    assert "Modifier le compte" not in page and f"/utilisateurs/{i}/modifier" not in page          # pas pour un compte Directeur
    assert "Mouvements de" in page                                                                  # la page reste consultable


def test_tous_les_mouvements_du_journal_ont_un_libelle_francais(app, login):
    import glob
    import re

    from app import activite

    codes = set()
    for f in glob.glob("app/*.py"):
        t = open(f).read()
        codes |= set(re.findall(r'audit\(\s*f?"([a-z_]+)"', t)) | set(re.findall(r'"([a-z_]+_saisie)"', t)) | set(re.findall(r'audit\(\s*\n?\s*"([a-z_]+)"', t))
    codes |= {"trace_ajoute", "trace_remplace"}
    codes -= {"erreur_saisie"}                                                                # type de problème, pas un mouvement du journal
    manquants = sorted(c for c in codes if c not in activite.ACTIONS and c not in ("connexion_echec",))
    assert manquants == []


# ------------------------------------------------------------------ premier administrateur créé depuis l'environnement (hébergement sans terminal)
def test_creation_de_l_admin_depuis_l_environnement(app, monkeypatch):
    from app.models import User

    runner = app.test_cli_runner()
    with app.app_context():
        User.query.filter_by(role=C.ADMIN).delete()
        db.session.commit()
    r = runner.invoke(args=["create-admin-env"])
    assert r.exit_code == 0 and "Aucun administrateur à créer" in r.output                       # sans variables : rien

    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "Chef.Admin")
    monkeypatch.setenv("INITIAL_ADMIN_NOM", "Chef de service")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "court")
    r = runner.invoke(args=["create-admin-env"])
    assert r.exit_code != 0 and "trop court" in r.output
    with app.app_context():
        assert User.query.filter_by(role=C.ADMIN).count() == 0

    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "un-mot-de-passe-solide")
    r = runner.invoke(args=["create-admin-env"])
    assert r.exit_code == 0 and "Administrateur chef.admin créé" in r.output
    with app.app_context():
        u = User.query.filter_by(username="chef.admin").one()
        assert u.role == C.ADMIN and u.nom == "Chef de service" and u.check_password("un-mot-de-passe-solide") and not u.doit_changer_mdp
    assert app.test_client().post("/connexion", data={"username": "chef.admin", "password": "un-mot-de-passe-solide"}).status_code == 302

    # idempotent : un administrateur existe, la commande ne touche à rien (même avec un autre mot de passe)
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "autre-mot-de-passe-solide")
    r = runner.invoke(args=["create-admin-env"])
    assert r.exit_code == 0 and "existe déjà" in r.output
    with app.app_context():
        assert User.query.filter_by(username="chef.admin").one().check_password("un-mot-de-passe-solide")


def test_base_sans_tables_donne_un_message_clair(app, monkeypatch):
    runner = app.test_cli_runner()
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin2")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "un-mot-de-passe-solide")
    with app.app_context():
        db.drop_all()
    r = runner.invoke(args=["create-admin-env"])
    assert r.exit_code != 0 and "pas de tables" in r.output and "db upgrade" in r.output


def test_sur_render_sans_base_postgresql_on_refuse_sqlite(monkeypatch):
    import pytest

    import config

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config._database_uri()
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
    assert config._database_uri().startswith("postgresql://")
    monkeypatch.delenv("RENDER")
    monkeypatch.delenv("DATABASE_URL")
    assert config._database_uri().startswith("sqlite")                    # en local : SQLite, comme avant
