from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import aliased

from . import terrain as Terrain
from . import titrage as Tit
from . import constants as C
from . import arbre as Ab
from . import blocages as B
from . import carte as Cm
from . import corrections as Cor
from . import documents as Doc
from . import generation as Gen
from . import pdf as Pdf
from . import stats as St
from . import mutations as M
from . import verification as V
from . import workflow as W
from .audit import audit
from .extensions import db
from .models import DocumentEmis, Dossier, Ile, Mutation, MutationPartie, Parcelle, Proprietaire, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("main", __name__)


def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    # 404 (et non 403) pour ne pas révéler l'existence d'un dossier hors périmètre
    if d is None or not W.peut_voir(d, current_user):
        abort(404)
    return d


@bp.route("/bienvenue")
@login_required
def bienvenue():
    """Page d'accueil après la connexion : message de bienvenue et accès rapides adaptés au rôle."""
    u = current_user
    if u.role == C.FONCTION_PUBLIQUE:                    # accès limité aux dossiers en titrage
        acces = [
            ("Titrage", "Suivre les dossiers : Journal officiel, bornage, journal de clôture, titre.", "signpost-split", url_for("titrage.page"), Tit.nb_a_traiter(u) or None, True),
            ("Vérifier un document", "Contrôler l'authenticité d'un document par son code QR.", "qr-code-scan", url_for("verif.accueil"), None, False),
            ("Mon profil", "Mes informations et mon mot de passe.", "person-circle", url_for("auth.profil"), None, False),
        ]
        return render_template("bienvenue.html", acces=acces, a_traiter=W.dossiers_a_traiter(u).count(), ile=u.ile, lien_a_traiter=url_for("titrage.page"))
    complet = u.role != C.TOPO_PRIVE                     # le topographe privé n'a ni mutations, ni blocages, ni anciens dossiers (et ne voit que ses propres propriétaires)
    acces = [("Tableau de bord", "Vue d'ensemble : statistiques, dossiers à traiter, activité récente.", "speedometer2", url_for("main.dashboard"), None, True)]
    acces.append(("Dossiers", "Consulter, rechercher et suivre les dossiers dans leur circuit.", "folder2-open", url_for("main.liste_dossiers"), None, False))
    if Tit.peut_consulter(u):
        acces.append(("Titrage", "Journal officiel, plan de bornage, journal de clôture, titre foncier.", "signpost-split", url_for("titrage.page"), Tit.nb_a_traiter(u) or None, False))
    if W.peut_creer_dossier(u):
        acces.append(("Nouveau dossier", "Ouvrir un nouveau dossier foncier.", "plus-circle", url_for("main.nouveau_dossier"), None, False))
    if complet:
        acces.append(("Anciens dossiers", "Les dossiers papier numérisés (reprise de l'existant).", "archive", url_for("main.liste_anciens"), None, False))
    if W.peut_importer_ancien(u):
        acces.append(("Ancien dossier", "Numériser un dossier papier.", "upload", url_for("main.nouvel_ancien_dossier"), None, False))
    if complet:
        acces.append(("Mutations", "Ventes, donations, héritages, hypothèques, divisions…", "arrow-left-right", url_for("mutations.liste"), M.nb_en_attente(u) if u.role == C.DIRECTEUR else None, False))
        acces.append(("Blocages", "Dossiers signalés comme ayant un problème.", "slash-circle", url_for("blocages.liste"), B.nb_actifs(u) or None, False))
    acces.append(("Propriétaires", "Les propriétaires de vos dossiers, leurs parcelles et la carte." if not complet else "Les personnes, leurs biens et leur parcours.", "person-vcard", url_for("proprietaires.liste"), None, False))
    acces.append(("Carte", "Voir toutes les parcelles sur la carte.", "map", url_for("carte.carte_generale"), None, False))
    acces.append(("Vérifier un document", "Contrôler l'authenticité d'un document par son code QR.", "qr-code-scan", url_for("verif.accueil"), None, False))
    if u.role in (C.ADMIN, C.DIRECTEUR):
        acces.append(("Utilisateurs", "Gérer les comptes et consulter leurs mouvements.", "people", url_for("users.liste"), None, False))
    if u.role in (C.ADMIN, C.DIRECTEUR):
        acces.append(("Paramètres", "Ma signature numérique, imprimée sur les certificats." if u.role == C.DIRECTEUR else "Les signatures numériques déposées par chaque Directeur.", "gear", url_for("parametres.page"), None, False))
    acces.append(("Mon profil", "Mes informations et mon mot de passe.", "person-circle", url_for("auth.profil"), None, False))
    return render_template(
        "bienvenue.html",
        acces=acces,
        a_traiter=W.dossiers_a_traiter(u).count(),
        ile=None if u.role == C.ADMIN else u.ile,
    )


@bp.route("/")
@login_required
def dashboard():
    visibles = W.dossiers_visibles(current_user)
    if W.est_restreint(current_user):
        a_traiter = W.dossiers_a_traiter(current_user).order_by(Dossier.id.desc()).limit(10).all()
        # Le topographe privé ne voit que ses dossiers : aucune statistique globale.
        base = visibles.filter(Dossier.avis_envoye_at.is_(None))
        compteurs = [
            ("Plan croquis en cours", base.filter(Dossier.statut == C.PLAN_CROQUIS).count(), "primary"),
            ("En attente d'avis", base.filter(Dossier.statut == C.ATTENTE_AVIS).count(), "warning"),
            ("Avis envoyé", visibles.filter(Dossier.avis_envoye_at.isnot(None)).count(), "success"),
        ]
        return render_template(
            "dashboard.html",
            restreint=True,
            compteurs=compteurs,
            a_traiter=a_traiter,
            recents=visibles.order_by(Dossier.id.desc()).limit(8).all(),
            total=visibles.count(),
        )

    # --- filtres : une île et une période, communs à toutes les statistiques ---
    ref = maintenant()
    iles = W.iles_visibles(current_user)
    ile = request.args.get("ile", type=int)
    ile = ile if ile in {i.id for i in iles} else None
    if W.ile_de(current_user):
        ile = W.ile_de(current_user)                      # compte limité à une île : toutes les statistiques sont celles de son île
    periode = request.args.get("periode", type=int)
    periode = periode if periode in St.PERIODES else 6

    # Les filtres sont en tête de page : TOUT ce qui suit en dépend, y compris la file de travail.
    file_de_travail = W.dossiers_a_traiter(current_user)
    if ile:
        file_de_travail = file_de_travail.join(Parcelle, Parcelle.id == Dossier.parcelle_id).filter(Parcelle.ile_id == ile)
    a_traiter = file_de_travail.order_by(Dossier.id.desc()).limit(10).all()

    activite = St.activite_mensuelle(ile, periode, ref)
    pipeline = St.pipeline(ile)
    iles_stats = [l for l in St.par_ile() if not W.ile_de(current_user) or l["ile"] == ile]
    mutations = St.mutations_par_type(ile, periode, ref)
    debut, fin = St.borne_debut(ref, periode), St.borne_fin(ref)
    delais = St.delais_par_etape(ile, debut, fin)

    def lien(**params):
        return url_for("main.liste_dossiers", **({"ile": ile} if ile else {}), **params)

    donnees = {
        "activite": activite,
        "pipeline": [dict(l, url=lien(statut=l["statut"])) for l in pipeline],
        "iles": [dict(l, actif=(ile is None or l["ile"] == ile)) for l in iles_stats],   # sans lien : nouveaux + anciens dossiers
        "mutations": [dict(l, url=url_for("mutations.liste", type=l["type"])) for l in mutations],
        "delais": delais,
    }
    return render_template(
        "dashboard.html",
        restreint=False,
        a_traiter=a_traiter,
        iles=iles,
        ile=ile,
        periode=periode,
        periodes=St.PERIODES,
        tuiles=St.tuiles(ile, periode, ref),
        retard=St.en_retard(ile, ref),
        nb_attente=St.mutations_en_attente(ile),
        seuil_retard=St.SEUIL_RETARD_JOURS,
        evenements=St.activite_recente(ile, 6),
        donnees=donnees,
        total=visibles.count(),
        ref=ref,
    )


@bp.route("/dossiers")
@login_required
def liste_dossiers():
    q = W.dossiers_visibles(current_user).join(Proprietaire).join(Parcelle)
    statut = request.args.get("statut", "")
    ile_id = request.args.get("ile", type=int)
    recherche = (request.args.get("q") or "").strip()
    restreint = W.est_restreint(current_user)

    # « Dossiers » = les NOUVEAUX dossiers. Les dossiers papier numérisés ont leur propre page (Anciens dossiers).
    q = q.filter(Dossier.origine == C.ORIGINE_NOUVEAU)
    if ile_id:
        q = q.filter(Parcelle.ile_id == ile_id)
    if recherche:
        q = W.appliquer_recherche(q, recherche)

    # Onglets par statut : le nombre de dossiers de chaque onglet suit la recherche et l'île, pas l'onglet choisi.
    # (Le topographe privé n'a pas d'onglets : ils révéleraient l'avancement de dossiers qu'il ne suit plus.)
    comptes = {} if restreint else dict(q.with_entities(Dossier.statut, func.count(Dossier.id)).group_by(Dossier.statut).all())
    if restreint:
        statut = ""
        if not recherche:
            q = q.filter(Dossier.statut != C.CLOTURE)
    else:
        # Pas d'onglet « Tous » : sans onglet choisi, on ouvre le premier qui contient des dossiers (les clôturés seulement en recherche).
        if statut not in C.ONGLETS_DOSSIERS:
            candidats = [k for k in C.ONGLETS_DOSSIERS if recherche or k != C.CLOTURE]
            statut = next((k for k in candidats if comptes.get(k)), C.PLAN_CROQUIS)
        if current_user.role != C.FONCTION_PUBLIQUE:        # la Fonction publique n'a pas d'onglets : elle suit ses dossiers dans « Titrage »
            q = q.filter(Dossier.statut == statut)

    page = request.args.get("page", 1, type=int)
    pagination = q.order_by(Dossier.id.desc()).paginate(page=page, per_page=20, error_out=False)
    statuts = {k: v for k, v in C.STATUTS.items() if k != C.ARCHIVE}
    return render_template(
        "dossiers/liste.html",
        pagination=pagination,
        iles=W.iles_visibles(current_user),
        filtres={"statut": statut if statut in statuts else "", "ile": ile_id, "q": recherche},
        statuts=statuts,
        titrage_libelle=Tit.libelle if Tit.peut_consulter(current_user) else None,
        onglets=None if restreint or current_user.role == C.FONCTION_PUBLIQUE else [(k, v, comptes.get(k, 0)) for k, v in C.ONGLETS_DOSSIERS.items()],
        libelle_onglet=C.ONGLETS_DOSSIERS,
        bloques=B.ids_bloques(d.id for d in pagination.items),
    )


@bp.route("/dossiers/anciens")
@login_required
def liste_anciens():
    """Tous les dossiers papier numérisés (reprise de l'existant), avec le bouton pour en ajouter."""
    if W.est_restreint(current_user):
        abort(404)
    base = W.dossiers_visibles(current_user).filter(Dossier.origine == C.ORIGINE_ANCIEN)
    q = base.join(Proprietaire, Proprietaire.id == Dossier.proprietaire_id).join(Parcelle, Parcelle.id == Dossier.parcelle_id)
    recherche = (request.args.get("q") or "").strip()
    ile_id = request.args.get("ile", type=int)
    statut = request.args.get("statut", "")
    annee = request.args.get("annee", type=int)

    if ile_id:
        q = q.filter(Parcelle.ile_id == ile_id)
    if statut in (C.ARCHIVE, C.CLOTURE):
        q = q.filter(Dossier.statut == statut)
    if annee:
        q = q.filter(Dossier.annee_archive == annee)
    if recherche:
        q = W.appliquer_recherche(q, recherche)

    page = request.args.get("page", 1, type=int)
    pagination = q.order_by(Dossier.id.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "dossiers/liste_anciens.html",
        pagination=pagination,
        iles=W.iles_visibles(current_user),
        filtres={"q": recherche, "ile": ile_id, "statut": statut, "annee": annee},
        total=base.count(),
        nb_scans={d.id: sum(1 for x in d.documents if x.retire_at is None) for d in pagination.items},
        bloques=B.ids_bloques(d.id for d in pagination.items),
    )


def _lire_formulaire(f):
    """Valide la partie propriétaire/parcelle commune aux deux formulaires."""
    nom = (f.get("proprio_nom") or "").strip()
    commune = (f.get("commune") or "").strip()
    ile = db.session.get(Ile, f.get("ile_id", type=int)) if f.get("ile_id", type=int) else None
    superficie = None
    erreurs = []
    if not nom:
        erreurs.append("Le nom du propriétaire est obligatoire.")
    identite = (f.get("proprio_identite") or "").strip()
    if not identite:
        erreurs.append("Le n° de pièce d'identité du propriétaire est obligatoire (CNI, passeport…) : il permet de reconnaître la même personne d'un dossier à l'autre.")
    elif len(identite) > 80:
        erreurs.append("Le n° de pièce d'identité est trop long (80 caractères maximum).")
    if not commune:
        erreurs.append("La commune est obligatoire.")
    if not ile:
        erreurs.append("L'île est obligatoire.")
    elif W.ile_de(current_user) and ile.id != W.ile_de(current_user):
        erreurs.append("Votre compte est limité à son île : vous ne pouvez créer un dossier que sur votre île.")
    if f.get("superficie"):
        try:
            superficie = float(f["superficie"].replace(",", "."))
            if superficie <= 0:
                raise ValueError
        except ValueError:
            erreurs.append("La superficie doit être un nombre positif.")
    if erreurs:
        return erreurs, None, None
    proprietaire = Proprietaire(
        nom=nom,
        prenom=(f.get("proprio_prenom") or "").strip() or None,
        identite=identite,
        telephone=(f.get("proprio_telephone") or "").strip() or None,
        adresse=(f.get("proprio_adresse") or "").strip() or None,
    )
    parcelle = Parcelle(
        reference=(f.get("reference") or "").strip() or None,
        ile=ile,
        commune=commune,
        village=(f.get("village") or "").strip() or None,
        superficie_m2=superficie,
        type_titre=f.get("type_titre") if f.get("type_titre") in C.TYPES_TITRE else None,
    )
    return [], proprietaire, parcelle


def _page_formulaire(ancien, f, code=200):
    return (
        render_template(
            "dossiers/nouveau.html",
            iles=W.iles_visibles(current_user),
            types=C.TYPES_TITRE,
            ancien=ancien,
            f=f,
            natures_terrain=C.NATURES_TERRAIN,
            modes_leve=C.MODES_LEVE,
            champs_description=C.CHAMPS_DESCRIPTION,
        ),
        code,
    )


@bp.route("/dossiers/nouveau", methods=["GET", "POST"])
@login_required
def nouveau_dossier():
    if not W.peut_creer_dossier(current_user):
        abort(403)
    if request.method == "POST":
        f = request.form
        erreurs, proprietaire, parcelle = _lire_formulaire(f)
        if erreurs:
            for e in erreurs:
                flash(e, "danger")
            return _page_formulaire(False, f, 400)
        try:
            description = Terrain.lire(f)                     # description du terrain (plan croquis), saisie dès la création
        except Terrain.DescriptionError as e:
            flash(str(e), "danger")
            return _page_formulaire(False, f, 400)
        d = W.creer_dossier(current_user, parcelle, proprietaire, f.get("notes"))
        Terrain.appliquer_a_la_creation(d, description)
        db.session.commit()
        flash(f"Dossier {d.numero} créé.", "success")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id))
    return _page_formulaire(False, {})


@bp.route("/dossiers/ancien/nouveau", methods=["GET", "POST"])
@login_required
def nouvel_ancien_dossier():
    """Numérisation d'un dossier papier : situation actuelle + scans, hors circuit."""
    if not W.peut_importer_ancien(current_user):
        abort(403)
    if request.method == "POST":
        f = request.form
        erreurs, proprietaire, parcelle = _lire_formulaire(f)
        annee = None
        if f.get("annee"):
            annee = f.get("annee", type=int)
            if annee is None or not 1800 <= annee <= 2100:
                erreurs.append("L'année du dossier papier est invalide.")
        fichiers = []
        if not erreurs:
            try:
                fichiers = Doc.preparer(
                    request.files.getlist("fichiers"),
                    request.form.getlist("type_document"),
                    request.form.getlist("libelle_document"),
                )
            except Doc.DocumentError as e:
                erreurs.append(str(e))
        if erreurs:
            for e in erreurs:
                flash(e, "danger")
            return _page_formulaire(True, f, 400)

        cles = []
        try:
            d = W.creer_ancien_dossier(
                current_user, parcelle, proprietaire, f.get("reference_archive"), annee, f.get("notes")
            )
            Doc.enregistrer(d, fichiers, None, current_user, cles)
            db.session.commit()
        except (Doc.DocumentError, StorageError) as e:
            db.session.rollback()
            Doc.nettoyer(cles)
            flash(str(e), "danger")
            return _page_formulaire(True, f, 400)
        except Exception:
            db.session.rollback()
            Doc.nettoyer(cles)
            raise
        flash(f"Ancien dossier {d.numero} enregistré avec {len(fichiers)} fichier(s).", "success")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id))
    return _page_formulaire(True, {})


@bp.route("/dossiers/<int:dossier_id>")
@login_required
def detail_dossier(dossier_id):
    d = _dossier_ou_404(dossier_id)
    restreint = W.est_restreint(current_user)
    return render_template(
        "dossiers/detail.html",
        d=d,
        nav="anciens" if d.origine == C.ORIGINE_ANCIEN else "",
        mode_correction=Cor.mode(current_user, d),
        restreint=restreint,
        actions=W.actions_disponibles(d, current_user),
        suivi=None if restreint or d.statut in (C.ARCHIVE, C.CLOTURE) else W.etapes_suivi(d),
        documents=Doc.documents_visibles(d, current_user),
        peut_televerser=Doc.peut_televerser(d, current_user),
        peut_retirer=Doc.peut_retirer(current_user),
        imprimable=W.peut_imprimer(d),
        imprimables=W.documents_imprimables(d, current_user),
        emis=_exemplaires_emis(d) if not restreint else [],
        section_documents=(not restreint) and Doc.section_ouverte(d),
        manquants=[] if restreint else Gen.manquants(d),
        peut_generer=(not restreint) and current_user.role in (C.ADMIN, C.DIRECTEUR, C.DOMAINE) and not d.est_bloque,
        proprietaires=M.etat(d).parts,
        carte=_carte_dossier(d),
        trace=_tracabilite(d) if M.peut_consulter(current_user) and (M.dossier_mutable(d) or d.mutations or d.origines or d.descendances) else None,
        peut_revoquer=current_user.role in (C.ADMIN, C.DIRECTEUR),
        titres_pdf=Pdf.TITRES,
        corrige_rsp=W.peut_corriger(d, current_user, "rsp"),
        corrige_rcp=W.peut_corriger(d, current_user, "rcp"),
        statut_visible=W.statut_affiche(d, current_user),
        peut_signaler=B.peut_signaler(current_user, d),
        titrage=Tit.contexte(d, current_user) if Tit.peut_consulter(current_user) else None,
        pieces_avis=Doc.pieces_avis(d),
        peut_titrage=Tit.peut_consulter(current_user),
        fonction_publique=current_user.role == C.FONCTION_PUBLIQUE,
        peut_decrire=Terrain.peut_decrire(d, current_user),
        description=Terrain.description(d),
        valeurs_description=Terrain.description(d),
        natures_terrain=C.NATURES_TERRAIN,
        modes_leve=C.MODES_LEVE,
        champs_description=C.CHAMPS_DESCRIPTION,
        peut_lever=B.peut_lever(current_user),
    )


@bp.route("/dossiers/<int:dossier_id>/action/<action>", methods=["POST"])
@login_required
def action_dossier(dossier_id, action):
    d = _dossier_ou_404(dossier_id)
    cles = []
    try:
        pieces = []
        if action == "envoyer_avis":                      # le topographe privé peut joindre des documents à son avis (facultatif)
            envoyes = [f for f in request.files.getlist("pieces_avis") if f and f.filename]
            if len(envoyes) > Doc.MAX_PIECES_AVIS:
                raise Doc.DocumentError(f"Vous pouvez joindre {Doc.MAX_PIECES_AVIS} documents au maximum à votre avis.")
            if envoyes:
                pieces = Doc.preparer(envoyes, [Doc.TYPE_AVIS] * len(envoyes), [""] * len(envoyes))
        W.executer(
            d,
            current_user,
            action,
            commentaire=request.form.get("commentaire"),
            valeur=request.form.get("valeur"),
        )
        if pieces:
            Doc.enregistrer(d, pieces, Doc.TYPE_AVIS, current_user, cles)
        db.session.commit()
        flash("Étape enregistrée." + (f" {len(pieces)} document{'s' if len(pieces) > 1 else ''} joint{'s' if len(pieces) > 1 else ''} à l'avis." if pieces else ""), "success")
    except (W.WorkflowError, Doc.DocumentError, StorageError) as e:
        db.session.rollback()
        Doc.nettoyer(cles)
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        Doc.nettoyer(cles)
        raise
    return redirect(url_for("main.detail_dossier", dossier_id=d.id))


@bp.route("/dossiers/<int:dossier_id>/corriger/<champ>", methods=["POST"])
@login_required
def corriger_dossier(dossier_id, champ):
    d = _dossier_ou_404(dossier_id)
    try:
        W.corriger_numero(d, current_user, champ, request.form.get("valeur"))
        db.session.commit()
        flash(f"{champ.upper()} corrigé.", "success")
    except W.WorkflowError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id))


@bp.route("/dossiers/<int:dossier_id>/imprimer/<type_doc>")
@login_required
def imprimer(dossier_id, type_doc):
    if type_doc not in Pdf.TITRES:
        abort(404)
    d = _dossier_ou_404(dossier_id)
    if type_doc not in W.documents_imprimables(d, current_user):
        abort(403)
    # Chaque impression est un exemplaire signé, avec son propre QR de vérification
    emis, contenu = Gen.emettre_et_archiver(d, type_doc, current_user)
    audit("impression", "dossier", d.id, nouveau=f"{type_doc} copie {emis.copie_n}")
    db.session.commit()
    return Response(
        contenu,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{type_doc}-{d.numero}.pdf"', "Cache-Control": "no-store"},
    )


@bp.route("/dossiers/<int:dossier_id>/documents-systeme", methods=["POST"])
@login_required
def generer_documents_manquants(dossier_id):
    """Reprise : génère les documents du système qui manquent à un dossier certifié (anciens dossiers certifiés, ou incident)."""
    d = _dossier_ou_404(dossier_id)
    if W.est_restreint(current_user) or current_user.role not in (C.ADMIN, C.DIRECTEUR, C.DOMAINE) or d.est_bloque:
        abort(403)
    a_faire = Gen.manquants(d)
    if not a_faire:
        flash("Tous les documents du dossier sont déjà générés.", "info")
        return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#documents")
    generes, echecs = Gen.generer_documents(d, current_user, a_faire)
    db.session.commit()
    if echecs:
        flash(f"Génération incomplète : {', '.join(Pdf.TITRES[t] for t in echecs)} n'a pas pu être généré. Réessayez ou contactez l'administrateur.", "danger")
    else:
        flash(f"{len(generes)} document{'s' if len(generes) > 1 else ''} généré{'s' if len(generes) > 1 else ''} : {', '.join(Pdf.TITRES[t] for t in generes)}.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#documents")


@bp.route("/emis/<int:rec_id>/pdf")
@login_required
def exemplaire_pdf(rec_id):
    """Rouvre un exemplaire imprimé, tel qu'il a été généré (avec son QR code et son numéro de copie)."""
    rec = db.session.get(DocumentEmis, rec_id)
    if rec is None or W.est_restreint(current_user) or not W.peut_voir(rec.dossier, current_user) or not rec.pdf_cle:
        abort(404)
    try:
        octets = get_storage().lire(rec.pdf_cle)
    except StorageError:
        abort(404)
    audit("document_consulte", "document_emis", rec.id, nouveau=f"{rec.type} copie {rec.copie_n}")
    db.session.commit()
    return Response(
        octets,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{rec.type}-{rec.dossier.numero}-copie{rec.copie_n}.pdf"', "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _exemplaires_emis(d):
    """Exemplaires imprimés du dossier, avec leur état actuel (valide, révoqué, non à jour…)."""
    from .models import Filiation

    cache = {}
    # + les actes de la mutation qui a créé ce dossier (vente, donation… : l'acte est rattaché au dossier d'origine)
    creatrices = [f.mutation_id for f in Filiation.query.filter_by(resultat_id=d.id).all() if f.mutation_id]
    cond = DocumentEmis.dossier_id == d.id
    if creatrices:
        cond = cond | DocumentEmis.mutation_id.in_(creatrices)
    recs = DocumentEmis.query.filter(cond).order_by(DocumentEmis.id.desc()).all()
    return [(r, V.evaluer(r, cache), V.formater(r.code)) for r in recs]


def _tracabilite(d):
    """Données de la section « Traçabilité du terrain » de la fiche dossier."""
    et = M.etat(d)
    integre, defaut = M.verifier_chaine(d)
    arbre = Ab.construire(d)
    historique = M.vue_historique(d)
    return {
        "arbre": arbre,
        "a_histoire": Ab.a_une_histoire(arbre),
        "etat": et,
        "historique": historique,
        "integre": integre,
        "defaut": defaut,
        "peut_saisir": M.peut_saisir(current_user, d),
        "peut_decider": M.peut_decider(current_user),
        "actes": {h["m"].id: Gen.acte_existant(h["m"]) for h in historique if h["m"].statut == C.MS_VALIDEE and h["m"].type in C.ACTES},
        "peut_acte": not d.est_bloque,
        "formater_part": M.formater_part,
        "types": C.MUTATIONS,
        "statuts": C.MUTATION_STATUTS,
        "mutable": M.dossier_mutable(d),
    }


def _carte_dossier(d):
    """Données de la section « Localisation et tracé » de la fiche dossier."""
    g = d.geometrie_courante
    declaree = d.parcelle.superficie_m2
    ecart = None
    if g and declaree:
        ecart = 100.0 * (g.superficie_m2 - declaree) / declaree
    return {
        "g": g,
        "versions": list(reversed(d.geometries)),
        "peut_gerer": Cm.peut_gerer_trace(current_user, d),
        "donnees": Cm.donnees_carte_dossier(d),
        "declaree": declaree,
        "ecart_pct": ecart,
        "ecart_notable": ecart is not None and abs(ecart) > 5,
    }
