"""Division et fusion de parcelles.

Division : une parcelle mère donne N parcelles filles. Fusion : plusieurs parcelles en donnent une.
Dans les deux cas :
- la saisie (Domaine) et la validation (Directeur) suivent le même circuit que les autres mutations ;
- les parcelles d'origine sont CLÔTURÉES : elles n'existent plus, mais leur historique est conservé ;
- chaque nouvelle parcelle est un NOUVEAU dossier qui entre dans le circuit (plan croquis, RTC, RTS,
  certificat), car une nouvelle parcelle reçoit de nouveaux numéros ;
- la mutation est inscrite dans la chaîne signée de chaque parcelle d'origine ;
- tout est atomique : la validation crée, clôture et chaîne tout ou rien.

Une division ou une fusion ne s'annule pas : la clôture est définitive. Pour revenir en arrière,
on enregistre une nouvelle opération (par exemple fusionner de nouveau).
"""

import json
import uuid
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import carte as Cm
from . import constants as C
from . import geo
from . import mutations as M
from . import personnes as Pe
from .audit import audit
from .extensions import db
from .models import Dossier, EtapeLog, Filiation, Geometrie, Mutation, Parcelle, PartInitiale, maintenant
from .storage import StorageError, get_storage

bp = Blueprint("partages", __name__)

MAX_ENFANTS = 20
MAX_LIBELLE = 80
TOLERANCE_DEBORD_PCT = 2.0       # une fille peut déborder de la mère de 2 % (imprécision de tracé)
TOLERANCE_DEBORD_M2 = 2.0
TOLERANCE_ECART_FUSION_PCT = 3.0  # le tracé fourni pour la fusion peut différer de 3 % de la somme des sources


# --------------------------------------------------------------------------
# Détails d'une mutation de partage
# --------------------------------------------------------------------------
def details(m):
    return json.loads(m.details) if m.details else {}


def ids_sources(m):
    """Fusion : identifiants de tous les dossiers fusionnés (le dossier initiateur en premier)."""
    return details(m).get("sources", [])


def _meta_trace(resultat, cle, nom, mime, octets):
    import hashlib

    return {
        "cle": cle, "nom": nom, "mime": mime, "taille": len(octets), "sha256": hashlib.sha256(octets).hexdigest(),
        "geojson": resultat.geojson, "empreinte": resultat.empreinte, "superficie_m2": resultat.superficie_m2,
        "perimetre_m": resultat.perimetre_m, "centre": list(resultat.centre), "bbox": list(resultat.bbox),
        "elements": resultat.elements,
    }


def _geometrie_depuis_meta(meta, dossier_id, user_id):
    minx, miny, maxx, maxy = meta["bbox"]
    return Geometrie(
        dossier_id=dossier_id, version=1, geojson=meta["geojson"], empreinte=meta["empreinte"],
        superficie_m2=meta["superficie_m2"], perimetre_m=meta["perimetre_m"],
        centre_lon=meta["centre"][0], centre_lat=meta["centre"][1], min_lon=minx, min_lat=miny, max_lon=maxx, max_lat=maxy,
        cle=meta["cle"], nom_original=meta["nom"], mime=meta["mime"], taille=meta["taille"], sha256=meta["sha256"],
        uploaded_by_id=user_id,
        elements_json=json.dumps(meta["elements"], separators=(",", ":")) if meta.get("elements") else None,
    )


# --------------------------------------------------------------------------
# Contrôles communs
# --------------------------------------------------------------------------
def _autres_en_attente(d, sauf_id=None):
    return Mutation.query.filter(Mutation.dossier_id == d.id, Mutation.statut == C.MS_ATTENTE, Mutation.id != (sauf_id or 0)).count()


def _verifier_libre(d, et, sauf_id=None, attente=True):
    """Pas d'hypothèque active ; à la saisie seulement, pas d'autre mutation en attente
    (à la validation, celles-ci sont rejetées automatiquement par la clôture)."""
    if et.charges:
        raise M.MutationError(f"{d.numero} porte une hypothèque active : faites d'abord une mainlevée.")
    if attente and _autres_en_attente(d, sauf_id):
        raise M.MutationError(f"{d.numero} a une autre mutation en attente de validation : traitez-la d'abord.")


def _controle_division_geometrie(parent, enfants_geo):
    """enfants_geo : [(libelle, shapely)]. Chaque fille dans la mère, sans chevauchement entre elles."""
    g = parent.geometrie_courante
    if g is not None:
        mere = geo.depuis_geojson(g.geojson)
        for libelle, forme in enfants_geo:
            hors = geo.aire_hors(forme, mere)
            if hors > max(TOLERANCE_DEBORD_M2, geo.aire_m2(forme) * TOLERANCE_DEBORD_PCT / 100):
                raise M.MutationError(f"Le tracé de « {libelle} » dépasse de la parcelle mère de {hors:,.0f} m².".replace(",", " "))
    for i, (a, fa) in enumerate(enfants_geo):
        for b, fb in enfants_geo[i + 1:]:
            commun = geo.aire_commune(fa, fb)
            if commun >= geo.SEUIL_CHEVAUCHEMENT_M2 and 100 * commun / (min(geo.aire_m2(fa), geo.aire_m2(fb)) or 1) >= geo.SEUIL_CHEVAUCHEMENT_PCT:
                raise M.MutationError(f"Les tracés de « {a} » et « {b} » se chevauchent ({commun:,.0f} m²).".replace(",", " "))


def _parts_identiques(a, b):
    return {Pe.cle(p): f for p, f in a.parts} == {Pe.cle(p): f for p, f in b.parts}


# --------------------------------------------------------------------------
# Division : saisie
# --------------------------------------------------------------------------
def construire_division(d, user, form, fichiers):
    """Retourne (Mutation, [(cle, octets, mime)] à stocker). Aucun effet de bord avant validation complète."""
    if not M.peut_saisir(user, d):
        raise M.MutationError("Vous ne pouvez pas saisir de mutation sur ce dossier.")
    et = M.etat(d)
    _verifier_libre(d, et)

    jour = M._date_acte(form.get("date_acte"))
    reference = M._texte(form.get("reference_acte"), "La référence de l'acte", 200)
    if not reference:
        raise M.MutationError("La référence de l'acte de partage (plan de division, jugement…) est obligatoire.")

    libelles = form.getlist("e_libelle")
    n = len(libelles)
    if not 2 <= n <= MAX_ENFANTS:
        raise M.MutationError(f"Une division donne entre 2 et {MAX_ENFANTS} parcelles filles.")
    superficies = (form.getlist("e_superficie") + [""] * n)[:n]
    fichiers = (list(fichiers) + [None] * n)[:n]

    noms = [M._texte(x, "Le nom d'une parcelle fille", MAX_LIBELLE) for x in libelles]
    if any(not x for x in noms):
        raise M.MutationError("Chaque parcelle fille doit avoir un nom (ex. : Lot 1).")
    if len({x.lower() for x in noms}) != n:
        raise M.MutationError("Deux parcelles filles portent le même nom.")

    # --- répartition des propriétaires ---
    brutes = {p.id: (form.getlist(f"e_alloc_{p.id}") + [""] * n)[:n] for p, _ in et.parts}
    herite = all(not (v or "").strip() for liste in brutes.values() for v in liste)
    repartition = None
    if not herite:
        repartition = []
        for i in range(n):
            lot = []
            for p, _ in et.parts:
                brut = (brutes[p.id][i] or "").strip()
                if brut:
                    part = M.parse_part(brut, f"La part de {p.nom_complet} dans « {noms[i]} »")
                    lot.append([p.id, part.numerator, part.denominator])
            repartition.append(lot)
        _controler_repartition(repartition, noms, et)

    # --- superficies déclarées, tracés ---
    enfants, a_stocker, formes = [], [], []
    for i in range(n):
        decl = None
        if (superficies[i] or "").strip():
            try:
                decl = float(superficies[i].replace(",", ".").replace(" ", ""))
                if decl <= 0:
                    raise ValueError
            except ValueError:
                raise M.MutationError(f"La superficie déclarée de « {noms[i]} » doit être un nombre positif (m²).") from None
        trace = None
        if fichiers[i] is not None and getattr(fichiers[i], "filename", ""):
            try:
                octets, nom, ext, resultat = Cm.lire_fichier_trace(fichiers[i])
            except geo.GeoError as e:
                raise M.MutationError(f"« {noms[i]} » : {e}") from None
            cle = f"dossiers/{d.id}/partage/{uuid.uuid4().hex}{ext}"
            trace = _meta_trace(resultat, cle, nom, Cm.MIME[ext], octets)
            a_stocker.append((cle, octets, Cm.MIME[ext]))
            formes.append((noms[i], resultat.geometrie))
        enfants.append({"libelle": noms[i], "superficie_declaree": decl, "proprietaires": repartition[i] if repartition else None, "trace": trace})
    _controle_division_geometrie(d, formes)

    m = Mutation(
        dossier_id=d.id, type=C.MUT_DIVISION, statut=C.MS_ATTENTE, date_acte=jour, reference_acte=reference,
        notes=M._texte(form.get("notes"), "Les notes", 4000) or None, saisi_par_id=user.id, saisi_at=maintenant(),
        details=json.dumps({"enfants": enfants}, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )
    return m, a_stocker


def _controler_repartition(repartition, noms, et):
    connus = {p.id for p, _ in et.parts}
    vus = set()
    for lot, nom in zip(repartition, noms):
        if not lot:
            raise M.MutationError(f"« {nom} » n'a aucun propriétaire : renseignez la répartition de chaque parcelle fille.")
        total = sum(M.Fraction(num, den) for _, num, den in lot)
        if total != 1:
            raise M.MutationError(f"Les parts des propriétaires de « {nom} » doivent faire 1 au total (actuellement {M.formater_part(total)}).")
        for pid, _, _ in lot:
            if pid not in connus:
                raise M.MutationError("Un propriétaire de la répartition n'est plus propriétaire de la parcelle mère.")
            vus.add(pid)
    absents = connus - vus
    if absents:
        noms_absents = ", ".join(p.nom_complet for p, _ in et.parts if p.id in absents)
        raise M.MutationError(f"Chaque propriétaire de la parcelle mère doit recevoir au moins une parcelle fille (oubliés : {noms_absents}).")


# --------------------------------------------------------------------------
# Fusion : saisie
# --------------------------------------------------------------------------
def _charger_sources(d, numeros, user):
    sources = [d]
    for brut in numeros:
        numero = (brut or "").strip()
        if not numero:
            continue
        autre = Dossier.query.filter(Dossier.numero.ilike(numero)).first()
        if autre is not None and not _visible(user, autre):
            autre = None                                  # hors de l'île du compte : introuvable
        if autre is None:
            raise M.MutationError(f"Dossier « {numero} » introuvable.")
        if autre.id in {s.id for s in sources}:
            raise M.MutationError(f"Le dossier {autre.numero} est indiqué deux fois.")
        sources.append(autre)
    return sources


def _controler_fusion(sources, sauf_id=None, trace_meta=None, attente=True):
    if len(sources) < 2:
        raise M.MutationError("Une fusion regroupe au moins deux parcelles : indiquez le numéro d'au moins un autre dossier.")
    if len(sources) > MAX_ENFANTS:
        raise M.MutationError(f"Une fusion regroupe au plus {MAX_ENFANTS} parcelles.")
    etats = []
    for s in sources:
        if not M.dossier_mutable(s):
            raise M.MutationError(f"{s.numero} ne peut pas être fusionné (statut « {s.statut_label} »).")
        et = M.etat(s)
        _verifier_libre(s, et, sauf_id if s.id == sources[0].id else None, attente)
        etats.append(et)
    if len({s.parcelle.ile_id for s in sources}) != 1:
        raise M.MutationError("Les parcelles à fusionner doivent être situées sur la même île.")
    for s, et in zip(sources[1:], etats[1:]):
        if not _parts_identiques(etats[0], et):
            raise M.MutationError(
                f"{s.numero} n'a pas exactement les mêmes propriétaires (aux mêmes parts) que {sources[0].numero}. "
                "Les personnes sont reconnues par leur n° de pièce d'identité : vérifiez qu'il est renseigné, "
                "sinon régularisez d'abord par une vente ou un partage."
            )
    formes = [geo.depuis_geojson(s.geometrie_courante.geojson) for s in sources if s.geometrie_courante]
    if len(formes) == len(sources) and not geo.sont_contigues(formes):
        raise M.MutationError("Ces parcelles ne se touchent pas : une fusion exige des parcelles contiguës.")
    if trace_meta and len(formes) == len(sources):
        somme = sum(s.geometrie_courante.superficie_m2 for s in sources)
        ecart = abs(trace_meta["superficie_m2"] - somme) / somme * 100
        if ecart > TOLERANCE_ECART_FUSION_PCT:
            raise M.MutationError(
                f"Le tracé fourni mesure {trace_meta['superficie_m2']:,.0f} m² alors que les parcelles fusionnées totalisent "
                f"{somme:,.0f} m² (écart de {ecart:.1f} % ; maximum {TOLERANCE_ECART_FUSION_PCT:.0f} %).".replace(",", " ")
            )
    return etats


def construire_fusion(d, user, form, fichier):
    """Retourne (Mutation, [(cle, octets, mime)] à stocker)."""
    if not M.peut_saisir(user, d):
        raise M.MutationError("Vous ne pouvez pas saisir de mutation sur ce dossier.")
    jour = M._date_acte(form.get("date_acte"))
    reference = M._texte(form.get("reference_acte"), "La référence de l'acte", 200)
    if not reference:
        raise M.MutationError("La référence de l'acte de fusion est obligatoire.")
    sources = _charger_sources(d, form.getlist("source_numero"), user)
    for s in sources[1:]:
        if not M.peut_consulter(user) or not _visible(user, s):
            raise M.MutationError(f"Dossier « {s.numero} » introuvable.")

    trace_meta, a_stocker = None, []
    if fichier is not None and getattr(fichier, "filename", ""):
        try:
            octets, nom, ext, resultat = Cm.lire_fichier_trace(fichier)
        except geo.GeoError as e:
            raise M.MutationError(f"Tracé de la parcelle fusionnée : {e}") from None
        cle = f"dossiers/{d.id}/partage/{uuid.uuid4().hex}{ext}"
        trace_meta = _meta_trace(resultat, cle, nom, Cm.MIME[ext], octets)
        a_stocker.append((cle, octets, Cm.MIME[ext]))
    _controler_fusion(sources, trace_meta=trace_meta)

    m = Mutation(
        dossier_id=d.id, type=C.MUT_FUSION, statut=C.MS_ATTENTE, date_acte=jour, reference_acte=reference,
        notes=M._texte(form.get("notes"), "Les notes", 4000) or None, saisi_par_id=user.id, saisi_at=maintenant(),
        details=json.dumps({"role": "initiateur", "sources": [s.id for s in sources], "trace": trace_meta},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )
    return m, a_stocker


def _visible(user, dossier):
    from . import workflow as W

    return W.peut_voir(dossier, user)


# --------------------------------------------------------------------------
# Contrôle à la validation (l'état a pu changer depuis la saisie)
# --------------------------------------------------------------------------
def controler(m, et):
    spec = details(m)
    if m.type == C.MUT_DIVISION:
        d = m.dossier
        _verifier_libre(d, et, sauf_id=m.id, attente=False)
        noms = [e["libelle"] for e in spec["enfants"]]
        if all(e["proprietaires"] is not None for e in spec["enfants"]):
            _controler_repartition([e["proprietaires"] for e in spec["enfants"]], noms, et)
        formes = [(e["libelle"], geo.depuis_geojson(e["trace"]["geojson"])) for e in spec["enfants"] if e.get("trace")]
        _controle_division_geometrie(d, formes)
    elif m.type == C.MUT_FUSION:
        sources = [db.session.get(Dossier, i) for i in spec["sources"]]
        _controler_fusion(sources, sauf_id=m.id, trace_meta=spec.get("trace"), attente=False)


# --------------------------------------------------------------------------
# Effets de la validation (appelé par mutations.valider, même transaction)
# --------------------------------------------------------------------------
def _fermer(source, user, m, texte):
    ancien = source.statut
    source.statut = C.CLOTURE
    db.session.add(EtapeLog(dossier=source, de_statut=ancien, vers_statut=C.CLOTURE, user_id=user.id, commentaire=texte))
    # Les mutations encore en attente sur une parcelle qui n'existe plus n'ont plus d'objet
    for autre in Mutation.query.filter(Mutation.dossier_id == source.id, Mutation.statut == C.MS_ATTENTE, Mutation.id != m.id).all():
        autre.statut = C.MS_REJETEE
        autre.decision_par_id = user.id
        autre.decision_at = maintenant().replace(microsecond=0)
        autre.motif_rejet = f"Rejetée automatiquement : {source.numero} a été clôturé ({texte})."


def _nouveau_dossier(modele, user, reference, declaree, parts, notes):
    p = modele.parcelle
    enfant = Dossier(
        origine=C.ORIGINE_NOUVEAU, filiere=C.FILIERE_PUBLIC, statut=C.PLAN_CROQUIS,
        parcelle=Parcelle(reference=reference, ile_id=p.ile_id, commune=p.commune, village=p.village, superficie_m2=declaree, type_titre=p.type_titre),
        proprietaire_id=parts[0][0], cree_par_id=user.id, notes=notes,
    )
    db.session.add(enfant)
    db.session.flush()
    enfant.numero = f"SIGF-{date.today().year}-{enfant.id:05d}"
    for pid, num, den in parts:
        enfant.parts_initiales.append(PartInitiale(proprietaire_id=pid, part_num=num, part_den=den))
    return enfant


def appliquer(m, user, verrouilles):
    """Crée les nouveaux dossiers, clôture les parcelles d'origine et enregistre la filiation."""
    spec = details(m)
    parent = verrouilles[m.dossier_id]

    if m.type == C.MUT_DIVISION:
        parts_mere = [(p.id, f.numerator, f.denominator) for p, f in M.etat(parent).parts]
        for e in spec["enfants"]:
            parts = e["proprietaires"] or parts_mere
            parts = sorted(parts, key=lambda t: (-t[1] / t[2], t[0]))
            nom = f"{parent.parcelle.reference}-{e['libelle']}" if parent.parcelle.reference else e["libelle"]
            enfant = _nouveau_dossier(parent, user, nom[:80], e.get("superficie_declaree"), parts,
                                      f"{e['libelle']} - issu de la division de {parent.numero} ({m.reference_acte})")
            db.session.add(EtapeLog(dossier=enfant, de_statut=None, vers_statut=C.PLAN_CROQUIS, user_id=user.id,
                                    commentaire=f"Créé par division de {parent.numero} (mutation n° {m.seq})"))
            db.session.add(Filiation(source_id=parent.id, resultat_id=enfant.id, type="division", mutation_id=m.id, libelle=e["libelle"]))
            if e.get("trace"):
                db.session.add(_geometrie_depuis_meta(e["trace"], enfant.id, user.id))
            audit("dossier_cree_par_division", "dossier", enfant.id, nouveau=f"{enfant.numero} <- {parent.numero}", user=user)
        _fermer(parent, user, m, f"divisé en {len(spec['enfants'])} parcelles")
        db.session.flush()
        db.session.expire(parent, ["descendances"])  # les liens viennent d'être ajoutés directement : on invalide la liste mise en cache
        return

    # --- fusion ---
    sources = [verrouilles[i] for i in spec["sources"]]
    et = M.etat(parent)
    parts = sorted([(p.id, f.numerator, f.denominator) for p, f in et.parts], key=lambda t: (-t[1] / t[2], t[0]))
    declarees = [s.parcelle.superficie_m2 for s in sources]
    total = sum(declarees) if all(declarees) else None
    enfant = _nouveau_dossier(parent, user, None, total, parts, f"Issu de la fusion de {', '.join(s.numero for s in sources)} ({m.reference_acte})")
    db.session.add(EtapeLog(dossier=enfant, de_statut=None, vers_statut=C.PLAN_CROQUIS, user_id=user.id,
                            commentaire=f"Créé par fusion de {', '.join(s.numero for s in sources)}"))

    trace = spec.get("trace")
    if trace:
        db.session.add(_geometrie_depuis_meta(trace, enfant.id, user.id))
    elif all(s.geometrie_courante for s in sources):
        union = geo.union_parcelles([geo.depuis_geojson(s.geometrie_courante.geojson) for s in sources])
        if union is not None:
            r = geo.resume_geometrie(union)
            cle = f"dossiers/{enfant.id}/partage/{uuid.uuid4().hex}.geojson"
            octets = r.geojson.encode()
            get_storage().put(cle, octets, "application/geo+json")
            meta = _meta_trace(r, cle, f"union-{enfant.numero}.geojson", "application/geo+json", octets)
            db.session.add(_geometrie_depuis_meta(meta, enfant.id, user.id))

    for s in sources:
        if s.id == parent.id:
            mut = m
        else:
            mut = Mutation(
                dossier_id=s.id, type=C.MUT_FUSION, statut=C.MS_ATTENTE, date_acte=m.date_acte, reference_acte=m.reference_acte,
                notes=m.notes, saisi_par_id=m.saisi_par_id, saisi_at=m.saisi_at,
                details=json.dumps({"role": "source", "origine_mutation": m.id, "resultat": enfant.id}, sort_keys=True, separators=(",", ":")),
            )
            db.session.add(mut)
            db.session.flush()
            M.sceller(s, mut, user)
        db.session.add(Filiation(source_id=s.id, resultat_id=enfant.id, type="fusion", mutation_id=mut.id))
        _fermer(s, user, m, f"fusionné dans {enfant.numero}")
    audit("dossier_cree_par_fusion", "dossier", enfant.id, nouveau=f"{enfant.numero} <- {', '.join(s.numero for s in sources)}", user=user)
    db.session.flush()
    for s in sources:
        db.session.expire(s, ["descendances"])
    db.session.expire(enfant, ["origines"])


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _dossier_ou_404(dossier_id):
    d = db.session.get(Dossier, dossier_id)
    if d is None or not M.peut_consulter(current_user) or not _visible(current_user, d):
        abort(404)
    return d


def _enregistrer(d, m, a_stocker, audit_action):
    cles = []
    try:
        for cle, octets, mime in a_stocker:
            get_storage().put(cle, octets, mime)
            cles.append(cle)
        db.session.add(m)
        db.session.flush()
        audit(audit_action, "mutation", m.id, nouveau=f"{m.type} dossier {d.numero}")
        db.session.commit()
    except Exception:
        db.session.rollback()
        for cle in cles:
            get_storage().delete(cle)
        raise


@bp.route("/dossiers/<int:dossier_id>/division/nouvelle", methods=["GET", "POST"])
@login_required
def division(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not M.peut_saisir(current_user, d):
        abort(403)
    et = M.etat(d)

    def page(code=200):
        n = len(request.form.getlist("e_libelle")) or 2
        init = [
            {
                "libelle": request.form.getlist("e_libelle")[i] if i < len(request.form.getlist("e_libelle")) else "",
                "superficie": (request.form.getlist("e_superficie") + [""] * n)[i],
                "alloc": {str(p.id): (request.form.getlist(f"e_alloc_{p.id}") + [""] * n)[i] for p, _ in et.parts},
            }
            for i in range(n)
        ]
        cfg = {"proprietaires": [{"id": p.id, "nom": p.nom_complet, "part": M.formater_part(f)} for p, f in et.parts], "enfants": init}
        return render_template("dossiers/division_form.html", d=d, et=et, f=request.form, cfg=cfg, today=date.today().isoformat(), formater_part=M.formater_part), code

    if request.method == "GET":
        return page()
    try:
        m, a_stocker = construire_division(d, current_user, request.form, request.files.getlist("e_fichier"))
        _enregistrer(d, m, a_stocker, "division_saisie")
    except (M.MutationError, StorageError) as e:
        db.session.rollback()
        flash(str(e), "danger")
        return page(400)
    flash("Division enregistrée : elle attend la validation du Directeur.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#tracabilite")


@bp.route("/dossiers/<int:dossier_id>/fusion/nouvelle", methods=["GET", "POST"])
@login_required
def fusion(dossier_id):
    d = _dossier_ou_404(dossier_id)
    if not M.peut_saisir(current_user, d):
        abort(403)
    et = M.etat(d)

    def page(code=200):
        return render_template("dossiers/fusion_form.html", d=d, et=et, f=request.form, today=date.today().isoformat(),
                               autres=request.form.getlist("source_numero") or [""], formater_part=M.formater_part), code

    if request.method == "GET":
        return page()
    try:
        m, a_stocker = construire_fusion(d, current_user, request.form, request.files.get("fichier_trace"))
        _enregistrer(d, m, a_stocker, "fusion_saisie")
    except (M.MutationError, StorageError) as e:
        db.session.rollback()
        flash(str(e), "danger")
        return page(400)
    flash("Fusion enregistrée : elle attend la validation du Directeur.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=d.id) + "#tracabilite")
