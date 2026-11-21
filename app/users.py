import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from . import constants as C
from . import workflow as W
from .audit import audit
from .auth import TELEPHONE_RE, _lire_periode, roles_required
from .extensions import db
from .models import Dossier, Ile, User

bp = Blueprint("users", __name__, url_prefix="/utilisateurs")

PLURIELS = {C.ADMIN: "Admin", C.DIRECTEUR: "Directeurs", C.TOPO_PUBLIC: "Topo Public", C.TOPO_PRIVE: "Topo Prive", C.DOMAINE: "Domaine", C.FONCTION_PUBLIQUE: "Fonct Pub"}
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def _roles_creables():
    """L'admin crée tous les rôles ; le Directeur uniquement des topographes privés."""
    if current_user.role == C.ADMIN:
        return list(C.ROLES)
    return [C.TOPO_PRIVE]


def _visibles():
    """Comptes que l'utilisateur peut consulter : tous (admin) ; pour un Directeur, tous les comptes de son île sauf les administrateurs
    (tous les comptes non administrateurs s'il n'est limité à aucune île)."""
    q = User.query
    if current_user.role == C.DIRECTEUR:
        q = q.filter(User.role != C.ADMIN)
        if W.ile_de(current_user):
            q = q.filter(User.ile_id == W.ile_de(current_user))
    return q


def _gerables():
    """Comptes que l'utilisateur peut modifier, désactiver ou supprimer : tous (admin) ; un Directeur ne gère que les topographes
    privés qu'il voit. Les autres comptes qu'il voit (Domaine, topographes publics, autres Directeurs) sont en lecture seule."""
    q = _visibles()
    if current_user.role == C.DIRECTEUR:
        q = q.filter(User.role == C.TOPO_PRIVE)
    return q


def _telephone(f, erreurs):
    valeur = " ".join((f.get("telephone") or "").split()) or None
    if valeur and not TELEPHONE_RE.match(valeur):
        erreurs.append("Numéro de téléphone invalide : chiffres, espaces, + - . ( ) uniquement, 6 à 25 caractères (ex. +269 333 20 20).")
    return valeur


def _agrement(f, role, erreurs):
    """N° d'agrément : seulement pour les topographes (il est imprimé sur leurs plans croquis)."""
    valeur = " ".join((f.get("agrement") or "").split()) or None
    if role not in (C.TOPO_PUBLIC, C.TOPO_PRIVE):
        return None
    if valeur and len(valeur) > 60:
        erreurs.append("Le n° d'agrément est trop long (60 caractères maximum).")
    return valeur


def _ile_imposee():
    """Île imposée aux comptes créés par l'utilisateur courant (Directeur limité à son île), sinon None."""
    return W.ile_de(current_user) if current_user.role == C.DIRECTEUR else None


def _ile_choisie(f, role, erreurs):
    """Île attribuée au compte. Obligatoire pour tout compte sauf l'administrateur, qui n'est jamais limité à une île."""
    if role == C.ADMIN:
        return None
    imposee = _ile_imposee()
    if imposee:
        return imposee
    ile = db.session.get(Ile, f.get("ile_id", type=int)) if f.get("ile_id", type=int) else None
    if ile is None:
        erreurs.append("L'île attribuée est obligatoire (sauf pour un administrateur) : le compte ne verra que les informations de son île.")
        return None
    return ile.id


@bp.route("/")
@roles_required(C.ADMIN, C.DIRECTEUR)
def liste():
    """Un onglet par type de compte (avec son effectif) ; « ?role=… » filtre."""
    comptes = {r: n for r, n in db.session.query(User.role, db.func.count(User.id)).filter(User.id.in_(_visibles().with_entities(User.id))).group_by(User.role)}
    roles_visibles = [r for r in C.ROLES if current_user.role == C.ADMIN or r != C.ADMIN]
    role = request.args.get("role")
    role = role if role in roles_visibles else None
    q = _visibles()
    if role:
        q = q.filter(User.role == role)
    return render_template(
        "users/liste.html",
        users=q.order_by(User.role, User.nom).all(),
        gerables={x.id for x in _gerables().with_entities(User.id)} if current_user.role != C.ADMIN else None,   # None : tous
        onglets=[(r, PLURIELS[r], comptes.get(r, 0)) for r in roles_visibles],
        total=sum(comptes.values()),
        role=role,
    )


def _contexte_formulaire(roles, f, minimum):
    imposee = _ile_imposee()
    return {
        "roles": {r: C.ROLES[r] for r in roles},
        "f": f,
        "minimum": minimum,
        "iles": W.iles_visibles(current_user) if not imposee else Ile.query.filter(Ile.id == imposee).all(),
        "ile_imposee": db.session.get(Ile, imposee) if imposee else None,
    }


@bp.route("/nouveau", methods=["GET", "POST"])
@roles_required(C.ADMIN, C.DIRECTEUR)
def nouveau():
    roles = _roles_creables()
    minimum = current_app.config["MDP_LONGUEUR_MIN"]
    if request.method == "POST":
        f = request.form
        nom = (f.get("nom") or "").strip()
        username = (f.get("username") or "").strip().lower()
        role = f.get("role") or (roles[0] if len(roles) == 1 else "")
        mdp = f.get("password") or ""
        erreurs = []
        if not nom:
            erreurs.append("Le nom est obligatoire.")
        elif len(nom) > 120:
            erreurs.append("Le nom est trop long (120 caractères maximum).")
        if not USERNAME_RE.match(username):
            erreurs.append("Identifiant invalide : 3 à 40 caractères (lettres, chiffres, point, tiret, underscore).")
        elif User.query.filter_by(username=username).first():
            erreurs.append("Cet identifiant est déjà utilisé.")
        if role not in roles:
            erreurs.append("Rôle non autorisé.")
        telephone = _telephone(f, erreurs)
        agrement = _agrement(f, role, erreurs)
        ile_id = _ile_choisie(f, role, erreurs) if role in roles else None
        if len(mdp) < minimum:
            erreurs.append(f"Le mot de passe provisoire doit contenir au moins {minimum} caractères.")
        if erreurs:
            for e in erreurs:
                flash(e, "danger")
            return render_template("users/nouveau.html", **_contexte_formulaire(roles, f, minimum)), 400
        u = User(nom=nom, username=username, role=role, telephone=telephone, agrement=agrement, ile_id=ile_id, cree_par_id=current_user.id, doit_changer_mdp=True)
        u.set_password(mdp)
        db.session.add(u)
        db.session.flush()
        audit("creation_utilisateur", "user", u.id, nouveau=f"{username} ({role})" + (f", île {u.ile.nom}" if u.ile else ""))
        db.session.commit()
        flash(f"Compte créé pour {nom}. Il devra changer son mot de passe à la première connexion.", "success")
        return redirect(url_for("users.liste"))
    return render_template("users/nouveau.html", **_contexte_formulaire(roles, {}, minimum))


@bp.route("/<int:user_id>/activer", methods=["POST"])
@roles_required(C.ADMIN, C.DIRECTEUR)
def basculer(user_id):
    u = _gerables().filter(User.id == user_id).first()
    if u is None:
        abort(404)
    if u.id == current_user.id:
        flash("Vous ne pouvez pas désactiver votre propre compte.", "danger")
    else:
        u.actif = not u.actif
        audit("activation_utilisateur" if u.actif else "desactivation_utilisateur", "user", u.id)
        db.session.commit()
        flash(f"Compte {'activé' if u.actif else 'désactivé'}.", "success")
    return redirect(url_for("users.liste"))


# --------------------------------------------------------------------------
# Modifier / supprimer
# --------------------------------------------------------------------------
def _gere(user_id):
    u = _gerables().filter(User.id == user_id).first()
    if u is None:
        abort(404)
    return u


def _admins_actifs():
    return User.query.filter(User.role == C.ADMIN, User.actif.is_(True)).count()


def _references(u):
    """Nombre de lignes (hors comptes créés par lui) qui pointent vers ce compte : dossiers, mutations, journal d'audit…"""
    total = 0
    for table in db.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name != "users" or (table.name == "users" and fk.parent.name == "cree_par_id"):
                continue
            total += db.session.execute(db.select(db.func.count()).select_from(table).where(fk.parent == u.id)).scalar()
    return total


def _contexte_modification(u, f, minimum, peut_changer_role, peut_changer_ile):
    return {
        "u": u,
        "roles": C.ROLES if peut_changer_role else {u.role: C.ROLES[u.role]},
        "f": f,
        "minimum": minimum,
        "peut_changer_role": peut_changer_role,
        "peut_changer_ile": peut_changer_ile,
        "iles": Ile.query.order_by(Ile.id).all(),
    }


@bp.route("/<int:user_id>/modifier", methods=["GET", "POST"])
@roles_required(C.ADMIN, C.DIRECTEUR)
def modifier(user_id):
    u = _gere(user_id)
    peut_changer_role = current_user.role == C.ADMIN and u.id != current_user.id
    # l'île se change par l'administrateur, ou par un Directeur qui n'est lui-même limité à aucune île
    peut_changer_ile = (current_user.role == C.ADMIN or not W.ile_de(current_user)) and u.id != current_user.id
    minimum = current_app.config["MDP_LONGUEUR_MIN"]
    if request.method == "POST":
        f = request.form
        nom = (f.get("nom") or "").strip()
        username = (f.get("username") or "").strip().lower()
        role = f.get("role") or u.role
        mdp = f.get("password") or ""
        erreurs = []
        if not nom:
            erreurs.append("Le nom est obligatoire.")
        elif len(nom) > 120:
            erreurs.append("Le nom est trop long (120 caractères maximum).")
        if not USERNAME_RE.match(username):
            erreurs.append("Identifiant invalide : 3 à 40 caractères (lettres, chiffres, point, tiret, underscore).")
        elif User.query.filter(User.username == username, User.id != u.id).first():
            erreurs.append("Cet identifiant est déjà utilisé.")
        if role != u.role:
            if not peut_changer_role or role not in C.ROLES:
                erreurs.append("Changement de rôle non autorisé.")
            elif u.role == C.ADMIN and u.actif and _admins_actifs() <= 1:
                erreurs.append("C'est le dernier administrateur actif : son rôle ne peut pas changer.")
            elif Dossier.query.filter(Dossier.topographe_id == u.id).first():
                erreurs.append("Ce topographe est rattaché à des dossiers : son rôle ne peut plus changer (désactivez le compte et créez-en un autre).")
        telephone = _telephone(f, erreurs)
        agrement = _agrement(f, role, erreurs)
        if role == C.ADMIN:
            ile_id = None
        elif peut_changer_ile:
            ile_id = _ile_choisie(f, role, erreurs)
        else:
            ile_id = u.ile_id
            if ile_id is None and role != C.ADMIN:
                erreurs.append("Ce compte n'a pas d'île attribuée : seul un administrateur peut la lui attribuer.")
        if ile_id != u.ile_id and u.role == C.TOPO_PRIVE and Dossier.query.filter(Dossier.topographe_id == u.id).first():
            erreurs.append("Ce topographe est rattaché à des dossiers de son île : elle ne peut plus changer (désactivez le compte et créez-en un autre).")
        if mdp and len(mdp) < minimum:
            erreurs.append(f"Le nouveau mot de passe doit contenir au moins {minimum} caractères.")
        if erreurs:
            for e in erreurs:
                flash(e, "danger")
            return render_template("users/modifier.html", **_contexte_modification(u, f, minimum, peut_changer_role, peut_changer_ile)), 400
        avant = f"{u.nom} / {u.username} / {u.role} / {u.telephone or '—'} / agrément {u.agrement or '—'} / île {u.ile.nom if u.ile else '—'}"
        u.nom, u.username, u.role, u.telephone, u.agrement, u.ile_id = nom, username, role, telephone, agrement, ile_id
        db.session.flush()
        db.session.refresh(u)
        if mdp:
            u.set_password(mdp)
            u.doit_changer_mdp = True
        audit("modification_utilisateur", "user", u.id, ancien=avant, nouveau=f"{nom} / {username} / {role} / {telephone or '—'} / agrément {agrement or '—'} / île {u.ile.nom if u.ile else '—'}" + (" ; mot de passe réinitialisé" if mdp else ""))
        db.session.commit()
        flash("Compte modifié." + (" Le nouveau mot de passe devra être changé à la prochaine connexion." if mdp else ""), "success")
        return redirect(url_for("users.liste", role=role if request.args.get("role") else None))
    return render_template("users/modifier.html", **_contexte_modification(u, {"nom": u.nom, "username": u.username, "role": u.role, "telephone": u.telephone or "", "agrement": u.agrement or "", "ile_id": str(u.ile_id or "")}, minimum, peut_changer_role, peut_changer_ile))


@bp.route("/<int:user_id>/activite")
@roles_required(C.ADMIN, C.DIRECTEUR)
def activite(user_id):
    """Tout ce que ce compte a fait (journal d'audit), avec un filtre par date et heure et par type d'action."""
    from . import activite as Act

    u = _visibles().filter(User.id == user_id).first()
    if u is None:
        abort(404)
    du, au, erreur = _lire_periode(request.args)
    codes = dict(Act.actions_du_compte(u))
    action = request.args.get("action") if request.args.get("action") in codes else None
    page = request.args.get("page", 1, type=int)
    pagination = Act.journal(u, du, au, action, page)
    return render_template(
        "users/activite.html",
        u=u,
        pagination=pagination,
        lignes=Act.lignes_journal(pagination.items, current_user),
        actions=codes,
        filtre={"du": request.args.get("du", ""), "au": request.args.get("au", ""), "action": action or "", "erreur": erreur, "actif": bool(du or au or action)},
    )


@bp.route("/<int:user_id>/supprimer", methods=["POST"])
@roles_required(C.ADMIN, C.DIRECTEUR)
def supprimer(user_id):
    u = _gere(user_id)
    if u.id == current_user.id:
        flash("Vous ne pouvez pas supprimer votre propre compte.", "danger")
    elif u.role == C.ADMIN and u.actif and _admins_actifs() <= 1:
        flash("C'est le dernier administrateur actif : il ne peut pas être supprimé.", "danger")
    elif _references(u):
        flash("Ce compte a une activité enregistrée (dossiers, documents, journal…) : il ne peut pas être supprimé, pour garder la traçabilité. Désactivez-le plutôt.", "danger")
    else:
        nom = u.nom
        User.query.filter(User.cree_par_id == u.id).update({"cree_par_id": None})
        audit("suppression_utilisateur", "user", u.id, ancien=f"{u.nom} / {u.username} / {u.role}")
        db.session.delete(u)
        db.session.commit()
        flash(f"Compte de {nom} supprimé.", "success")
    return redirect(url_for("users.liste", role=request.form.get("role") or None))
