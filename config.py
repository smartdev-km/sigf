import os


def _charger_env(chemin=".env"):
    """Lit un fichier .env local (développement). Les vraies variables d'environnement l'emportent."""
    try:
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "=" not in ligne:
                    continue
                cle, _, valeur = ligne.partition("=")
                os.environ.setdefault(cle.strip(), valeur.strip().strip("\"'"))
    except OSError:
        pass


_charger_env()


def _database_uri():
    uri = os.environ.get("DATABASE_URL", "sqlite:///sigf.db")
    # Render fournit parfois "postgres://", SQLAlchemy attend "postgresql://"
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    if os.environ.get("RENDER") and uri.startswith("sqlite"):
        # Sans DATABASE_URL, l'application utiliserait un fichier SQLite effacé à chaque déploiement : on refuse plutôt que de perdre les données.
        raise RuntimeError(
            "DATABASE_URL n'est pas défini : créez une base PostgreSQL sur Render et copiez son adresse "
            "(Internal Database URL) dans la variable d'environnement DATABASE_URL du service."
        )
    return uri


class Config:
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = bool(os.environ.get("RENDER"))
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8  # 8 heures
    MDP_LONGUEUR_MIN = 10

    # Satellite Google (facultatif). Sans clé, la carte utilise Esri. La clé s'affiche dans la page : elle DOIT être
    # restreinte (référents HTTP + API « Maps JavaScript » seulement) dans la console Google Cloud.
    GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY") or None

    # Fonds de carte (modifiables : à remplacer par un fournisseur sous contrat si besoin)
    TUILES_SATELLITE_URL = os.environ.get(
        "TUILES_SATELLITE_URL",
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    )
    # Dernier niveau de zoom où l'imagerie existe vraiment (mesuré sur les 3 îles : 17). Au-delà, le
    # fournisseur renvoie une image « Map data not yet available » : la carte agrandit alors le niveau 17.
    TUILES_SATELLITE_ZOOM_NATIF = int(os.environ.get("TUILES_SATELLITE_ZOOM_NATIF", 17))
    TUILES_SATELLITE_ATTRIBUTION = os.environ.get("TUILES_SATELLITE_ATTRIBUTION", "Imagerie © Esri, Maxar, Earthstar Geographics")
    TUILES_PLAN_URL = os.environ.get("TUILES_PLAN_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    TUILES_PLAN_ATTRIBUTION = os.environ.get("TUILES_PLAN_ATTRIBUTION", "© contributeurs OpenStreetMap")

    # QR code de sécurité. QR_SECRET signe chaque document émis : ne JAMAIS le changer une
    # fois des documents imprimés (leurs QR deviendraient invérifiables) et ne jamais le publier.
    QR_SECRET = os.environ.get("QR_SECRET")
    # Adresse publique du site (ex. https://sigf.onrender.com), encodée dans les QR.
    PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")

    # En-tête des documents imprimés (modifiable sans toucher au code)
    ENTETE_PAYS = os.environ.get("ENTETE_PAYS", "UNION DES COMORES")
    ENTETE_DEVISE = os.environ.get("ENTETE_DEVISE", "Unité - Solidarité - Développement")
    ENTETE_SERVICE = os.environ.get("ENTETE_SERVICE", "Service de la gestion foncière")

    # Génération automatique de tous les documents (plan croquis, certificat, extrait, attestation) à la certification d'un dossier
    DOCUMENTS_AUTOMATIQUES = os.environ.get("DOCUMENTS_AUTOMATIQUES", "1").lower() not in ("0", "non", "false", "off")

    # Fichiers numérisés
    MAX_FICHIER_MO = int(os.environ.get("MAX_FICHIER_MO", 25))
    # Délai légal minimal (en jours) entre la parution au Journal officiel et le bornage ; 0 = aucun délai imposé.
    TITRAGE_DELAI_BORNAGE_JOURS = int(os.environ.get("TITRAGE_DELAI_BORNAGE_JOURS", 0))
    MAX_CONTENT_LENGTH = 120 * 1024 * 1024  # taille maximale d'une requête (plusieurs scans)
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR")  # défaut : instance/uploads
    # Stockage S3-compatible (Cloudflare R2, Backblaze B2, AWS S3…) — obligatoire sur Render
    S3_BUCKET = os.environ.get("S3_BUCKET")
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
    S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
    S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
    S3_REGION = os.environ.get("S3_REGION", "auto")

    @classmethod
    def verifier(cls):
        if not cls.QR_SECRET:
            if os.environ.get("RENDER"):
                raise RuntimeError("QR_SECRET doit être défini en production.")
            cls.QR_SECRET = "dev-uniquement-qr-secret"
        if not cls.SECRET_KEY:
            if os.environ.get("RENDER"):
                raise RuntimeError("SECRET_KEY doit être défini en production.")
            cls.SECRET_KEY = "dev-uniquement-ne-pas-utiliser-en-production"
