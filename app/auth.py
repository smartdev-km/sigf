import re
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .audit import audit
from .extensions import db
from .models import User, maintenant

bp = Blueprint("auth", __name__)

TELEPHONE_RE = re.compile(r"^[+(]{0,2}[0-9][0-9 .()\-]{4,23}[0-9)]$")


def roles_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)

        return wrapper

    return deco


def _url_sure(cible):
    """N'accepte que les redirections internes."""
    if not cible:
        return False
    p = urlparse(cible)
    return not p.netloc and not p.scheme and cible.startswith("/") and not cible.startswith("//")


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        mdp = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and user.actif and user.check_password(mdp):
            login_user(user)
            user.derniere_connexion = maintenant()
            audit("connexion", "user", user.id, user=user)
            db.session.commit()
            cible = request.args.get("next")
            return redirect(cible if _url_sure(cible) else url_for("main.bienvenue"))
        audit("connexion_echec", "user", user.id if user else None, nouveau=username)
        db.session.commit()
        flash("Identifiant ou mot de passe incorrect.", "danger")
    return render_template("login.html")


@bp.route("/deconnexion", methods=["POST"])
@login_required
def logout():
    audit("deconnexion", "user", current_user.id)
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/mot-de-passe", methods=["GET", "POST"])
@login_required
def changer_mdp():
    minimum = current_app.config["MDP_LONGUEUR_MIN"]
    if request.method == "POST":
        ancien = request.form.get("ancien") or ""
        nouveau = request.form.get("nouveau") or ""
        confirmation = request.form.get("confirmation") or ""
        if not current_user.check_password(ancien):
            flash("Ancien mot de passe incorrect.", "danger")
        elif len(nouveau) < minimum:
            flash(f"Le nouveau mot de passe doit contenir au moins {minimum} caractères.", "danger")
        elif nouveau != confirmation:
            flash("La confirmation ne correspond pas.", "danger")
        elif nouveau == ancien:
            flash("Le nouveau mot de passe doit être différent de l'ancien.", "danger")
        else:
            current_user.set_password(nouveau)
            current_user.doit_changer_mdp = False
            audit("changement_mdp", "user", current_user.id)
            db.session.commit()
            flash("Mot de passe modifié.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("changer_mdp.html", minimum=minimum)


def _lire_periode(args):
    """Filtre de « Mes dernières actions » : (du, au, erreur). Les heures sont celles affichées dans l'application."""
    def lire(cle):
        brut = (args.get(cle) or "").strip()
        if not brut:
            return None, False
        try:
            return datetime.strptime(brut, "%Y-%m-%dT%H:%M"), False
        except ValueError:
            return None, True

    du, e1 = lire("du")
    au, e2 = lire("au")
    if e1 or e2:
        return None, None, "Date ou heure invalide : le filtre n'a pas été appliqué."
    if du and au and du > au:
        return None, None, "La date de début doit précéder la date de fin : le filtre n'a pas été appliqué."
    return du, au, None


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profil():
    """Mon compte : nom, identifiant et mot de passe. Le type de compte ne se change pas ici."""
    from .users import USERNAME_RE

    minimum = current_app.config["MDP_LONGUEUR_MIN"]
    f = request.form
    erreur_infos = erreur_mdp = False
    if request.method == "POST" and f.get("action") == "infos":
        nom = " ".join((f.get("nom") or "").split())
        username = (f.get("username") or "").strip().lower()
        telephone = " ".join((f.get("telephone") or "").split()) or None
        erreurs = []
        if telephone and not TELEPHONE_RE.match(telephone):
            erreurs.append("Numéro de téléphone invalide : chiffres, espaces, + - . ( ) uniquement, 6 à 25 caractères (ex. +269 333 20 20).")
        if not nom:
            erreurs.append("Le nom est obligatoire.")
        elif len(nom) > 120:
            erreurs.append("Le nom est trop long (120 caractères maximum).")
        if not USERNAME_RE.match(username):
            erreurs.append("Identifiant invalide : 3 à 40 caractères (lettres, chiffres, point, tiret, underscore).")
        elif username != current_user.username and User.query.filter(User.username == username, User.id != current_user.id).first():
            erreurs.append("Cet identifiant est déjà utilisé.")
        if erreurs:
            for e in erreurs:
                flash(e, "danger")
            erreur_infos = True
        elif (nom, username, telephone) == (current_user.nom, current_user.username, current_user.telephone):
            flash("Aucune modification.", "info")
            return redirect(url_for("auth.profil"))
        else:
            avant = f"{current_user.nom} / {current_user.username} / {current_user.telephone or '—'}"
            ancien_username = current_user.username
            current_user.nom, current_user.username, current_user.telephone = nom, username, telephone
            audit("modification_profil", "user", current_user.id, ancien=avant, nouveau=f"{nom} / {username} / {telephone or '—'}")
            db.session.commit()
            flash("Informations enregistrées." + (" Utilisez le nouvel identifiant à votre prochaine connexion." if ancien_username != username else ""), "success")
            return redirect(url_for("auth.profil"))
    elif request.method == "POST" and f.get("action") == "mdp":
        ancien, nouveau, confirmation = f.get("ancien") or "", f.get("nouveau") or "", f.get("confirmation") or ""
        if not current_user.check_password(ancien):
            flash("Ancien mot de passe incorrect.", "danger")
        elif len(nouveau) < minimum:
            flash(f"Le nouveau mot de passe doit contenir au moins {minimum} caractères.", "danger")
        elif nouveau != confirmation:
            flash("La confirmation ne correspond pas.", "danger")
        elif nouveau == ancien:
            flash("Le nouveau mot de passe doit être différent de l'ancien.", "danger")
        else:
            current_user.set_password(nouveau)
            current_user.doit_changer_mdp = False
            audit("changement_mdp", "user", current_user.id)
            db.session.commit()
            flash("Mot de passe modifié.", "success")
            return redirect(url_for("auth.profil"))
        erreur_mdp = True
    from . import activite

    return render_template(
        "profil.html",
        minimum=minimum,
        f=f if erreur_infos else {},
        erreur_infos=erreur_infos,
        erreur_mdp=erreur_mdp,
        chiffres=activite.chiffres(current_user),
        connexions=activite.connexions(current_user),
    ), 400 if (erreur_infos or erreur_mdp) else 200
