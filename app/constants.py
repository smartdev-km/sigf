"""Constantes métier : rôles, statuts du circuit, libellés."""

ADMIN = "admin"
DIRECTEUR = "directeur"
TOPO_PUBLIC = "topo_public"
TOPO_PRIVE = "topo_prive"
DOMAINE = "domaine"
FONCTION_PUBLIQUE = "fonction_publique"

ROLES = {
    ADMIN: "Administrateur",
    DIRECTEUR: "Directeur",
    TOPO_PUBLIC: "Topographe public",
    TOPO_PRIVE: "Topographe privé",
    DOMAINE: "Domaine",
    FONCTION_PUBLIQUE: "Fonction publique",
}

# --- Titrage : étapes qui suivent la certification (publication, bornage, titre foncier) ---
# `Dossier.etape_titre` donne l'étape À FAIRE ; le statut du circuit reste « Certificat émis » (les mutations restent possibles).
TIT_JO = "journal_officiel"
TIT_BORNAGE = "plan_bornage"
TIT_JC = "journal_cloture"
TIT_TITRAGE = "titrage"
TIT_FIN = "titre"          # titre foncier délivré : titrage terminé
ETAPES_TITRAGE = [TIT_JO, TIT_BORNAGE, TIT_JC, TIT_TITRAGE]
TITRAGE_LIBELLES = {
    TIT_JO: "Journal officiel",
    TIT_BORNAGE: "Plan de bornage",
    TIT_JC: "Journal de clôture",
    TIT_TITRAGE: "Titré",
    TIT_FIN: "Titré",
}
# Qui accomplit l'étape (l'administrateur peut toujours se substituer)
TITRAGE_ACTEUR = {TIT_JO: FONCTION_PUBLIQUE, TIT_BORNAGE: DOMAINE, TIT_JC: FONCTION_PUBLIQUE, TIT_TITRAGE: DOMAINE}
# Ce que chaque étape enregistre : (libellé de la référence, libellé de la date, documents à joindre)
TITRAGE_CHAMPS = {
    TIT_JO: ("N° du Journal officiel", "Date de parution", ["Journal officiel"]),
    TIT_BORNAGE: ("N° du procès-verbal de bornage", "Date du bornage", ["Procès-verbal de bornage", "Plan de bornage"]),
    TIT_JC: ("N° du Journal de clôture", "Date de parution", ["Journal de clôture"]),
    TIT_TITRAGE: ("N° du titre foncier", "Date du titre", ["Titre foncier"]),
}
# Onglets de la page « Titrage » : une étape à faire chacun, puis les dossiers titrés
ONGLETS_TITRAGE = {
    TIT_JO: "Journal officiel",
    TIT_BORNAGE: "Plan de bornage",
    TIT_JC: "Journal de clôture",
    TIT_TITRAGE: "À titrer",
    TIT_FIN: "Titrés",
}


# Statuts du circuit
PLAN_CROQUIS = "plan_croquis"
ATTENTE_AVIS = "attente_avis"
SAISIE_RSP = "saisie_rsp"
ENREGISTREMENT_RCP = "enregistrement_rcp"
VALIDATION_DIRECTEUR = "validation_directeur"
CERTIFIE = "certifie"
ARCHIVE = "archive"  # ancien dossier numérisé, hors circuit
CLOTURE = "cloture"  # dossier clôturé (divisé, fusionné, ou propriété transférée) : historique conservé

STATUTS = {
    PLAN_CROQUIS: "Plan croquis en cours",
    ATTENTE_AVIS: "En attente d'avis",
    SAISIE_RSP: "Saisie du RTC",
    ENREGISTREMENT_RCP: "Enregistrement Domaine (RTS)",
    VALIDATION_DIRECTEUR: "Validation du Directeur",
    CERTIFIE: "Certificat émis",
    ARCHIVE: "Ancien dossier (numérisé)",
    CLOTURE: "Clôturé",
}

STATUT_COULEUR = {
    PLAN_CROQUIS: "primary",
    ATTENTE_AVIS: "warning",
    SAISIE_RSP: "info",
    ENREGISTREMENT_RCP: "secondary",
    VALIDATION_DIRECTEUR: "dark",
    CERTIFIE: "success",
    ARCHIVE: "light",
    CLOTURE: "secondary",
}

# Étapes affichées dans le suivi, selon la filière
ETAPES_PUBLIC = [PLAN_CROQUIS, SAISIE_RSP, ENREGISTREMENT_RCP, VALIDATION_DIRECTEUR, CERTIFIE]
ETAPES_PRIVE = [PLAN_CROQUIS, ATTENTE_AVIS, SAISIE_RSP, ENREGISTREMENT_RCP, VALIDATION_DIRECTEUR, CERTIFIE]

FILIERE_PUBLIC = "public"
FILIERE_PRIVE = "prive"

ORIGINE_NOUVEAU = "nouveau"
ORIGINE_ANCIEN = "ancien"

# --- Mutations (mouvements du terrain) ---
MUT_VENTE = "vente"
MUT_DONATION = "donation"
MUT_HERITAGE = "heritage"
MUT_EXPROPRIATION = "expropriation"
MUT_HYPOTHEQUE = "hypotheque"
MUT_MAINLEVEE = "mainlevee"
MUT_RECTIFICATION = "rectification"
MUT_ANNULATION = "annulation"
MUT_DIVISION = "division"
MUT_FUSION = "fusion"

MUTATIONS = {
    MUT_VENTE: "Vente",
    MUT_DONATION: "Donation",
    MUT_HERITAGE: "Héritage (succession)",
    MUT_EXPROPRIATION: "Expropriation",
    MUT_HYPOTHEQUE: "Hypothèque (inscription)",
    MUT_MAINLEVEE: "Mainlevée d'hypothèque",
    MUT_RECTIFICATION: "Rectification",
    MUT_ANNULATION: "Annulation de la dernière mutation",
    MUT_DIVISION: "Division (partage)",
    MUT_FUSION: "Fusion de parcelles",
}
# Mutations qui clôturent la parcelle et créent de nouveaux dossiers (formulaires dédiés)
PARTAGES = (MUT_DIVISION, MUT_FUSION)
# Mutations qui transfèrent des parts de propriété
TRANSFERTS = (MUT_VENTE, MUT_DONATION, MUT_HERITAGE, MUT_EXPROPRIATION)

MS_ATTENTE = "en_attente"
MS_VALIDEE = "validee"
MS_REJETEE = "rejetee"
MUTATION_STATUTS = {MS_ATTENTE: "En attente de validation", MS_VALIDEE: "Validée", MS_REJETEE: "Rejetée"}

ROLE_CEDANT = "cedant"
ROLE_BENEFICIAIRE = "beneficiaire"
ROLE_CREANCIER = "creancier"

TYPE_AUTRE = "Autre"

TYPES_DOCUMENT = [
    "Plan croquis",
    "Avis",
    "Titre foncier",
    "Journal officiel",
    "Procès-verbal de bornage",
    "Plan de bornage",
    "Journal de clôture",
    "Acte (vente, donation, succession…)",
    "Pièce d'identité",
    "Attestation / certificat",
    TYPE_AUTRE,
]

# Formats de scan acceptés (vérifiés sur le contenu, pas seulement l'extension)
EXTENSIONS_AUTORISEES = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

ILES = ["Ngazidja (Grande Comore)", "Ndzuwani (Anjouan)", "Mwali (Mohéli)"]

TYPES_TITRE = [
    "Titre foncier",
    "Immatriculation",
    "Acte de notoriété",
    "Autre",
]


# Problèmes qui bloquent un dossier jusqu'à preuve du contraire (groupes affichés dans le formulaire de signalement)
PROBLEMES_GROUPES = [
    ("Juridique", [
        ("litige_propriete", "Litige de propriété ou contestation"),
        ("double_vente", "Double vente ou double titre"),
        ("succession", "Succession non réglée ou héritier qui conteste"),
        ("decision_justice", "Saisie ou décision de justice en cours"),
        ("opposition", "Opposition d'un tiers"),
    ]),
    ("Fraude et authenticité", [
        ("faux", "Soupçon de faux (acte, signature, cachet)"),
        ("usurpation", "Usurpation d'identité"),
        ("document_falsifie", "Document falsifié ou altéré"),
        ("complicite", "Complicité interne suspectée"),
    ]),
    ("Terrain", [
        ("chevauchement", "Chevauchement avec une autre parcelle"),
        ("limites_contestees", "Limites contestées par un voisin"),
        ("superficie", "Superficie incohérente (mesurée / déclarée)"),
        ("trace_invalide", "Tracé invalide ou impossible à vérifier"),
        ("domaine_public", "Empiétement sur le domaine public (route, mer, cimetière…)"),
    ]),
    ("Administratif et données", [
        ("pieces_manquantes", "Pièces manquantes ou illisibles"),
        ("identite", "Pièce d'identité manquante ou en doublon"),
        ("erreur_saisie", "Erreur de saisie à corriger"),
        ("doublon", "Dossier en doublon"),
    ]),
    ("Financier", [
        ("hypotheque_contestee", "Hypothèque contestée"),
        ("droits_impayes", "Droits ou taxes impayés"),
        ("creancier_introuvable", "Créancier introuvable"),
    ]),
    ("Intégrité", [
        ("historique_altere", "Historique des mutations altéré"),
        ("document_conteste", "Document imprimé contesté"),
    ]),
    ("Autre", [
        ("autre", "Autre problème (à préciser)"),
    ]),
]
PROBLEMES = {code: libelle for _, lignes in PROBLEMES_GROUPES for code, libelle in lignes}


# Libellés courts des onglets de la page « Dossiers » (un onglet par statut, dans l'ordre du circuit)
ONGLETS_DOSSIERS = {
    PLAN_CROQUIS: "Plan croquis",
    ATTENTE_AVIS: "Avis",
    SAISIE_RSP: "Saisie du RTC",
    ENREGISTREMENT_RCP: "Domaine (RTS)",
    VALIDATION_DIRECTEUR: "Validation",
    CERTIFIE: "Certificat",
    CLOTURE: "Clôturé",
}


# Document généré pour chaque mutation qui cède des parts ou grève la parcelle : mutation -> (code du document, titre)
ACTES = {
    MUT_VENTE: ("acte_vente", "Acte de vente"),
    MUT_DONATION: ("acte_donation", "Acte de donation"),
    MUT_HERITAGE: ("acte_heritage", "Acte de succession (héritage)"),
    MUT_EXPROPRIATION: ("acte_expropriation", "Acte d'expropriation"),
    MUT_HYPOTHEQUE: ("acte_hypotheque", "Acte d'inscription d'hypothèque"),
    MUT_MAINLEVEE: ("acte_mainlevee", "Acte de mainlevée d'hypothèque"),
}
TYPES_ACTES = {code: titre for code, titre in ACTES.values()}


# Description du terrain imprimée sur le plan croquis (saisie par le topographe)
NATURES_TERRAIN = ["Terrain nu", "Terrain bâti", "Terrain cultivé", "Terrain planté (cocoteraie, bananeraie…)", "Terrain clôturé", "Terrain mixte"]
MODES_LEVE = ["GPS", "Station totale", "Théodolite", "Ruban / décamètre", "Autre"]
CHAMPS_DESCRIPTION = [
    # (champ, libellé, longueur maximale)
    ("limitrophe_nord", "Limitrophe nord", 200),
    ("limitrophe_sud", "Limitrophe sud", 200),
    ("limitrophe_est", "Limitrophe est", 200),
    ("limitrophe_ouest", "Limitrophe ouest", 200),
    ("nature_terrain", "Nature du terrain", 80),
    ("usage_terrain", "Usage / mise en valeur", 200),
    ("acces_terrain", "Accès", 200),
    ("date_leve", "Date du levé", 10),
    ("mode_leve", "Mode de levé", 40),
    ("observations_plan", "Observations", 1000),
]
