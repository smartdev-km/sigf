"""QR code de sécurité : émission signée, vérification publique, révocation.

Principe : le QR ne contient qu'une adresse (…/verifier/<code>), jamais de données.
Le code se compose de 16 caractères aléatoires + 8 caractères de contrôle (HMAC) : une adresse
fabriquée est rejetée sans même consulter la base. Chaque exemplaire imprimé est enregistré
avec une signature HMAC-SHA256 de son contenu ; la page publique la recalcule et signale
toute altération. La signature est versionnée pour permettre plus tard une rotation de clé
ou une signature asymétrique sans casser les QR déjà imprimés.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import constants as C
from . import workflow as W
from .audit import audit
from .auth import roles_required
from .extensions import db
from .models import DocumentEmis, Dossier, maintenant

bp = Blueprint("verif", __name__)

LONGUEUR_ALEATOIRE = 16
LONGUEUR_CONTROLE = 8
LONGUEUR_CODE = LONGUEUR_ALEATOIRE + LONGUEUR_CONTROLE

# États d'un document vérifié
VALIDE, REVOQUE, OBSOLETE, ALTERE, CLOTURE, BLOQUE = "valide", "revoque", "obsolete", "altere", "cloture", "bloque"


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------
def _cle():
    return current_app.config["QR_SECRET"].encode()


def _b32(octets):
    return base64.b32encode(octets).decode().rstrip("=")


def _controle(aleatoire):
    mac = hmac.new(_cle(), b"code:" + aleatoire.encode(), hashlib.sha256).digest()
    return _b32(mac)[:LONGUEUR_CONTROLE]


def nouveau_code():
    aleatoire = _b32(secrets.token_bytes(10))[:LONGUEUR_ALEATOIRE]  # 80 bits
    return aleatoire + _controle(aleatoire)


def normaliser(saisie):
    """Accepte les minuscules, espaces et tirets (saisie manuelle du code imprimé)."""
    return re.sub(r"[^A-Z2-7]", "", (saisie or "").upper())


def code_valide(code):
    """Contrôle de forme + HMAC, sans accès à la base."""
    if len(code) != LONGUEUR_CODE or not re.fullmatch(r"[A-Z2-7]+", code):
        return False
    return hmac.compare_digest(code[LONGUEUR_ALEATOIRE:], _controle(code[:LONGUEUR_ALEATOIRE]))


def formater(code):
    """XXXX-XXXX-XXXX-XXXX-XXXX-XXXX (pour l'impression)."""
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def signer(rec):
    """HMAC-SHA256 de tout ce qui définit l'exemplaire (hors révocation, qui évolue)."""
    elements = [rec.code, rec.dossier_id, rec.type, rec.copie_n, rec.snapshot, rec.emis_at.isoformat(), rec.emis_par_id, rec.version_signature]
    if rec.mutation_id is not None:                        # acte d'une mutation : la mutation fait partie de ce qui est signé
        elements.append(rec.mutation_id)
    contenu = json.dumps(elements, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(_cle(), contenu.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Contenu imprimé
# --------------------------------------------------------------------------
def donnees(d, type_doc):
    """Données du dossier qui figurent sur le document (comparées plus tard à l'état actuel)."""
    p = d.parcelle
    donnees_doc = {
        "document": type_doc,
        "dossier": d.numero,
        "proprietaire": d.proprietaire_label,
        "ile": p.ile.nom,
        "commune": p.commune,
        "village": p.village,
        "reference": p.reference,
        "superficie_m2": p.superficie_m2,
        "type_titre": p.type_titre,
        "rsp": d.rsp,
        "rcp": d.rcp,
    }
    # Le plan croquis porte le tracé : si celui-ci change, les exemplaires déjà imprimés ne sont plus à jour.
    # (Clés ajoutées seulement s'il existe un tracé, pour ne pas invalider les plans imprimés avant.)
    g = d.geometrie_courante if type_doc == "plan_croquis" else None
    if g:
        donnees_doc["trace"] = g.empreinte[:16]
        donnees_doc["superficie_mesuree_m2"] = round(g.superficie_m2)
        if g.elements_json:                      # repères du croquis : ajoutés seulement s'il y en a (plans déjà imprimés non invalidés)
            import hashlib

            donnees_doc["reperes"] = hashlib.sha256(g.elements_json.encode()).hexdigest()[:16]
    if type_doc == "plan_croquis":
        # La description du terrain figure sur le plan : si elle change, les plans déjà imprimés ne sont plus à jour.
        # (Clé ajoutée seulement si quelque chose a été saisi : les plans imprimés avant ne sont pas invalidés.)
        from . import terrain

        description = terrain.description(d)
        if description:
            donnees_doc["description"] = description
    if type_doc == "bornage":
        # Le plan de bornage porte le tracé de bornage et le procès-verbal : tout changement (tracé remplacé, étape refaite) le rend « non à jour ».
        from . import bornage as Bo

        b = Bo.courant(d)
        pv = Bo.proces_verbal(d)
        donnees_doc["bornage"] = b.empreinte[:16] if b else None
        donnees_doc["superficie_bornee_m2"] = round(b.superficie_m2) if b else None
        donnees_doc["pv"] = [pv.reference, pv.date_etape.isoformat() if pv.date_etape else None] if pv else None
    if type_doc == "extrait":
        # L'extrait porte les charges et l'historique des mutations : toute hypothèque, mutation ou correction validée
        # après son impression le rend « non à jour » (la tête de la chaîne signée change).
        from . import historique as H

        donnees_doc["charges"] = len(H.charges_en_vigueur(d))
        donnees_doc["mutations"] = len(H.mutations_du_dossier(d))
        donnees_doc["tete_chaine"] = H.tete_de_chaine(d)
    return donnees_doc


def donnees_mutation(m, type_doc=None):
    """Contenu de l'ACTE d'une mutation (vente, donation, héritage, expropriation, hypothèque, mainlevée), comparé plus tard à l'état actuel."""
    from fractions import Fraction

    from . import historique as H
    from . import mutations as M
    from .models import Filiation, Mutation

    d = m.dossier
    p = d.parcelle
    aire, _ = H.superficie(d)

    def partie(x):
        f = Fraction(x.part_num, x.part_den) if x.part_num else None
        return {
            "role": x.role,
            "nom": x.proprietaire.nom_complet,
            "identite": x.proprietaire.identite,
            "part": M.formater_part(f) if f else None,
            "m2": H.metres_carres(f, aire),
        }

    donnees_doc = {
        "document": type_doc or C.ACTES[m.type][0],
        "mutation": m.type,
        "seq": m.seq,
        "date_acte": m.date_acte.isoformat(),
        "reference": m.reference_acte,
        "montant": m.montant,
        "dossier": d.numero,
        "ile": p.ile.nom,
        "commune": p.commune,
        "village": p.village,
        "superficie_m2": aire,
        "rsp": d.rsp,
        "rcp": d.rcp,
        "parties": [partie(x) for x in m.parties],
        "annulee": Mutation.query.filter(Mutation.lie_a_id == m.id, Mutation.type == C.MUT_ANNULATION, Mutation.statut == C.MS_VALIDEE).first() is not None,
        "empreinte": (m.hash or "")[:16],
    }
    if m.type in C.TRANSFERTS:
        suite = Filiation.query.filter_by(mutation_id=m.id).first()
        donnees_doc["nouveau_dossier"] = suite.resultat.numero if suite else None
    if m.type == C.MUT_HYPOTHEQUE:
        donnees_doc["charge_active"] = any(c.id == m.id for c in M.etat(d).charges)        # une mainlevée la rend « non à jour »
    if m.type == C.MUT_MAINLEVEE and m.lie_a is not None:
        donnees_doc["hypotheque_du"] = m.lie_a.date_acte.isoformat()
        donnees_doc["hypotheque_montant"] = m.lie_a.montant
    return donnees_doc


def _normaliser(contenu):
    """Ne tient pas compte de l'ordre « nom prénom » / « prénom nom » : un document imprimé avant que le nom passe en premier
    (usage comorien) reste « à jour ». Seul un vrai changement de nom ou de prénom le rend « non à jour »."""
    def mots(texte):
        return " ; ".join(" ".join(sorted(segment.split())) for segment in texte.split(" ; ")) if isinstance(texte, str) else texte

    contenu = json.loads(json.dumps(contenu))
    if "proprietaire" in contenu:
        contenu["proprietaire"] = mots(contenu["proprietaire"])
    for x in contenu.get("parties", []):
        x["nom"] = mots(x.get("nom"))
    return contenu


def _json(contenu):
    return json.dumps(contenu, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def emettre(d, type_doc, user, mutation=None):
    """Enregistre un nouvel exemplaire signé (sans commit) et le retourne. `mutation` : acte d'une mutation (vente, hypothèque…)."""
    requete = DocumentEmis.query.filter_by(dossier_id=d.id, type=type_doc)
    if mutation is not None:
        requete = requete.filter(DocumentEmis.mutation_id == mutation.id)            # une numérotation de copies par mutation
    copie = requete.count() + 1
    rec = DocumentEmis(
        code=nouveau_code(),
        dossier_id=d.id,
        type=type_doc,
        copie_n=copie,
        mutation_id=mutation.id if mutation is not None else None,
        snapshot=_json(donnees_mutation(mutation, type_doc) if mutation is not None else donnees(d, type_doc)),
        emis_at=maintenant().replace(microsecond=0),
        emis_par_id=user.id,
        version_signature=1,
        signature="",
    )
    rec.signature = signer(rec)
    db.session.add(rec)
    db.session.flush()
    audit("document_emis", "document_emis", rec.id, nouveau=f"{type_doc} copie {copie}", user=user)
    return rec


def url_verification(code):
    base = current_app.config.get("PUBLIC_BASE_URL") or request.url_root.rstrip("/")
    return f"{base}{url_for('verif.page', code=code)}"


# --------------------------------------------------------------------------
# Évaluation
# --------------------------------------------------------------------------
def evaluer(rec, cache=None):
    """Retourne l'état du document : valide, révoqué, non à jour ou altéré."""
    if not hmac.compare_digest(rec.signature, signer(rec)):
        return ALTERE
    if rec.revoque_at:
        return REVOQUE
    if rec.type in C.TYPES_ACTES:
        return _evaluer_acte(rec, cache)
    if rec.dossier.statut == C.CLOTURE:
        return CLOTURE  # la parcelle a été divisée ou fusionnée : elle n'existe plus
    if rec.dossier.est_bloque:
        return BLOQUE  # dossier suspendu pour examen : on ne dit pas pourquoi au public
    cache = cache if cache is not None else {}
    cle = (rec.dossier_id, rec.type)
    if cle not in cache:
        cache[cle] = _json(_normaliser(donnees(rec.dossier, rec.type)))
    return VALIDE if cache[cle] == _json(_normaliser(json.loads(rec.snapshot))) else OBSOLETE


def _evaluer_acte(rec, cache):
    """Un acte reste authentique même quand sa parcelle a été clôturée par le transfert qu'il constate ; il devient « non à jour »
    si la mutation est annulée, l'hypothèque levée, ou une information imprimée corrigée."""
    if rec.mutation is None:
        return ALTERE
    if rec.dossier.est_bloque:
        return BLOQUE
    cache = cache if cache is not None else {}
    cle = (rec.mutation_id, rec.type)
    if cle not in cache:
        cache[cle] = _json(_normaliser(donnees_mutation(rec.mutation, rec.type)))
    return VALIDE if cache[cle] == _json(_normaliser(json.loads(rec.snapshot))) else OBSOLETE


# --------------------------------------------------------------------------
# Pages publiques
# --------------------------------------------------------------------------
def _reponse(html, code=200):
    r = make_response(html, code)
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Robots-Tag"] = "noindex, nofollow"
    return r


def _est_agent():
    """Agent connecté (hors topographe privé) : il voit en plus le dossier et l'historique d'émission.
    Un visiteur public, ou un topographe privé, a exactement la même page que quiconque scanne le QR."""
    return current_user.is_authenticated and current_user.role != C.TOPO_PRIVE


def _dernieres_verifications(limite=10):
    from . import pdf as Pdf

    q = DocumentEmis.query.join(Dossier, Dossier.id == DocumentEmis.dossier_id).filter(DocumentEmis.derniere_verification_at.isnot(None))
    recs = W.restreindre_ile(q, current_user).order_by(DocumentEmis.derniere_verification_at.desc()).limit(limite).all()
    cache = {}
    return [{"rec": r, "etat": evaluer(r, cache), "titre": Pdf.TITRES.get(r.type, r.type), "code": formater(r.code)} for r in recs]


@bp.route("/verifier")
def accueil():
    saisie = request.args.get("code")
    if saisie:
        return redirect(url_for("verif.page", code=normaliser(saisie) or "invalide"))
    return _reponse(render_template("verifier.html", etat="accueil", agent=_est_agent(), recentes=_dernieres_verifications() if _est_agent() else []))


@bp.route("/verifier/<code>")
def page(code):
    from . import pdf as Pdf

    code = normaliser(code)
    rec = DocumentEmis.query.filter_by(code=code).first() if code_valide(code) else None
    if rec is None:
        # Même réponse pour « mal formé », « fausse signature » et « inconnu » : rien à deviner.
        return _reponse(render_template("verifier.html", etat="inconnu", agent=_est_agent(), recentes=[]), 404)

    etat = evaluer(rec)
    rec.nb_verifications += 1
    rec.derniere_verification_at = maintenant()
    db.session.commit()
    contenu = json.loads(rec.snapshot) if etat != ALTERE else None
    return _reponse(
        render_template(
            "verifier.html",
            etat=etat,
            rec=rec,
            contenu=contenu,
            titre_doc=Pdf.TITRES.get(rec.type, rec.type),
            code_affiche=formater(rec.code),
            agent=_est_agent() and W.peut_voir(rec.dossier, current_user),     # un agent d'une autre île a la page publique
            motif_cloture=(rec.dossier.descendances[0].type if etat == CLOTURE and rec.dossier.descendances else None),
            peut_revoquer=_est_agent() and W.peut_voir(rec.dossier, current_user) and current_user.role in (C.ADMIN, C.DIRECTEUR),
        )
    )


# --------------------------------------------------------------------------
# Révocation (agents)
# --------------------------------------------------------------------------
@bp.route("/emis/<int:rec_id>/revoquer", methods=["POST"])
@roles_required(C.ADMIN, C.DIRECTEUR)
def revoquer(rec_id):
    rec = db.session.get(DocumentEmis, rec_id)
    if rec is None or not W.peut_voir(rec.dossier, current_user):
        abort(404)
    motif = (request.form.get("motif") or "").strip()
    if rec.revoque_at:
        flash("Cet exemplaire est déjà révoqué.", "warning")
    elif not motif:
        flash("Le motif de la révocation est obligatoire.", "danger")
    else:
        rec.revoque_at = maintenant()
        rec.revoque_par_id = current_user.id
        rec.revoque_motif = motif
        audit("document_revoque", "document_emis", rec.id, ancien=f"{rec.type} copie {rec.copie_n}", nouveau=motif)
        db.session.commit()
        flash("Exemplaire révoqué : son QR code ne sera plus reconnu comme valide.", "success")
    return redirect(url_for("main.detail_dossier", dossier_id=rec.dossier_id))
