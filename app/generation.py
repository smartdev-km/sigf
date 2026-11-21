"""Génération automatique des documents du système (plan croquis, certificat, extrait, attestation).

Dès qu'un NOUVEAU dossier est certifié (validation du Directeur, ou dossier issu d'un transfert de propriété), le système génère
tous ses documents : chacun est un exemplaire n°1 avec son QR code, archivé dans le stockage, et apparaît dans la section « Documents ».
Un échec ne bloque jamais la validation : les documents manquants se régénèrent ensuite depuis la fiche du dossier.
"""

import hashlib

from flask import current_app

from . import constants as C
from . import pdf as Pdf
from . import verification as V
from .audit import audit
from .extensions import db
from .models import DocumentEmis
from .storage import StorageError, get_storage

TYPES_SYSTEME = ("plan_croquis", "certificat", "extrait", "attestation")


def archiver(emis, contenu):
    """Garde le PDF imprimé dans le stockage (consultable à l'identique). Un échec de stockage n'est jamais bloquant."""
    cle = f"documents-emis/{emis.dossier_id}/{emis.code}.pdf"
    try:
        get_storage().put(cle, contenu, "application/pdf")
    except StorageError:
        current_app.logger.exception("Archivage impossible du document émis %s", emis.code)
        return False
    emis.pdf_cle, emis.pdf_sha256 = cle, hashlib.sha256(contenu).hexdigest()
    return True


def emettre_et_archiver(d, type_doc, user, mutation=None):
    """Nouvel exemplaire signé (QR code) du document, PDF généré et archivé. Retourne (exemplaire, octets du PDF).
    `mutation` : pour l'acte d'une mutation (vente, hypothèque…)."""
    emis = V.emettre(d, type_doc, user, mutation=mutation)
    try:
        qr = {"url": V.url_verification(emis.code), "code": V.formater(emis.code), "copie": emis.copie_n}
        contenu = Pdf.generer(type_doc, d, user, qr=qr, mutation=mutation)
    except Exception:
        db.session.delete(emis)                       # pas d'exemplaire fantôme si le PDF n'a pas pu être produit
        db.session.flush()
        raise
    archiver(emis, contenu)
    return emis, contenu


def manquants(d):
    """Documents du système qu'un dossier certifié n'a jamais eus."""
    if d.origine != C.ORIGINE_NOUVEAU or d.statut != C.CERTIFIE:
        return []
    presents = {t for (t,) in db.session.query(DocumentEmis.type).filter(DocumentEmis.dossier_id == d.id).distinct()}
    return [t for t in TYPES_SYSTEME if t not in presents]


def generer_documents(d, user, types=None):
    """Génère les documents manquants (ou ceux demandés). Retourne (générés, échecs)."""
    types = list(types) if types is not None else manquants(d)
    generes, echecs = [], []
    for t in types:
        try:
            emettre_et_archiver(d, t, user)
            generes.append(t)
        except Exception:  # noqa: BLE001 - un document en échec ne doit jamais empêcher les autres, ni la validation
            current_app.logger.exception("Génération impossible : %s du dossier %s", t, d.numero)
            echecs.append(t)
    if generes or echecs:
        audit(
            "documents_generes", "dossier", d.id,
            nouveau=", ".join(Pdf.TITRES[t] for t in generes) + (f" ; ÉCHEC : {', '.join(Pdf.TITRES[t] for t in echecs)}" if echecs else ""),
            user=user,
        )
    return generes, echecs


def automatiques(d, user):
    """Appelé à la certification d'un nouveau dossier : génère tous ses documents (si l'option est active)."""
    if not current_app.config.get("DOCUMENTS_AUTOMATIQUES", True):
        return [], []
    if d.origine != C.ORIGINE_NOUVEAU or d.statut != C.CERTIFIE:
        return [], []
    return generer_documents(d, user)


# --------------------------------------------------------------------------
# Acte d'une mutation : généré quand les parts sont cédées ou la parcelle hypothéquée (à la validation du Directeur)
# --------------------------------------------------------------------------
def code_acte(m):
    """Code du document de l'acte pour cette mutation, ou None (rectification, annulation, division, fusion : pas d'acte)."""
    return C.ACTES[m.type][0] if m.type in C.ACTES else None


def acte_existant(m):
    """Dernier exemplaire archivé et non révoqué de l'acte de cette mutation, ou None."""
    code = code_acte(m)
    if code is None:
        return None
    return (
        DocumentEmis.query.filter(DocumentEmis.mutation_id == m.id, DocumentEmis.type == code, DocumentEmis.revoque_at.is_(None), DocumentEmis.pdf_cle.isnot(None))
        .order_by(DocumentEmis.id.desc())
        .first()
    )


def acte_de(m, user, forcer=False):
    """Acte de la mutation : l'exemplaire déjà généré, sinon un nouveau. Retourne (exemplaire, octets du PDF ou None)."""
    if not forcer:
        deja = acte_existant(m)
        if deja is not None:
            return deja, None
    emis, contenu = emettre_et_archiver(m.dossier, code_acte(m), user, mutation=m)
    return emis, contenu


def pour_mutation(m, user):
    """Appelé à la validation d'une mutation : génère son acte (si l'option est active et si elle en a un). Ne bloque jamais la validation."""
    if not current_app.config.get("DOCUMENTS_AUTOMATIQUES", True) or m.statut != C.MS_VALIDEE or code_acte(m) is None:
        return None
    if DocumentEmis.query.filter_by(mutation_id=m.id).first() is not None:
        return None
    try:
        emis, _ = emettre_et_archiver(m.dossier, code_acte(m), user, mutation=m)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Génération impossible : acte de la mutation %s", m.id)
        audit("documents_generes", "dossier", m.dossier_id, nouveau=f"ÉCHEC : {C.ACTES[m.type][1]} (mutation n° {m.seq})", user=user)
        return None
    audit("documents_generes", "dossier", m.dossier_id, nouveau=f"{C.ACTES[m.type][1]} (mutation n° {m.seq})", user=user)
    return emis
