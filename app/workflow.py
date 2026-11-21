"""Circuit de traitement d'un dossier.

Filière publique : plan croquis -> RTC -> RTS (Domaine) -> validation Directeur -> certificat
Filière privée   : plan croquis -> avis (topo privé) -> RTC (topo public) -> RTS -> validation -> certificat

Toutes les règles de permission sont ici, côté serveur : l'interface ne fait que
refléter ce que ce module autorise.
"""

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased

from . import constants as C
from .audit import audit
from .extensions import db
from .models import Dossier, EtapeLog, Ile, critere_nom, Mutation, MutationPartie, Parcelle, PartInitiale, Proprietaire, maintenant


class WorkflowError(Exception):
    """Action refusée ou données invalides (message affichable à l'utilisateur)."""


# --------------------------------------------------------------------------
# Visibilité
# --------------------------------------------------------------------------
def ile_de(user):
    """Île à laquelle le compte est limité (None : toutes les îles). L'administrateur n'est jamais limité."""
    return None if user.role == C.ADMIN else user.ile_id


def dans_son_ile(user):
    """Condition SQL sur Dossier : la parcelle est dans l'île du compte (aucune condition si le compte n'est pas limité)."""
    ile = ile_de(user)
    return Dossier.parcelle.has(Parcelle.ile_id == ile) if ile else db.true()


def restreindre_ile(q, user):
    """Limite une requête qui porte déjà sur Dossier (ou y est jointe) à l'île du compte."""
    return q.filter(dans_son_ile(user)) if ile_de(user) else q


def condition_perimetre(user):
    """Condition SQL sur Dossier : tout ce que le compte a le droit de voir (son île ; pour un topographe privé, ses propres dossiers).
    None : aucune restriction."""
    conds = []
    if ile_de(user):
        conds.append(dans_son_ile(user))
    if user.role == C.TOPO_PRIVE:
        conds.append(Dossier.topographe_id == user.id)
    if user.role == C.FONCTION_PUBLIQUE:
        conds.append(Dossier.etape_titre.isnot(None))             # la Fonction publique ne voit que les dossiers entrés en titrage
    return and_(*conds) if conds else None


def perimetre(q, user):
    """Limite une requête portant sur Dossier au périmètre du compte (île, et dossiers propres pour un topographe privé)."""
    cond = condition_perimetre(user)
    return q.filter(cond) if cond is not None else q


def iles_visibles(user):
    """Îles proposées dans les listes et formulaires : toutes, ou seulement celle du compte."""
    q = Ile.query.order_by(Ile.id)
    ile = ile_de(user)
    return q.filter(Ile.id == ile).all() if ile else q.all()


def dossiers_visibles(user):
    """Requête des dossiers qu'un utilisateur a le droit de voir."""
    q = restreindre_ile(Dossier.query, user)
    if user.role == C.TOPO_PRIVE:
        q = q.filter(Dossier.topographe_id == user.id)
    if user.role == C.FONCTION_PUBLIQUE:
        q = q.filter(Dossier.etape_titre.isnot(None))
    return q


def peut_voir(dossier, user):
    ile = ile_de(user)
    if ile and dossier.parcelle.ile_id != ile:
        return False
    if user.role == C.TOPO_PRIVE:
        return dossier.topographe_id == user.id
    if user.role == C.FONCTION_PUBLIQUE:
        return dossier.etape_titre is not None
    return True


def est_restreint(user):
    """Le topographe privé a une vue limitée (pas de RTC/RTS/suivi complet)."""
    return user.role == C.TOPO_PRIVE


def statut_affiche(dossier, user):
    if est_restreint(user) and dossier.avis_envoye_at:
        return "Avis envoyé"
    return dossier.statut_label


def appliquer_recherche(q, texte):
    """Filtre une requête de dossiers (déjà jointe à Proprietaire et Parcelle) sur un texte libre :
    n° de dossier, propriétaire d'origine OU actuel (bénéficiaire d'une mutation validée),
    référence, commune, village, n° du dossier papier."""
    motif = f"%{texte}%"
    autre = aliased(Proprietaire)
    via_mutation = (
        db.session.query(Mutation.dossier_id)
        .join(MutationPartie, MutationPartie.mutation_id == Mutation.id)
        .join(autre, autre.id == MutationPartie.proprietaire_id)
        .filter(Mutation.statut == C.MS_VALIDEE, critere_nom(autre, motif))
    )
    depart = aliased(Proprietaire)
    via_parts = (  # dossiers issus d'un transfert, d'une division ou d'une fusion : propriétaires de départ
        db.session.query(PartInitiale.dossier_id)
        .join(depart, depart.id == PartInitiale.proprietaire_id)
        .filter(critere_nom(depart, motif))
    )
    return q.filter(
        Dossier.id.in_(via_mutation)
        | Dossier.id.in_(via_parts)
        | Dossier.numero.ilike(motif)
        | critere_nom(Proprietaire, motif)
        | Parcelle.reference.ilike(motif)
        | Parcelle.commune.ilike(motif)
        | Parcelle.village.ilike(motif)
        | Dossier.reference_archive.ilike(motif)
    )


def peut_creer_dossier(user):
    return user.role in (C.TOPO_PUBLIC, C.TOPO_PRIVE, C.DIRECTEUR, C.ADMIN)


def peut_importer_ancien(user):
    """Saisie des dossiers papier numérisés (hors circuit)."""
    return user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE)


def dossiers_a_traiter(user):
    """File de travail de l'utilisateur."""
    q = dossiers_visibles(user)
    r = user.role
    if r == C.TOPO_PUBLIC:
        cond = or_(
            (Dossier.statut == C.PLAN_CROQUIS) & (Dossier.filiere == C.FILIERE_PUBLIC),
            Dossier.statut == C.SAISIE_RSP,
        )
    elif r == C.TOPO_PRIVE:
        cond = Dossier.statut.in_([C.PLAN_CROQUIS, C.ATTENTE_AVIS])
    elif r == C.DOMAINE:
        cond = or_(Dossier.statut == C.ENREGISTREMENT_RCP,
                   (Dossier.statut == C.CERTIFIE) & Dossier.etape_titre.in_([C.TIT_BORNAGE, C.TIT_TITRAGE]))
    elif r == C.FONCTION_PUBLIQUE:
        cond = (Dossier.statut == C.CERTIFIE) & Dossier.etape_titre.in_([C.TIT_JO, C.TIT_JC])
    elif r == C.DIRECTEUR:
        cond = Dossier.statut == C.VALIDATION_DIRECTEUR
    else:
        return q.filter(db.false())
    return q.filter(cond)


# --------------------------------------------------------------------------
# Actions autorisées
# --------------------------------------------------------------------------
def actions_disponibles(d, u):
    s, r = d.statut, u.role
    proprio_prive = r == C.TOPO_PRIVE and d.topographe_id == u.id
    acts = []
    if d.est_bloque:  # problème signalé : le dossier est figé jusqu'à la levée du blocage
        return acts

    if s == C.PLAN_CROQUIS:
        if proprio_prive or (r == C.TOPO_PUBLIC and d.filiere == C.FILIERE_PUBLIC):
            acts.append("terminer_plan")
    elif s == C.ATTENTE_AVIS:
        if proprio_prive:
            acts.append("envoyer_avis")
    elif s == C.SAISIE_RSP:
        if r == C.TOPO_PUBLIC:
            acts.append("saisir_rsp")
    elif s == C.ENREGISTREMENT_RCP:
        if r == C.DOMAINE:
            acts += ["saisir_rcp", "rejeter"]
    elif s == C.VALIDATION_DIRECTEUR:
        if r == C.DIRECTEUR:
            acts += ["valider", "rejeter"]
    return acts


def peut_corriger(d, u, champ):
    """Correction d'un RTC/RTS déjà saisi. Après validation, seul le Directeur."""
    if d.est_bloque:
        return False
    if champ == "rsp":
        if not d.rsp:
            return False
        if u.role == C.DIRECTEUR:
            return True
        return u.role == C.TOPO_PUBLIC and d.statut in (C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR)
    if champ == "rcp":
        if not d.rcp:
            return False
        if u.role == C.DIRECTEUR:
            return True
        return u.role == C.DOMAINE and d.statut == C.VALIDATION_DIRECTEUR
    return False


def peut_imprimer(d):
    """Le plan croquis n'est imprimable qu'une fois RTC et RTS saisis."""
    return bool(d.rsp and d.rcp)


def documents_imprimables(d, user):
    """Types de documents officiels que cet utilisateur peut imprimer pour ce dossier."""
    if d.est_bloque or d.statut in (C.ARCHIVE, C.CLOTURE) or user.role not in (C.ADMIN, C.DIRECTEUR, C.TOPO_PUBLIC, C.DOMAINE):
        return []
    out = []
    if peut_imprimer(d):
        out.append("plan_croquis")
    if d.statut == C.CERTIFIE and user.role != C.TOPO_PUBLIC:
        out += ["certificat", "extrait", "attestation"]
    return out


def etapes_suivi(d):
    """Liste (statut, libellé, état) pour la barre d'étapes."""
    ordre = C.ETAPES_PRIVE if d.filiere == C.FILIERE_PRIVE else C.ETAPES_PUBLIC
    if d.statut == C.CERTIFIE:
        idx = len(ordre)
    elif d.statut in ordre:
        idx = ordre.index(d.statut)
    else:
        idx = -1
    out = []
    for i, st in enumerate(ordre):
        etat = "done" if i < idx else "current" if i == idx else "todo"
        out.append((st, C.STATUTS[st], etat))
    return out


# --------------------------------------------------------------------------
# Exécution
# --------------------------------------------------------------------------
def _propre(valeur, nom, maxlen=None):
    valeur = (valeur or "").strip()
    if not valeur:
        raise WorkflowError(f"{nom} est obligatoire.")
    if maxlen and len(valeur) > maxlen:
        raise WorkflowError(f"{nom} est trop long (maximum {maxlen} caractères).")
    return valeur


def _transition(d, user, vers, commentaire=None, type_="avance"):
    db.session.add(
        EtapeLog(
            dossier=d,
            de_statut=d.statut,
            vers_statut=vers,
            type=type_,
            user_id=user.id,
            commentaire=commentaire,
        )
    )
    audit("transition", "dossier", d.id, d.statut, vers, user=user)
    d.statut = vers


def executer(d, user, action, commentaire=None, valeur=None):
    if action not in actions_disponibles(d, user):
        raise WorkflowError("Action non autorisée pour ce dossier à cette étape.")

    if action == "terminer_plan":
        vers = C.ATTENTE_AVIS if d.filiere == C.FILIERE_PRIVE else C.SAISIE_RSP
        _transition(d, user, vers, (commentaire or "").strip() or None)

    elif action == "envoyer_avis":
        d.avis = _propre(commentaire, "L'avis")
        d.avis_envoye_at = maintenant()
        audit("avis_envoye", "dossier", d.id, user=user)
        _transition(d, user, C.SAISIE_RSP, "Avis envoyé par le topographe privé")

    elif action == "saisir_rsp":
        d.rsp = _propre(valeur, "Le numéro RTC", 60)
        audit("saisie_rsp", "dossier", d.id, nouveau=d.rsp, user=user)
        _transition(d, user, C.ENREGISTREMENT_RCP, f"RTC : {d.rsp}")

    elif action == "saisir_rcp":
        d.rcp = _propre(valeur, "Le numéro RTS", 60)
        audit("saisie_rcp", "dossier", d.id, nouveau=d.rcp, user=user)
        _transition(d, user, C.VALIDATION_DIRECTEUR, f"RTS : {d.rcp}")

    elif action == "valider":
        d.valide_par_id = user.id
        d.valide_at = maintenant()
        _transition(d, user, C.CERTIFIE, (commentaire or "").strip() or None)
        from . import titrage

        titrage.entrer_en_titrage(d, user)                # le titrage commence : Journal officiel à faire
        from . import generation  # import tardif : la génération dépend des PDF et de la vérification

        db.session.flush()
        generation.automatiques(d, user)                  # le dossier est complet : tous ses documents sont générés

    elif action == "rejeter":
        motif = _propre(commentaire, "Le motif du rejet")
        vers = C.SAISIE_RSP if d.statut == C.ENREGISTREMENT_RCP else C.ENREGISTREMENT_RCP
        _transition(d, user, vers, motif, type_="rejet")


def corriger_numero(d, user, champ, valeur):
    if not peut_corriger(d, user, champ):
        raise WorkflowError("Vous ne pouvez pas corriger ce numéro.")
    nouveau = _propre(valeur, f"Le numéro {champ.upper()}", 60)
    ancien = getattr(d, champ)
    if nouveau == ancien:
        return
    setattr(d, champ, nouveau)
    audit(f"correction_{champ}", "dossier", d.id, ancien, nouveau, user=user)
    db.session.add(
        EtapeLog(
            dossier=d,
            de_statut=d.statut,
            vers_statut=d.statut,
            type="correction",
            user_id=user.id,
            commentaire=f"{champ.upper()} corrigé : {ancien} → {nouveau}",
        )
    )


def creer_ancien_dossier(user, parcelle, proprietaire, reference_archive=None, annee=None, notes=None):
    """Reprise d'un dossier papier : situation actuelle uniquement, hors circuit (option A)."""
    if not peut_importer_ancien(user):
        raise WorkflowError("Vous ne pouvez pas importer d'ancien dossier.")
    d = Dossier(
        origine=C.ORIGINE_ANCIEN,
        filiere=C.FILIERE_PUBLIC,
        statut=C.ARCHIVE,
        parcelle=parcelle,
        proprietaire=proprietaire,
        cree_par_id=user.id,
        reference_archive=(reference_archive or "").strip() or None,
        annee_archive=annee,
        notes=(notes or "").strip() or None,
    )
    db.session.add(d)
    db.session.flush()
    d.numero = f"SIGF-{datetime.now().year}-{d.id:05d}"
    db.session.add(
        EtapeLog(
            dossier=d,
            de_statut=None,
            vers_statut=C.ARCHIVE,
            user_id=user.id,
            commentaire="Reprise de l'existant (numérisation d'un dossier papier)",
        )
    )
    audit("import_ancien_dossier", "dossier", d.id, nouveau=d.numero, user=user)
    return d


def creer_dossier(user, parcelle, proprietaire, notes=None):
    """Crée un dossier neuf, toujours au début du circuit."""
    if not peut_creer_dossier(user):
        raise WorkflowError("Vous ne pouvez pas créer de dossier.")
    prive = user.role == C.TOPO_PRIVE
    d = Dossier(
        origine=C.ORIGINE_NOUVEAU,
        filiere=C.FILIERE_PRIVE if prive else C.FILIERE_PUBLIC,
        statut=C.PLAN_CROQUIS,
        parcelle=parcelle,
        proprietaire=proprietaire,
        topographe_id=user.id if prive else None,
        cree_par_id=user.id,
        notes=(notes or "").strip() or None,
    )
    db.session.add(d)
    db.session.flush()
    d.numero = f"SIGF-{datetime.now().year}-{d.id:05d}"
    db.session.add(
        EtapeLog(dossier=d, de_statut=None, vers_statut=C.PLAN_CROQUIS, user_id=user.id, commentaire="Dossier créé")
    )
    audit("creation_dossier", "dossier", d.id, nouveau=d.numero, user=user)
    return d
