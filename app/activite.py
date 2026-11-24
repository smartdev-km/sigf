"""Informations de la page « Mon profil » : chiffres d'activité, dernières actions et connexions."""

from datetime import timedelta

from flask import url_for

from . import constants as C
from .extensions import db
from .models import AuditLog, Blocage, Document, DocumentEmis, Dossier, ModificationPersonne, Mutation, maintenant

# Libellé affichable de chaque action du journal d'audit
ACTIONS = {
    "connexion": "Connexion",
    "deconnexion": "Déconnexion",
    "changement_mdp": "Mot de passe modifié",
    "modification_profil": "Informations du profil modifiées",
    "creation_dossier": "Dossier créé",
    "import_ancien_dossier": "Ancien dossier importé",
    "transition": "Dossier avancé dans le circuit",
    "avis_envoye": "Avis envoyé",
    "saisie_rsp": "Numéro RTC saisi",
    "saisie_rcp": "Numéro RTS saisi",
    "correction_dossier": "Dossier corrigé",
    "description_terrain_modifiee": "Description du terrain modifiée",
    "bornage_trace_ajoute": "Tracé de bornage déposé",
    "bornage_trace_remplace": "Tracé de bornage remplacé",
    "bornage_trace_telecharge": "Tracé de bornage téléchargé",
    "titrage_ouvert": "Titrage ouvert (Journal officiel à faire)",
    "titrage_etape": "Étape de titrage accomplie",
    "titrage_retour": "Dossier renvoyé à l'étape précédente du titrage",
    "correction_rsp": "Numéro RTC corrigé",
    "correction_rcp": "Numéro RTS corrigé",
    "correction_appliquee": "Correction validée et appliquée",
    "mutation_saisie": "Mutation saisie",
    "mutation_validee": "Mutation validée",
    "mutation_rejetee": "Mutation rejetée",
    "dossier_cree_par_transfert": "Nouveau dossier créé par transfert de propriété",
    "dossier_cree_par_division": "Nouveaux dossiers créés par division",
    "dossier_cree_par_fusion": "Dossier créé par fusion",
    "document_ajoute": "Document déposé",
    "document_retire": "Document retiré",
    "document_emis": "Document émis",
    "documents_generes": "Documents du dossier générés",
    "impression": "Document imprimé",
    "document_revoque": "Document révoqué",
    "trace_ajoute": "Tracé déposé",
    "trace_remplace": "Tracé remplacé",
    "division_saisie": "Division saisie",
    "fusion_saisie": "Fusion saisie",
    "document_consulte": "Document consulté",
    "proprietaire_consulte": "Profil de propriétaire consulté",
    "trace_telecharge": "Tracé téléchargé",
    "trace_export_google_earth": "Tracé exporté vers Google Earth",
    "signature_enregistree": "Signature numérique enregistrée",
    "signature_titre_modifiee": "Mention de la signature modifiée",
    "signature_supprimee": "Signature numérique supprimée",
    "blocage_signale": "Problème signalé (dossier bloqué)",
    "blocage_leve": "Blocage levé",
    "personne_modification_demandee": "Modification d'une personne demandée",
    "personne_modification_validee": "Modification d'une personne validée",
    "personne_modification_rejetee": "Modification d'une personne rejetée",
    "creation_utilisateur": "Compte créé",
    "modification_utilisateur": "Compte modifié",
    "activation_utilisateur": "Compte activé",
    "desactivation_utilisateur": "Compte désactivé",
    "suppression_utilisateur": "Compte supprimé",
}


def _n(q):
    return q.count()


def chiffres(user):
    """[(libellé, nombre, icône)] adaptés au rôle."""
    lignes = []
    if user.role in (C.ADMIN, C.DIRECTEUR, C.TOPO_PUBLIC, C.TOPO_PRIVE):
        lignes.append(("Dossiers créés", _n(Dossier.query.filter(Dossier.cree_par_id == user.id)), "folder-plus"))
    if user.role == C.TOPO_PRIVE:
        lignes.append(("Dossiers qui vous sont attribués", _n(Dossier.query.filter(Dossier.topographe_id == user.id)), "folder2-open"))
    if user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE):
        lignes.append(("Mutations saisies", _n(Mutation.query.filter(Mutation.saisi_par_id == user.id)), "arrow-left-right"))
    if user.role == C.DIRECTEUR:
        lignes.append(("Mutations validées", _n(Mutation.query.filter(Mutation.decision_par_id == user.id, Mutation.statut == C.MS_VALIDEE)), "check2-circle"))
        lignes.append(("Mutations rejetées", _n(Mutation.query.filter(Mutation.decision_par_id == user.id, Mutation.statut == C.MS_REJETEE)), "x-circle"))
        lignes.append(("Blocages levés", _n(Blocage.query.filter(Blocage.leve_par_id == user.id)), "unlock"))
    if user.role == C.DOMAINE:
        lignes.append(("Modifications de personnes proposées", _n(ModificationPersonne.query.filter(ModificationPersonne.saisi_par_id == user.id)), "person-vcard"))
    if user.role != C.TOPO_PRIVE:
        lignes.append(("Documents générés", _n(DocumentEmis.query.filter(DocumentEmis.emis_par_id == user.id)), "printer"))
    if user.role != C.TOPO_PRIVE:
        lignes.append(("Documents déposés", _n(Document.query.filter(Document.uploaded_by_id == user.id)), "paperclip"))
    lignes.append(("Problèmes signalés", _n(Blocage.query.filter(Blocage.signale_par_id == user.id)), "slash-circle"))
    return lignes


def connexions(user, limite=5):
    """Dernières connexions (date, adresse IP) et tentatives échouées récentes sur ce compte."""
    reussies = AuditLog.query.filter(AuditLog.user_id == user.id, AuditLog.action == "connexion").order_by(AuditLog.id.desc()).limit(limite).all()
    depuis = maintenant() - timedelta(days=30)
    echecs = AuditLog.query.filter(AuditLog.action == "connexion_echec", AuditLog.entite == "user", AuditLog.entite_id == user.id, AuditLog.created_at >= depuis)
    return {"reussies": reussies, "echecs_30j": echecs.count(), "dernier_echec": echecs.order_by(AuditLog.id.desc()).first()}


# --------------------------------------------------------------------------
# Journal complet d'un compte (page « Utilisateurs » > Voir)
# --------------------------------------------------------------------------
PAR_PAGE = 50


def libelle_action(code):
    if code == "connexion_echec":
        return "Tentative de connexion échouée"
    return ACTIONS.get(code, code.replace("_", " ").capitalize())


def actions_du_compte(user):
    """Actions distinctes de ce compte, pour le sélecteur : [(code, libellé)] triées par libellé."""
    codes = [c for (c,) in db.session.query(AuditLog.action).filter(AuditLog.user_id == user.id).distinct()]
    return sorted(((c, libelle_action(c)) for c in codes), key=lambda x: x[1])


def journal(user, du=None, au=None, action=None, page=1):
    """Tous les mouvements du compte, les plus récents d'abord, avec filtre par période (à la minute près) et par action."""
    q = AuditLog.query.filter(AuditLog.user_id == user.id)
    if du:
        q = q.filter(AuditLog.created_at >= du)
    if au:
        q = q.filter(AuditLog.created_at < au + timedelta(minutes=1))
    if action:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.id.desc()).paginate(page=page, per_page=PAR_PAGE, error_out=False)


def lignes_journal(lignes, voyeur):
    """Prépare l'affichage : libellé, dossier (avec lien seulement si `voyeur` peut le voir), détail abrégé."""
    from . import workflow as W

    ids = [a.entite_id for a in lignes if a.entite == "dossier" and a.entite_id]
    dossiers = {d.id: d for d in Dossier.query.filter(Dossier.id.in_(ids)).all()} if ids else {}
    sortie = []
    for a in lignes:
        d = dossiers.get(a.entite_id) if a.entite == "dossier" else None
        detail = (a.nouveau or a.ancien or "").strip()
        sortie.append(
            {
                "quand": a.created_at,
                "texte": libelle_action(a.action),
                "code": a.action,
                "dossier": d.numero if d else None,
                "url": url_for("main.detail_dossier", dossier_id=d.id) if d and W.peut_voir(d, voyeur) else None,
                "detail": detail if len(detail) <= 160 else detail[:157] + "…",
                "ip": a.ip,
            }
        )
    return sortie
