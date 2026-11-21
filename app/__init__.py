import os

import click
from flask import Flask, abort, redirect, render_template, request, url_for
from flask_login import current_user

from . import constants as C
from . import workflow as W
from .extensions import csrf, db, login_manager, migrate


def create_app(overrides=None):
    from config import Config

    Config.verifier()
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)

    if os.environ.get("RENDER"):
        # Derrière le proxy de Render : bon schéma (https) et bonne adresse dans les QR et les redirections
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    login_manager.init_app(app)
    csrf.init_app(app)

    from . import arbre, auth, blocages, carte, signature, terrain, titrage, corrections, documents, models, mutations, partages, proprietaires, users, verification, views  # noqa: F401

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(documents.bp)
    app.register_blueprint(verification.bp)
    app.register_blueprint(mutations.bp)
    app.register_blueprint(carte.bp)
    app.register_blueprint(partages.bp)
    app.register_blueprint(corrections.bp)
    app.register_blueprint(proprietaires.bp)
    app.register_blueprint(blocages.bp)
    app.register_blueprint(signature.bp)
    app.register_blueprint(terrain.bp)
    app.register_blueprint(titrage.bp)

    @app.route("/favicon.ico")
    def favicon():
        """Icône « SIGF » demandée d'office par les navigateurs (publique : elle sert aussi sur la page de connexion)."""
        from flask import send_from_directory

        return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon", max_age=86400)

    @app.before_request
    def forcer_changement_mdp():
        if (
            current_user.is_authenticated
            and current_user.doit_changer_mdp
            and request.endpoint not in ("auth.changer_mdp", "auth.logout", "static", "favicon")
        ):
            return redirect(url_for("auth.changer_mdp"))

    # La Fonction publique n'accède qu'à la page Titrage (dossiers entrés en titrage : étapes, scans), à la vérification publique et à son profil.
    ENDPOINTS_FONCTION_PUBLIQUE = {
        "main.bienvenue", "titrage.page", "titrage.fiche", "titrage.route_avancer", "titrage.route_retour", "titrage.route_bloquer", "blocages.lever_route",
        "documents.voir", "main.exemplaire_pdf", "auth.profil", "auth.logout", "auth.changer_mdp", "static", "favicon",
    }

    @app.before_request
    def restreindre_fonction_publique():
        if not (current_user.is_authenticated and current_user.role == C.FONCTION_PUBLIQUE):
            return None
        ep = request.endpoint
        if ep is None or ep in ENDPOINTS_FONCTION_PUBLIQUE or (ep.startswith("verif.") and ep != "verif.revoquer"):
            return None
        if ep == "main.dashboard":
            return redirect(url_for("main.bienvenue"))
        abort(403)

    @app.context_processor
    def contexte():
        return {
            "C": C,
            "STATUTS": C.STATUTS,
            "STATUT_COULEUR": C.STATUT_COULEUR,
            "statut_affiche": W.statut_affiche,
            "peut_creer_dossier": W.peut_creer_dossier,
            "FILIATION": {"libelles": arbre.LIBELLES, "passe": arbre.PASSE, "origine": arbre.ORIGINE, "icones": arbre.ICONES},
            "GOOGLE_CLE": app.config.get("GOOGLE_MAPS_API_KEY"),
            # évaluée seulement si le modèle l'appelle (une requête, réservée au Directeur)
            "nb_mutations_attente": lambda: mutations.nb_en_attente(current_user),
            "nb_modifs_attente": lambda: __import__("app.personnes", fromlist=["x"]).demandes_en_attente(current_user).count(),
            "nb_titrage_a_traiter": lambda: titrage.nb_a_traiter(current_user),
            "nb_blocages_actifs": lambda: __import__("app.blocages", fromlist=["nb_actifs"]).nb_actifs(current_user),
            "peut_importer_ancien": W.peut_importer_ancien,
            "formater_part": mutations.formater_part,
            "TYPES_DOCUMENT": C.TYPES_DOCUMENT,
            "PROBLEMES_GROUPES": C.PROBLEMES_GROUPES,
            "TYPES_FIXES": [t for t in C.TYPES_DOCUMENT if t != C.TYPE_AUTRE],
        }

    @app.template_filter("dt")
    def format_dt(valeur):
        return valeur.strftime("%d/%m/%Y %H:%M") if valeur else "—"

    @app.errorhandler(403)
    def interdit(_):
        return render_template("erreur.html", code=403, message="Accès refusé."), 403

    @app.errorhandler(413)
    def trop_gros(_):
        return render_template("erreur.html", code=413, message="Fichiers trop volumineux."), 413

    @app.errorhandler(404)
    def introuvable(_):
        return render_template("erreur.html", code=404, message="Page introuvable."), 404

    @app.after_request
    def entetes_securite(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    _enregistrer_commandes(app)
    return app


def _enregistrer_commandes(app):
    @app.cli.command("init-data")
    def init_data():
        """Crée les données de référence (îles). Idempotent."""
        from .models import Ile

        for nom in C.ILES:
            if not Ile.query.filter_by(nom=nom).first():
                db.session.add(Ile(nom=nom))
        db.session.commit()
        click.echo("Données de référence prêtes.")

    @app.cli.command("seed-demo")
    @click.option("--ajouter", is_flag=True, help="Ajoute les exemples même s'ils existent déjà.")
    def seed_demo(ajouter):
        """Crée des dossiers FICTIFS (nouveaux et anciens) pour la démonstration."""
        from . import demo

        if os.environ.get("RENDER"):
            raise click.ClickException("Refusé : on ne crée jamais de données fictives en production.")
        if demo.deja_present() and not ajouter:
            raise click.ClickException(f"{demo.deja_present()} dossier(s) de démonstration existent déjà. Utilisez --ajouter pour en créer d'autres.")
        try:
            # les documents générés à la certification ont un QR code : il lui faut l'adresse du site (pas de requête web en ligne de commande)
            with app.test_request_context("/", base_url=app.config.get("PUBLIC_BASE_URL") or "http://127.0.0.1:5003"):
                resume = demo.semer()
        except demo.DemoError as e:
            raise click.ClickException(str(e)) from None
        for ligne in resume:
            d, texte = ligne[0], ligne[1]
            click.echo(f"{d.numero}  {d.proprietaire_label[:28]:28}  {texte}")
            for type_doc, code in (ligne[2].items() if len(ligne) > 2 else []):
                click.echo(f"{'':16}vérification {type_doc}: /verifier/{code}")
        click.echo(f"{len(resume)} dossier(s) de démonstration créés.")

    @app.cli.command("create-admin-env")
    def create_admin_env():
        """Crée le premier administrateur depuis les variables INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_NOM et INITIAL_ADMIN_PASSWORD
        (pour les hébergements sans terminal). Ne fait rien si les variables sont absentes ou si un administrateur existe déjà :
        la commande peut donc rester dans la commande de build sans danger ; supprimez ensuite INITIAL_ADMIN_PASSWORD."""
        from .models import User

        from sqlalchemy.exc import OperationalError, ProgrammingError

        username = (os.environ.get("INITIAL_ADMIN_USERNAME") or "").strip().lower()
        nom = (os.environ.get("INITIAL_ADMIN_NOM") or "Administrateur").strip()
        password = os.environ.get("INITIAL_ADMIN_PASSWORD") or ""
        if not username or not password:
            click.echo("Aucun administrateur à créer (INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD absents).")
            return
        try:
            admin_existant = User.query.filter_by(role=C.ADMIN).first()
        except (OperationalError, ProgrammingError):
            db.session.rollback()
            raise click.ClickException(
                "La base n'a pas de tables : lancez d'abord « flask --app wsgi db upgrade » (avant create-admin-env dans la commande de build) "
                "et vérifiez que DATABASE_URL pointe vers votre base PostgreSQL."
            ) from None
        if admin_existant:
            click.echo("Un administrateur existe déjà : rien à faire (vous pouvez supprimer INITIAL_ADMIN_PASSWORD).")
            return
        minimum = app.config["MDP_LONGUEUR_MIN"]
        if len(password) < minimum:
            raise click.ClickException(f"INITIAL_ADMIN_PASSWORD est trop court (minimum {minimum} caractères).")
        if User.query.filter_by(username=username).first():
            raise click.ClickException("Cet identifiant existe déjà.")
        u = User(nom=nom, username=username, role=C.ADMIN, doit_changer_mdp=False)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Administrateur {username} créé. Supprimez maintenant la variable INITIAL_ADMIN_PASSWORD.")

    @app.cli.command("verifier-stockage")
    def verifier_stockage():
        """Vérifie le stockage des fichiers : écrit, relit puis supprime un petit fichier d'essai (utile après avoir renseigné les variables S3_*)."""
        import uuid

        from .storage import StorageError, get_storage

        try:
            stockage = get_storage()
            nom = type(stockage).__name__
            cle = f"_verification/{uuid.uuid4().hex}.txt"
            contenu = b"SIGF : essai de stockage"
            stockage.put(cle, contenu, "text/plain")
            relu = stockage.lire(cle)
            stockage.delete(cle)
        except StorageError as e:
            raise click.ClickException(f"Stockage inutilisable : {e}") from None
        except Exception as e:  # noqa: BLE001 - mauvaise clé, mauvais point d'accès, bucket introuvable…
            raise click.ClickException(f"Stockage inutilisable ({type(e).__name__}) : {e}") from None
        if relu != contenu:
            raise click.ClickException("Le fichier relu est différent du fichier écrit.")
        cible = app.config.get("S3_BUCKET") and f"bucket « {app.config['S3_BUCKET']} » ({app.config.get('S3_ENDPOINT_URL') or 'AWS'})"
        click.echo(f"Stockage OK : {nom}" + (f" · {cible}" if cible else " · disque local") + " — écriture, lecture et suppression réussies.")

    @app.cli.command("generer-documents")
    @click.option("--utilisateur", required=True, help="Identifiant du compte (Directeur) au nom duquel les documents sont émis.")
    def generer_documents(utilisateur):
        """Génère les documents manquants des nouveaux dossiers DÉJÀ certifiés (rattrapage, idempotent)."""
        from . import generation
        from .models import Dossier, User

        u = User.query.filter_by(username=utilisateur.strip().lower()).first()
        if u is None:
            raise click.ClickException("Compte introuvable.")
        base = app.config.get("PUBLIC_BASE_URL") or "http://127.0.0.1:5003"
        traites = nb = 0
        with app.test_request_context("/", base_url=base):          # les QR codes ont besoin de l'adresse publique du site
            for d in Dossier.query.filter(Dossier.origine == C.ORIGINE_NOUVEAU, Dossier.statut == C.CERTIFIE).order_by(Dossier.id).all():
                if not generation.manquants(d):
                    continue
                generes, echecs = generation.generer_documents(d, u)
                db.session.commit()
                traites += 1
                nb += len(generes)
                click.echo(f"{d.numero} : {len(generes)} document(s) généré(s)" + (f", ÉCHEC : {', '.join(echecs)}" if echecs else ""))
        click.echo(f"{traites} dossier(s) traité(s), {nb} document(s) généré(s).")

    @app.cli.command("create-admin")
    @click.option("--username", prompt="Identifiant")
    @click.option("--nom", prompt=True)
    @click.password_option()
    def create_admin(username, nom, password):
        """Crée un compte Administrateur."""
        from .models import User

        minimum = app.config["MDP_LONGUEUR_MIN"]
        if len(password) < minimum:
            raise click.ClickException(f"Mot de passe trop court (minimum {minimum}).")
        username = username.strip().lower()
        if User.query.filter_by(username=username).first():
            raise click.ClickException("Cet identifiant existe déjà.")
        u = User(nom=nom, username=username, role=C.ADMIN, doit_changer_mdp=False)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Administrateur {username} créé.")
