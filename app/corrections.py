"""Correction des données d'un dossier : commune, superficie, nom du propriétaire…

Deux circuits, selon la valeur juridique des données :

- Dossier EN COURS de traitement : la correction s'applique tout de suite. Elle est tracée (qui, quand, motif,
  valeur avant -> après) dans l'historique du circuit et le journal d'audit.
- Dossier CERTIFIÉ ou ancien : le Domaine PROPOSE, le Directeur VALIDE, comme pour toute mutation. La demande est
  une rectification structurée, scellée dans la chaîne signée du dossier (avec les valeurs avant -> après) ; les
  données ne changent qu'à la validation. Les documents déjà imprimés deviennent alors « non à jour ».

Un dossier clôturé n'est plus modifiable. Le propriétaire d'un dossier est une fiche partagée avec les dossiers dont il
descend ou qui en descendent (vente, division, fusion) : corriger son nom le corrige partout, ce que le formulaire signale.
Corriger ne permet JAMAIS de changer les parts ni le propriétaire : pour cela, il faut une mutation.
"""

import json
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import mutations as M
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import Dossier, EtapeLog, Ile, Mutation, MutationPartie, PartInitiale, Proprietaire, maintenant

bp = Blueprint("corrections", __name__)

CHAMPS_PARCELLE = [
    ("ile_id", "Île"),
    ("commune", "Commune"),
    ("village", "Village / quartier"),
    ("reference", "Référence de la parcelle"),
    ("superficie_m2", "Superficie déclarée (m²)"),
    ("type_titre", "Type de titre"),
]
CHAMPS_PROPRIETAIRE = [
    ("nom", "Nom"),
    ("prenom", "Prénom"),
    ("identite", "Pièce d'identité"),
    ("telephone", "Téléphone"),
    ("adresse", "Adresse"),
]
LIBELLE = {**dict(CHAMPS_PARCELLE), **dict(CHAMPS_PROPRIETAIRE)}
ETAPES_CIRCUIT = (C.PLAN_CROQUIS, C.ATTENTE_AVIS, C.SAISIE_RSP, C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR)


class CorrectionError(M.MutationError):
    """Correction refusée (message affichable à l'utilisateur)."""


# --------------------------------------------------------------------------
# Droits
# --------------------------------------------------------------------------
def mode(user, d):
    """'direct' (dossier en cours), 'demande' (dossier certifié ou ancien, à valider) ou None."""
    if d.est_bloque:
        return None
    if d.statut in ETAPES_CIRCUIT:
        if user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE, C.TOPO_PUBLIC):
            return "direct"
        if user.role == C.TOPO_PRIVE and d.topographe_id == user.id and d.avis_envoye_at is None:
            return "direct"
        return None
    if M.peut_saisir(user, d):
        return "demande"
    return None


# --------------------------------------------------------------------------
# Lecture et validation du formulaire
# --------------------------------------------------------------------------
def _texte(brut, nom, maxlen, obligatoire=False):
    valeur = " ".join((brut or "").split())
    if obligatoire and not valeur:
        raise CorrectionError(f"{nom} est obligatoire.")
    if len(valeur) > maxlen:
        raise CorrectionError(f"{nom} est trop long (maximum {maxlen} caractères).")
    return valeur or None


def _valeurs_parcelle(d):
    p = d.parcelle
    return {"ile_id": p.ile_id, "commune": p.commune, "village": p.village, "reference": p.reference, "superficie_m2": p.superficie_m2, "type_titre": p.type_titre}


def _valeurs_proprietaire(p):
    return {"nom": p.nom, "prenom": p.prenom, "identite": p.identite, "telephone": p.telephone, "adresse": p.adresse}


def _nettoyer_parcelle(champ, brut):
    if champ == "ile_id":
        try:
            ile = db.session.get(Ile, int(brut))
        except (TypeError, ValueError):
            ile = None
        if ile is None:
            raise CorrectionError("L'île choisie n'existe pas.")
        return ile.id
    if champ == "commune":
        return _texte(brut, "La commune", 120, obligatoire=True)
    if champ == "village":
        return _texte(brut, "Le village", 120)
    if champ == "reference":
        return _texte(brut, "La référence", 80)
    if champ == "superficie_m2":
        texte = (brut or "").strip().replace(",", ".").replace(" ", "")
        if not texte:
            return None
        try:
            valeur = float(texte)
        except ValueError:
            raise CorrectionError("La superficie doit être un nombre.") from None
        if not 0 < valeur < 1e9:
            raise CorrectionError("La superficie doit être un nombre positif.")
        return round(valeur, 2)
    if champ == "type_titre":
        valeur = (brut or "").strip() or None
        if valeur is not None and valeur not in C.TYPES_TITRE:
            raise CorrectionError("Type de titre inconnu.")
        return valeur
    raise CorrectionError("Champ inconnu.")


def _nettoyer_proprietaire(champ, brut):
    maxima = {"nom": 120, "prenom": 120, "identite": 80, "telephone": 40, "adresse": 255}
    return _texte(brut, f"{LIBELLE[champ]}", maxima[champ], obligatoire=(champ == "nom"))


def _meme(a, b):
    return (a or None) == (b or None) if not isinstance(a, (int, float)) or isinstance(a, bool) else a == b


def lire_formulaire(d, form, et):
    """Retourne (changements, motif). `changements` ne contient que ce qui a réellement changé."""
    motif = _texte(form.get("motif"), "Le motif de la correction", 500, obligatoire=True)
    changements = {"parcelle": {}, "proprietaires": {}}

    actuel = _valeurs_parcelle(d)
    for champ, _ in CHAMPS_PARCELLE:
        if f"p_{champ}" not in form:
            continue
        nouveau = _nettoyer_parcelle(champ, form.get(f"p_{champ}"))
        if not _meme(actuel[champ], nouveau):
            changements["parcelle"][champ] = [actuel[champ], nouveau]

    for proprietaire, _ in et.parts:  # seuls les propriétaires ACTUELS de ce dossier sont modifiables
        courant = _valeurs_proprietaire(proprietaire)
        modifs = {}
        for champ, _ in CHAMPS_PROPRIETAIRE:
            cle = f"o{proprietaire.id}_{champ}"
            if cle not in form:
                continue
            nouveau = _nettoyer_proprietaire(champ, form.get(cle))
            if not _meme(courant[champ], nouveau):
                modifs[champ] = [courant[champ], nouveau]
        if modifs:
            changements["proprietaires"][str(proprietaire.id)] = modifs

    if not changements["parcelle"] and not changements["proprietaires"]:
        raise CorrectionError("Aucune modification : changez au moins une valeur.")
    return changements, motif


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
def verifier_avant(d, changements, et):
    """Les valeurs « avant » doivent encore être les valeurs actuelles (sinon : conflit avec une autre correction)."""
    actuel = _valeurs_parcelle(d)
    for champ, (avant, _) in changements.get("parcelle", {}).items():
        if not _meme(actuel[champ], avant):
            raise CorrectionError(f"« {LIBELLE[champ]} » a changé depuis la saisie de cette correction : elle n'est plus applicable telle quelle.")
    courants = {p.id: p for p, _ in et.parts}
    for pid, modifs in changements.get("proprietaires", {}).items():
        p = courants.get(int(pid))
        if p is None:
            raise CorrectionError("Un propriétaire visé par cette correction n'est plus propriétaire de ce dossier.")
        valeurs = _valeurs_proprietaire(p)
        for champ, (avant, _) in modifs.items():
            if not _meme(valeurs[champ], avant):
                raise CorrectionError(f"« {LIBELLE[champ]} » de {p.nom_complet} a changé depuis la saisie de cette correction.")


def appliquer(d, changements):
    p = d.parcelle
    for champ, (_, apres) in changements.get("parcelle", {}).items():
        setattr(p, champ, apres)
    for pid, modifs in changements.get("proprietaires", {}).items():
        personne = db.session.get(Proprietaire, int(pid))
        for champ, (_, apres) in modifs.items():
            setattr(personne, champ, apres)


def resume(changements):
    """Lignes affichables : {"groupe", "champ", "avant", "apres"}."""
    noms_iles = {i.id: i.nom for i in Ile.query.all()}

    def afficher(champ, valeur):
        if valeur in (None, ""):
            return "—"
        if champ == "ile_id":
            return noms_iles.get(valeur, str(valeur))
        if champ == "superficie_m2":
            return f"{valeur:,.2f}".replace(",", " ").rstrip("0").rstrip(".").replace(".", ",") + " m²"
        return str(valeur)

    lignes = []
    for champ, (avant, apres) in changements.get("parcelle", {}).items():
        lignes.append({"groupe": "Parcelle", "champ": LIBELLE[champ], "avant": afficher(champ, avant), "apres": afficher(champ, apres)})
    for pid, modifs in changements.get("proprietaires", {}).items():
        personne = db.session.get(Proprietaire, int(pid))
        nom = " ".join(x for x in ((modifs.get("nom") or [personne.nom])[0], (modifs.get("prenom") or [personne.prenom])[0]) if x)    # nom d'abord
        for champ, (avant, apres) in modifs.items():
            lignes.append({"groupe": nom, "champ": LIBELLE[champ], "avant": afficher(champ, avant), "apres": afficher(champ, apres)})
    return lignes


def texte_resume(changements, fleche=" → "):
    return " ; ".join(f"{l['champ']} : {l['avant']}{fleche}{l['apres']}" for l in resume(changements))


def charger(m):
    return json.loads(m.details).get("corrections", {}) if m.details else {}


def controler(m, et, d):
    """Contrôle à la saisie ET à la validation d'une correction structurée."""
    verifier_avant(d, charger(m), et)


def appliquer_mutation(m, user, d):
    """Appelé par mutations.valider (même transaction) pour une correction structurée validée."""
    changements = charger(m)
    appliquer(d, changements)
    audit("correction_appliquee", "dossier", d.id, ancien=json.dumps(changements, ensure_ascii=False), nouveau=texte_resume(changements), user=user)


def appliquer_direct(d, user, changements, motif):
    texte = texte_resume(changements)
    appliquer(d, changements)
    db.session.add(
        EtapeLog(dossier=d, de_statut=d.statut, vers_statut=d.statut, type="correction", user_id=user.id, commentaire=f"Correction ({motif}) : {texte}")
    )
    audit("correction_dossier", "dossier", d.id, ancien=json.dumps(changements, ensure_ascii=False), nouveau=f"{texte} | motif : {motif}", user=user)


def proposer(d, user, changements, motif):
    return Mutation(
        dossier_id=d.id,
        type=C.MUT_RECTIFICATION,
        statut=C.MS_ATTENTE,
        date_acte=date.today(),
        notes=motif,
        saisi_par_id=user.id,
        saisi_at=maintenant(),
        details=json.dumps({"corrections": changements}, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )


# --------------------------------------------------------------------------
# Personnes partagées avec d'autres dossiers
# --------------------------------------------------------------------------
def autres_dossiers(proprietaire_id, d):
    ids = {i for (i,) in db.session.query(Dossier.id).filter(Dossier.proprietaire_id == proprietaire_id)}
    ids |= {i for (i,) in db.session.query(PartInitiale.dossier_id).filter(PartInitiale.proprietaire_id == proprietaire_id)}
    ids |= {i for (i,) in db.session.query(Mutation.dossier_id).join(MutationPartie, MutationPartie.mutation_id == Mutation.id).filter(MutationPartie.proprietaire_id == proprietaire_id)}
    ids.discard(d.id)
    return W.restreindre_ile(Dossier.query.filter(Dossier.id.in_(ids)), current_user).order_by(Dossier.id).all() if ids else []


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
@bp.route("/dossiers/<int:dossier_id>/corriger", methods=["GET", "POST"])
@login_required
def corriger(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    quel_mode = mode(current_user, d)
    if quel_mode is None:
        abort(403)
    et = M.etat(d)

    def page(code=200):
        valeurs = request.form if request.method == "POST" else {}
        return (
            render_template(
                "dossiers/corriger.html",
                d=d,
                mode=quel_mode,
                et=et,
                v=valeurs,
                iles=Ile.query.order_by(Ile.id).all(),
                types_titre=C.TYPES_TITRE,
                actuel=_valeurs_parcelle(d),
                partages={p.id: autres_dossiers(p.id, d) for p, _ in et.parts},
                champs_proprietaire=CHAMPS_PROPRIETAIRE,
            ),
            code,
        )

    if request.method == "GET":
        return page()
    try:
        changements, motif = lire_formulaire(d, request.form, et)
        if quel_mode == "direct":
            appliquer_direct(d, current_user, changements, motif)
            db.session.commit()
            flash("Dossier corrigé. La correction est enregistrée dans l'historique.", "success")
            return redirect(url_for("main.detail_dossier", dossier_id=d.id))
        m = proposer(d, current_user, changements, motif)
        verifier_avant(d, changements, et)
        db.session.add(m)
        db.session.flush()
        audit("mutation_saisie", "mutation", m.id, nouveau=f"correction dossier {d.numero}")
        db.session.commit()
    except M.MutationError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return page(400)
    flash("Demande de correction enregistrée : elle attend la validation du Directeur. Les données ne changent qu'à la validation.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#tracabilite")
