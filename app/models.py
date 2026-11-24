from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .constants import ROLES, STATUTS
from .extensions import db, login_manager


def critere_nom(entite, motif):
    """Condition SQL : `motif` (déjà entouré de %) trouve la personne par son nom, son prénom, ou son nom complet dans les DEUX ordres
    (« Mohamed Abdallah » comme « Abdallah Mohamed »)."""
    from sqlalchemy import func, literal, or_

    prenom = func.coalesce(entite.prenom, "")
    return or_(
        entite.nom.ilike(motif),
        entite.prenom.ilike(motif),
        (entite.nom + literal(" ") + prenom).ilike(motif),
        (prenom + literal(" ") + entite.nom).ilike(motif),
    )


def maintenant():
    """Heure UTC (naïve) pour les horodatages."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(40), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False)
    actif = db.Column(db.Boolean, default=True, nullable=False)
    doit_changer_mdp = db.Column(db.Boolean, default=True, nullable=False)
    cree_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False)
    derniere_connexion = db.Column(db.DateTime)
    telephone = db.Column(db.String(40))
    agrement = db.Column(db.String(60))  # n° d'agrément (topographes) : imprimé sur le plan croquis
    # Île attribuée : un compte (hors administrateur) ne voit que les informations de son île. Vide = pas de restriction
    # (comptes créés avant cette règle ; l'administrateur n'a jamais d'île).
    ile_id = db.Column(db.Integer, db.ForeignKey("iles.id"), index=True)
    # Signature numérique du Directeur (image), imprimée sur les certificats et attestations qu'il a validés
    signature_cle = db.Column(db.String(255))
    signature_at = db.Column(db.DateTime)
    signature_titre = db.Column(db.String(80))  # mention sous la signature ; « Le Directeur » par défaut

    cree_par = db.relationship("User", remote_side=[id])
    ile = db.relationship("Ile", foreign_keys=[ile_id])

    @property
    def is_active(self):  # utilisé par Flask-Login
        return self.actif

    @property
    def role_label(self):
        return ROLES.get(self.role, self.role)

    def set_password(self, mdp):
        self.password_hash = generate_password_hash(mdp)

    def check_password(self, mdp):
        return check_password_hash(self.password_hash, mdp)


@login_manager.user_loader
def charger_utilisateur(user_id):
    return db.session.get(User, int(user_id))


class Ile(db.Model):
    __tablename__ = "iles"

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(80), unique=True, nullable=False)


class Proprietaire(db.Model):
    __tablename__ = "proprietaires"

    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(120), nullable=False)
    prenom = db.Column(db.String(120))
    identite = db.Column(db.String(80))  # n° CNI / passeport
    telephone = db.Column(db.String(40))
    adresse = db.Column(db.String(255))

    @property
    def nom_complet(self):
        """Aux Comores le nom vient en premier : « Mohamed Abdallah » (nom Mohamed, prénom Abdallah)."""
        return f"{self.nom} {self.prenom}".strip() if self.prenom else self.nom


class Parcelle(db.Model):
    __tablename__ = "parcelles"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(80))
    ile_id = db.Column(db.Integer, db.ForeignKey("iles.id"), nullable=False)
    commune = db.Column(db.String(120), nullable=False)
    village = db.Column(db.String(120))
    superficie_m2 = db.Column(db.Float)
    type_titre = db.Column(db.String(80))

    ile = db.relationship("Ile")


class Dossier(db.Model):
    __tablename__ = "dossiers"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(30), unique=True, index=True)
    origine = db.Column(db.String(10), nullable=False, default="nouveau")
    filiere = db.Column(db.String(10), nullable=False, default="public")
    statut = db.Column(db.String(30), nullable=False, index=True)

    parcelle_id = db.Column(db.Integer, db.ForeignKey("parcelles.id"), nullable=False)
    proprietaire_id = db.Column(db.Integer, db.ForeignKey("proprietaires.id"), nullable=False)
    # Topographe privé propriétaire du dossier (filière privée uniquement)
    topographe_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    cree_par_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    rsp = db.Column(db.String(60))
    rcp = db.Column(db.String(60))
    # Description du terrain, imprimée sur le plan croquis (saisie par le topographe)
    limitrophe_nord = db.Column(db.String(200))
    limitrophe_sud = db.Column(db.String(200))
    limitrophe_est = db.Column(db.String(200))
    limitrophe_ouest = db.Column(db.String(200))
    nature_terrain = db.Column(db.String(80))
    usage_terrain = db.Column(db.String(200))
    acces_terrain = db.Column(db.String(200))
    date_leve = db.Column(db.Date)
    mode_leve = db.Column(db.String(40))
    observations_plan = db.Column(db.Text)
    avis = db.Column(db.Text)
    avis_envoye_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    # Anciens dossiers papier numérisés
    reference_archive = db.Column(db.String(80), index=True)  # n° du dossier papier d'origine
    annee_archive = db.Column(db.Integer)

    valide_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # Titrage après la certification : étape À FAIRE (journal_officiel, plan_bornage, journal_cloture, titrage) ou « titre » (terminé).
    # Vide : dossier certifié avant la mise en place du titrage, ou pas encore certifié.
    etape_titre = db.Column(db.String(30), index=True)
    valide_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False)

    parcelle = db.relationship("Parcelle")
    proprietaire = db.relationship("Proprietaire")
    topographe = db.relationship("User", foreign_keys=[topographe_id])
    cree_par = db.relationship("User", foreign_keys=[cree_par_id])
    valide_par = db.relationship("User", foreign_keys=[valide_par_id])
    etapes = db.relationship(
        "EtapeLog", back_populates="dossier", order_by="EtapeLog.id", cascade="all, delete-orphan"
    )
    documents = db.relationship("Document", back_populates="dossier", order_by="Document.id")
    mutations = db.relationship("Mutation", back_populates="dossier", order_by="Mutation.id")
    blocages = db.relationship("Blocage", back_populates="dossier", order_by="Blocage.id")
    geometries = db.relationship("Geometrie", back_populates="dossier", order_by="Geometrie.version")
    # Propriétaires de départ (parcelles issues d'une division ou d'une fusion). Vide = un seul propriétaire, 1/1.
    parts_initiales = db.relationship("PartInitiale", cascade="all, delete-orphan", order_by="PartInitiale.id")
    # Filiation : d'où vient ce dossier, et ce qu'il est devenu
    origines = db.relationship("Filiation", foreign_keys="Filiation.resultat_id", back_populates="resultat", order_by="Filiation.id")
    descendances = db.relationship("Filiation", foreign_keys="Filiation.source_id", back_populates="source", order_by="Filiation.id")

    @property
    def geometrie_courante(self):
        """Tracé en vigueur (la version non remplacée), ou None."""
        return next((g for g in reversed(self.geometries) if g.remplace_at is None), None)

    # --- propriétaires ACTUELS : calculés à partir de l'historique des mutations validées ---
    @property
    def proprietaires_actuels(self):
        from .mutations import etat

        return etat(self).parts

    @property
    def blocages_actifs(self):
        return [b for b in self.blocages if b.leve_at is None]

    @property
    def est_bloque(self):
        """Un problème non levé fige le dossier : aucune écriture (circuit, mutation, correction, impression, dépôt…)."""
        return any(b.leve_at is None for b in self.blocages)

    @property
    def proprietaire_label(self):
        from .mutations import libelle_proprietaires

        return libelle_proprietaires(self)

    @property
    def statut_label(self):
        return STATUTS.get(self.statut, self.statut)


class Document(db.Model):
    """Fichier numérisé rattaché à un dossier. Jamais supprimé physiquement : seulement « retiré »."""

    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    type = db.Column(db.String(60), nullable=False)
    libelle = db.Column(db.String(120))  # nom précisé par l'utilisateur pour un document « Autre »
    nom_original = db.Column(db.String(255), nullable=False)
    cle = db.Column(db.String(255), unique=True, nullable=False)  # clé dans le stockage
    mime = db.Column(db.String(80), nullable=False)
    taille = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False)
    retire_at = db.Column(db.DateTime)
    retire_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    retire_motif = db.Column(db.Text)
    mutation_id = db.Column(db.Integer, db.ForeignKey("mutations.id"), index=True)  # justificatif d'une mutation

    @property
    def intitule(self):
        return self.libelle or self.type

    dossier = db.relationship("Dossier", back_populates="documents")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])
    retire_par = db.relationship("User", foreign_keys=[retire_par_id])


class PartInitiale(db.Model):
    """Propriétaire de départ d'un dossier créé par division ou fusion (avec sa quote-part)."""

    __tablename__ = "parts_initiales"

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    proprietaire_id = db.Column(db.Integer, db.ForeignKey("proprietaires.id"), nullable=False)
    part_num = db.Column(db.Integer, nullable=False)
    part_den = db.Column(db.Integer, nullable=False)

    proprietaire = db.relationship("Proprietaire")


class Filiation(db.Model):
    """Lien entre une parcelle d'origine et la parcelle qui en est issue (division ou fusion)."""

    __tablename__ = "filiations"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    resultat_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)  # division | fusion
    mutation_id = db.Column(db.Integer, db.ForeignKey("mutations.id"))
    libelle = db.Column(db.String(80))  # « Lot 1 » pour une parcelle fille

    source = db.relationship("Dossier", foreign_keys=[source_id], back_populates="descendances")
    resultat = db.relationship("Dossier", foreign_keys=[resultat_id], back_populates="origines")


class Geometrie(db.Model):
    """Tracé cartographique d'un dossier (import KMZ/KML). Versionné : un tracé n'est jamais écrasé,
    il est « remplacé » par une version plus récente, avec motif. L'original est conservé."""

    __tablename__ = "geometries"
    __table_args__ = (db.UniqueConstraint("dossier_id", "version", name="uq_geometrie_dossier_version"),)

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)

    geojson = db.Column(db.Text, nullable=False)  # (Multi)Polygone, longitude/latitude WGS84
    empreinte = db.Column(db.String(64), nullable=False)  # SHA-256 du GeoJSON canonique
    superficie_m2 = db.Column(db.Float, nullable=False)  # mesurée sur l'ellipsoïde
    perimetre_m = db.Column(db.Float, nullable=False)
    centre_lon = db.Column(db.Float, nullable=False)
    centre_lat = db.Column(db.Float, nullable=False)
    # Rectangle englobant : présélection rapide des voisins pour la détection de chevauchements
    min_lon = db.Column(db.Float, nullable=False)
    min_lat = db.Column(db.Float, nullable=False)
    max_lon = db.Column(db.Float, nullable=False)
    max_lat = db.Column(db.Float, nullable=False)

    # Fichier d'origine (conservé tel quel pour Google Earth et comme preuve)
    # Repères du croquis lus dans le fichier (lignes, points, zones annexes nommés) : JSON [{type, nom, coords}]
    elements_json = db.Column(db.Text)

    cle = db.Column(db.String(255), unique=True, nullable=False)
    nom_original = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(80), nullable=False)
    taille = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=maintenant)
    remplace_at = db.Column(db.DateTime, index=True)
    motif_remplacement = db.Column(db.Text)  # motif donné quand CETTE version a été créée pour en remplacer une

    dossier = db.relationship("Dossier", back_populates="geometries")
    uploaded_by = db.relationship("User")


class Mutation(db.Model):
    """Mouvement juridique du terrain (vente, héritage, hypothèque…).

    Append-only : une mutation validée n'est jamais modifiée ni supprimée. Une erreur se corrige
    par une mutation d'annulation. Les mutations validées forment une chaîne : chacune porte
    l'empreinte (HMAC) de la précédente, donc toute altération rétroactive est détectable.
    """

    __tablename__ = "mutations"
    __table_args__ = (db.UniqueConstraint("dossier_id", "seq", name="uq_mutation_dossier_seq"),)

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    type = db.Column(db.String(30), nullable=False)
    statut = db.Column(db.String(20), nullable=False, default="en_attente", index=True)

    date_acte = db.Column(db.Date, nullable=False)
    reference_acte = db.Column(db.String(200))  # notaire, jugement, n° d'acte…
    montant = db.Column(db.BigInteger)  # prix de vente / montant garanti, en KMF
    notes = db.Column(db.Text)
    lie_a_id = db.Column(db.Integer, db.ForeignKey("mutations.id"))  # cible d'une annulation / mainlevée
    details = db.Column(db.Text)  # JSON : parcelles filles d'une division, sources d'une fusion

    saisi_par_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    saisi_at = db.Column(db.DateTime, nullable=False, default=maintenant)
    decision_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    decision_at = db.Column(db.DateTime)
    motif_rejet = db.Column(db.Text)

    # Chaîne (renseignée à la validation seulement)
    seq = db.Column(db.Integer)
    hash_precedent = db.Column(db.String(64))
    hash = db.Column(db.String(64))

    dossier = db.relationship("Dossier", back_populates="mutations")
    parties = db.relationship("MutationPartie", back_populates="mutation", order_by="MutationPartie.id", cascade="all, delete-orphan")
    documents = db.relationship("Document", foreign_keys="Document.mutation_id")
    saisi_par = db.relationship("User", foreign_keys=[saisi_par_id])
    decision_par = db.relationship("User", foreign_keys=[decision_par_id])
    lie_a = db.relationship("Mutation", remote_side=[id], foreign_keys=[lie_a_id])


class MutationPartie(db.Model):
    """Personne concernée par une mutation : cédant, bénéficiaire ou créancier."""

    __tablename__ = "mutation_parties"

    id = db.Column(db.Integer, primary_key=True)
    mutation_id = db.Column(db.Integer, db.ForeignKey("mutations.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)
    proprietaire_id = db.Column(db.Integer, db.ForeignKey("proprietaires.id"), nullable=False, index=True)
    # Quote-part de la parcelle entière (fraction exacte, ex. 1/2) ; vide pour un créancier
    part_num = db.Column(db.Integer)
    part_den = db.Column(db.Integer)

    mutation = db.relationship("Mutation", back_populates="parties")
    proprietaire = db.relationship("Proprietaire")


class DocumentEmis(db.Model):
    """Un exemplaire imprimé d'un document officiel, identifié par son code QR.

    Chaque impression crée un enregistrement distinct (copie n°…), signé par HMAC : on peut
    ainsi révoquer une copie perdue sans invalider les autres, et détecter toute altération.
    """

    __tablename__ = "documents_emis"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(24), unique=True, nullable=False, index=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    type = db.Column(db.String(30), nullable=False)
    copie_n = db.Column(db.Integer, nullable=False)
    snapshot = db.Column(db.Text, nullable=False)  # données imprimées (JSON canonique)
    emis_at = db.Column(db.DateTime, nullable=False)
    emis_par_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    signature = db.Column(db.String(64), nullable=False)
    version_signature = db.Column(db.Integer, nullable=False, default=1)

    revoque_at = db.Column(db.DateTime)
    revoque_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    revoque_motif = db.Column(db.Text)

    nb_verifications = db.Column(db.Integer, nullable=False, default=0)
    derniere_verification_at = db.Column(db.DateTime)

    # Copie du PDF tel qu'il a été imprimé, archivée dans le stockage : le document généré reste consultable à l'identique
    # (vide pour les exemplaires imprimés avant l'archivage).
    pdf_cle = db.Column(db.String(255))
    pdf_sha256 = db.Column(db.String(64))
    # Acte d'une mutation (vente, donation, hypothèque…) : la mutation dont il constate les effets ; vide pour les documents du dossier
    mutation_id = db.Column(db.Integer, db.ForeignKey("mutations.id"), index=True)

    dossier = db.relationship("Dossier")
    mutation = db.relationship("Mutation", foreign_keys=[mutation_id])
    emis_par = db.relationship("User", foreign_keys=[emis_par_id])
    revoque_par = db.relationship("User", foreign_keys=[revoque_par_id])


class Bornage(db.Model):
    """Tracé de BORNAGE d'un dossier (KMZ/KML déposé à l'étape « Plan de bornage » du titrage). Versionné comme le tracé de la parcelle :
    jamais écrasé, remplacé avec un motif ; le fichier d'origine est conservé. Les bornes sont les sommets du contour."""

    __tablename__ = "bornages"
    __table_args__ = (db.UniqueConstraint("dossier_id", "version", name="uq_bornage_dossier_version"),)

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)
    geojson = db.Column(db.Text, nullable=False)
    empreinte = db.Column(db.String(64), nullable=False)
    superficie_m2 = db.Column(db.Float, nullable=False)
    perimetre_m = db.Column(db.Float, nullable=False)
    elements_json = db.Column(db.Text)               # repères nommés du fichier (lignes, points, zones annexes)
    cle = db.Column(db.String(255), unique=True, nullable=False)
    nom_original = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(80), nullable=False)
    taille = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=maintenant)
    remplace_at = db.Column(db.DateTime, index=True)
    motif_remplacement = db.Column(db.Text)

    dossier = db.relationship("Dossier", backref=db.backref("bornages", order_by="Bornage.version"))
    uploaded_by = db.relationship("User")


class EtapeTitrage(db.Model):
    """Étape du titrage accomplie (journal officiel, bornage, journal de clôture, titre) ou retour en arrière. Jamais supprimée."""

    __tablename__ = "etapes_titrage"

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    etape = db.Column(db.String(30), nullable=False)
    type = db.Column(db.String(10), nullable=False, default="fait")  # fait | retour
    reference = db.Column(db.String(120))      # n° de parution, du procès-verbal ou du titre foncier
    date_etape = db.Column(db.Date)
    observations = db.Column(db.Text)          # motif d'un retour, ou remarque
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False)
    annule_at = db.Column(db.DateTime)         # l'étape a été annulée par un retour en arrière

    dossier = db.relationship("Dossier", backref=db.backref("etapes_titrage", order_by="EtapeTitrage.id"))
    user = db.relationship("User")


class EtapeLog(db.Model):
    """Chaque transition du circuit. On ne modifie ni ne supprime jamais une ligne."""

    __tablename__ = "etapes_log"

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    de_statut = db.Column(db.String(30))
    vers_statut = db.Column(db.String(30), nullable=False)
    type = db.Column(db.String(20), nullable=False, default="avance")  # avance | rejet
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    commentaire = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False)

    dossier = db.relationship("Dossier", back_populates="etapes")
    user = db.relationship("User")


class AuditLog(db.Model):
    """Journal technique : qui a fait quoi, quand, depuis quelle IP."""

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(60), nullable=False)
    entite = db.Column(db.String(40))
    entite_id = db.Column(db.Integer)
    ancien = db.Column(db.Text)
    nouveau = db.Column(db.Text)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=maintenant, nullable=False, index=True)

    user = db.relationship("User")


class Blocage(db.Model):
    """Problème signalé sur un dossier : tant qu'un blocage est actif (non levé), le dossier est figé (lecture seule).

    Seul le Directeur le lève, avec un motif et une preuve. Un dossier peut avoir plusieurs blocages à la fois."""

    __tablename__ = "blocages"

    id = db.Column(db.Integer, primary_key=True)
    dossier_id = db.Column(db.Integer, db.ForeignKey("dossiers.id"), nullable=False, index=True)
    type = db.Column(db.String(40), nullable=False)
    description = db.Column(db.Text, nullable=False)
    signale_par_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    signale_at = db.Column(db.DateTime, nullable=False, default=maintenant)
    leve_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    leve_at = db.Column(db.DateTime, index=True)
    motif_levee = db.Column(db.Text)  # la preuve : décision, jugement, pièce fournie…
    preuve_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))

    dossier = db.relationship("Dossier", back_populates="blocages")
    signale_par = db.relationship("User", foreign_keys=[signale_par_id])
    leve_par = db.relationship("User", foreign_keys=[leve_par_id])
    preuve = db.relationship("Document", foreign_keys=[preuve_document_id])

    @property
    def actif(self):
        return self.leve_at is None

    @property
    def type_label(self):
        from .constants import PROBLEMES

        return PROBLEMES.get(self.type, self.type)


class ModificationPersonne(db.Model):
    """Demande de modification de l'état civil d'une personne (nom, pièce d'identité, téléphone…).

    Saisie par le Domaine, appliquée seulement quand le Directeur la valide. Elle vise une personne, pas un dossier :
    à la validation, toutes les fiches qui désignent cette personne (même pièce d'identité) sont mises à jour."""

    __tablename__ = "modifications_personnes"

    id = db.Column(db.Integer, primary_key=True)
    proprietaire_id = db.Column(db.Integer, db.ForeignKey("proprietaires.id"), nullable=False, index=True)
    changements = db.Column(db.Text, nullable=False)  # JSON {champ: [avant, après]}
    motif = db.Column(db.Text, nullable=False)
    statut = db.Column(db.String(20), nullable=False, default="en_attente", index=True)  # en_attente | validee | rejetee
    saisi_par_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    saisi_at = db.Column(db.DateTime, nullable=False, default=maintenant)
    decision_par_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    decision_at = db.Column(db.DateTime)
    motif_rejet = db.Column(db.Text)

    proprietaire = db.relationship("Proprietaire")
    saisi_par = db.relationship("User", foreign_keys=[saisi_par_id])
    decision_par = db.relationship("User", foreign_keys=[decision_par_id])
