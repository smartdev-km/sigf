"""Données de démonstration (personnages et parcelles FICTIFS).

Tout passe par les vraies règles de l'application (circuit, mutations validées, chaîne signée,
division, fusion, QR) : les exemples sont donc cohérents et se comportent comme de vrais dossiers.
Les dossiers créés sont repérables par la mention « [DÉMO] » dans leurs notes.
Commande : flask seed-demo   (refusée sur Render : jamais de données fictives en production)
"""

import hashlib
import io
import math
import zipfile
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from werkzeug.datastructures import FileStorage, MultiDict

from . import carte as Cm
from . import constants as C
from . import documents as Doc
from . import mutations as M
from . import partages
from . import verification as V
from . import workflow as W
from .extensions import db
from .models import Dossier, Ile, Parcelle, Proprietaire, User

MARQUE = "[DÉMO]"

# Points de départ (longitude, latitude) dans chaque île
MORONI = (43.2560, -11.7040)
MITSAMIOULI = (43.2990, -11.3860)
MUTSAMUDU = (44.4005, -12.1670)
DOMONI = (44.5290, -12.2570)
FOMBONI = (43.7160, -12.2800)


class DemoError(Exception):
    pass


# --------------------------------------------------------------------------
# Fabrication de fichiers
# --------------------------------------------------------------------------
def _pts(origine, metres):
    """[(x_m, y_m)] -> [(lon, lat)] (x vers l'est, y vers le sud)."""
    lon0, lat0 = origine
    return [(lon0 + x / (111320.0 * math.cos(math.radians(abs(lat0)))), lat0 - y / 110574.0) for x, y in metres]


def _rect(origine, dx, dy, largeur, hauteur):
    return _pts(origine, [(dx, dy), (dx + largeur, dy), (dx + largeur, dy + hauteur), (dx, dy + hauteur)])


def _kml(*polygones):
    corps = ""
    for i, pts in enumerate(polygones):
        anneau = pts + [pts[0]]
        coords = " ".join(f"{x:.7f},{y:.7f},0" for x, y in anneau)
        corps += f"<Placemark><name>Parcelle {i + 1}</name><Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    return f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>{corps}</Document></kml>'.encode()


def _fichier_trace(nom, pts, kmz=True):
    contenu = _kml(pts)
    if kmz:
        tampon = BytesIO()
        with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", contenu)
        return FileStorage(BytesIO(tampon.getvalue()), filename=f"{nom}.kmz")
    return FileStorage(BytesIO(contenu), filename=f"{nom}.kml")


def _pdf_fictif(titre, lignes):
    tampon = BytesIO()
    c = canvas.Canvas(tampon, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 780, titre)
    c.setFont("Helvetica", 10)
    y = 750
    for ligne in lignes:
        c.drawString(60, y, ligne)
        y -= 16
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(60, 60, "Document fictif généré pour la démonstration de SIGF : aucune valeur juridique.")
    c.save()
    return tampon.getvalue()


def _scan(type_doc, nom_fichier, titre, lignes, libelle=None):
    data = _pdf_fictif(titre, lignes)
    return Doc.Fichier(nom_fichier, data, "application/pdf", ".pdf", hashlib.sha256(data).hexdigest(), type_doc, libelle)


# --------------------------------------------------------------------------
# Acteurs
# --------------------------------------------------------------------------
def _acteurs():
    def premier(role, rang=0):
        users = User.query.filter_by(role=role, actif=True).order_by(User.id).all()
        if len(users) <= rang:
            raise DemoError(f"Il faut au moins {rang + 1} compte(s) actif(s) de rôle « {C.ROLES[role]} ». Créez-les d'abord (menu Utilisateurs).")
        return users[rang]

    return {
        "pub": premier(C.TOPO_PUBLIC),
        "dom": premier(C.DOMAINE),
        "dir": premier(C.DIRECTEUR),
        "priv1": premier(C.TOPO_PRIVE, 0),
        "priv2": premier(C.TOPO_PRIVE, 1),
        "fp": User.query.filter_by(role=C.FONCTION_PUBLIQUE, actif=True).order_by(User.id).first(),   # facultatif : sans compte, pas de titrage démontré
    }


# --------------------------------------------------------------------------
# Opérations réutilisables
# --------------------------------------------------------------------------
def _ile(n):
    return db.session.get(Ile, n)


def _dossier(user, prenom, nom, identite, ile, commune, village, declaree, titre="Titre foncier", telephone=None, adresse=None, ref=None):
    proprietaire = Proprietaire(nom=nom, prenom=prenom, identite=identite, telephone=telephone, adresse=adresse)
    parcelle = Parcelle(reference=ref, ile=_ile(ile), commune=commune, village=village, superficie_m2=declaree, type_titre=titre)
    d = W.creer_dossier(user, parcelle, proprietaire, f"{MARQUE} Exemple fictif")
    db.session.flush()
    return d


def _trace(d, user, nom, pts, motif=None, kmz=True):
    g, _ = Cm.enregistrer_trace(d, user, _fichier_trace(nom, pts, kmz), motif)
    return g


def _avancer(d, a, jusqua):
    """Fait avancer un dossier public jusqu'à l'étape voulue."""
    etapes = ["plan", "rsp", "rcp", "certifie"]
    n = etapes.index(jusqua)
    if n >= 0 and jusqua != "creation":
        W.executer(d, a["pub"], "terminer_plan", commentaire="Levé de terrain effectué")
    if n >= 1:
        W.executer(d, a["pub"], "saisir_rsp", valeur=f"RTC-2026/{d.id:04d}")
    if n >= 2:
        W.executer(d, a["dom"], "saisir_rcp", valeur=f"RTS-2026/{d.id:04d}")
    if n >= 3:
        W.executer(d, a["dir"], "valider", commentaire="Conforme")


def _formulaire(**champs):
    """MultiDict depuis des valeurs simples ou des listes."""
    paires = []
    for k, v in champs.items():
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            paires.append((k, x))
    return MultiDict(paires)


def _muter(d, a, form):
    """Saisie par le Domaine puis validation par le Directeur."""
    m = M.construire(d, a["dom"], form)
    db.session.add(m)
    db.session.flush()
    M.valider(a["dir"], m)
    return m


def _saisir_sans_valider(d, a, form):
    m = M.construire(d, a["dom"], form)
    db.session.add(m)
    db.session.flush()
    return m


def _vendre(d, a, cedant, part, acheteurs, prix, ref, date="2019-03-12"):
    champs = {"type": C.MUT_VENTE, "date_acte": date, "reference_acte": ref, "montant": str(prix), f"cede_{cedant.id}": part}
    champs.update({"b_existant": [""] * len(acheteurs), "b_nom": [x[0] for x in acheteurs], "b_prenom": [x[1] for x in acheteurs],
                   "b_identite": [x[2] for x in acheteurs], "b_telephone": [""] * len(acheteurs), "b_part": [x[3] for x in acheteurs]})
    return _formulaire(**champs)


def _imprimer(d, a, types):
    """Enregistre des exemplaires imprimés (QR) pour que la vérification publique soit testable."""
    codes = {}
    for t in types:
        codes[t] = V.emettre(d, t, a["dir"]).code
    return codes


# --------------------------------------------------------------------------
# Scénarios
# --------------------------------------------------------------------------
def _nouveaux(a, resume):
    # ---- 1. Tout juste créé ----
    d = _dossier(a["pub"], "Mariame", "Soilihi", "CNI-0100101", 1, "Moroni", "Volo-Volo", 600, telephone="+269 333 10 10")
    resume.append((d, "Nouveau · plan croquis en cours (aucun tracé)"))

    # ---- 2. Plan terminé, saisie du RTC ; son tracé chevauche volontairement le dossier n°5 ----
    d = _dossier(a["pub"], "Nassuf", "Abdallah", "CNI-0100102", 1, "Moroni", "Itsandra", 1200)
    _trace(d, a["pub"], "nassuf-abdallah", _rect(MORONI, 25, 5, 50, 30))
    _avancer(d, a, "plan")
    resume.append((d, "Nouveau · saisie du RTC (tracé qui CHEVAUCHE le dossier certifié voisin)"))

    # ---- 3. RTC saisi, enregistrement du RTS (Domaine) ----
    d = _dossier(a["pub"], "Zaharia", "Bacar", "CNI-0100103", 1, "Mitsamiouli", "Bambao", 2500)
    _trace(d, a["pub"], "zaharia-bacar", _pts(MITSAMIOULI, [(0, 0), (60, 0), (70, 40), (30, 55), (-5, 35)]))
    _avancer(d, a, "rsp")
    resume.append((d, "Nouveau · enregistrement du RTS (Domaine), tracé irrégulier"))

    # ---- 4. RTS saisi, validation du Directeur ----
    d = _dossier(a["pub"], "Chamsidine", "Ali", "CNI-0100104", 2, "Mutsamudu", "Hombo", 900)
    _trace(d, a["pub"], "chamsidine-ali", _rect(MUTSAMUDU, 0, 0, 30, 30))
    _avancer(d, a, "rcp")
    resume.append((d, "Nouveau · en attente de validation du Directeur"))

    # ---- 5. Certifié, avec hypothèque active et exemplaires imprimés (QR) ----
    d5 = _dossier(a["pub"], "Fatima", "Said Hamadi", "CNI-0100105", 1, "Moroni", "Volo-Volo", 1200, telephone="+269 333 20 20", adresse="Rue des Cocotiers, Moroni")
    _trace(d5, a["pub"], "fatima-said", _rect(MORONI, 0, 0, 40, 30))
    _avancer(d5, a, "certifie")
    _muter(d5, a, _formulaire(type=C.MUT_HYPOTHEQUE, date_acte="2023-06-05", reference_acte="Acte hypothécaire n°88/2023", creancier_nom="Banque de démonstration", montant="8000000"))
    codes = _imprimer(d5, a, ["plan_croquis", "certificat"])
    resume.append((d5, "Nouveau · CERTIFIÉ, hypothèque active, 2 exemplaires imprimés avec QR", codes))

    # ---- 6. Certifié, puis héritage en indivision (3 héritiers) ----
    d = _dossier(a["pub"], "Ibrahim", "Ahamada", "CNI-0100106", 2, "Mutsamudu", "Mirontsy", 1500)
    _trace(d, a["pub"], "ibrahim-ahamada", _rect(MUTSAMUDU, 200, 0, 50, 40))
    _avancer(d, a, "certifie")
    _muter(d, a, _formulaire(type=C.MUT_HERITAGE, date_acte="2024-02-20", reference_acte="Jugement de succession n°41/2024", **{f"cede_{d.proprietaire_id}": "tout"},
                             b_existant=["", "", ""], b_nom=["Ahamada", "Ahamada", "Ahamada"], b_prenom=["Anfane", "Zalia", "Moinécha"],
                             b_identite=["CNI-0200601", "CNI-0200602", "CNI-0200603"], b_telephone=["", "", ""], b_part=["1/3", "1/3", "1/3"]))
    resume.append((d, "Nouveau · clôturé : propriété transmise par HÉRITAGE"))
    resume.append((d.descendances[0].resultat, "Nouveau · issu de l'HÉRITAGE, en indivision (3 héritiers à 1/3)"))

    # ---- 7. Certifié avec une vente EN ATTENTE de validation (le Directeur peut la traiter) ----
    d = _dossier(a["pub"], "Moussa", "Combo", "CNI-0100107", 3, "Fomboni", "Ouallah", 800)
    _trace(d, a["pub"], "moussa-combo", _rect(FOMBONI, 0, 0, 35, 25))
    _avancer(d, a, "certifie")
    _saisir_sans_valider(d, a, _vendre(d, a, d.proprietaire, "tout", [("Anli", "Bacar", "CNI-0300701", "1")], 4500000, "Acte notarié n°12/2026", date="2026-11-03"))
    resume.append((d, "Nouveau · CERTIFIÉ, VENTE EN ATTENTE de validation du Directeur"))

    # ---- 8. Filière privée : plan terminé, en attente de l'avis du topographe privé ----
    d = _dossier(a["priv1"], "Soulaimana", "Mze", "CNI-0100108", 3, "Fomboni", "Hoani", 1000)
    _trace(d, a["priv1"], "soulaimana-mze", _rect(FOMBONI, 150, 0, 40, 40))
    W.executer(d, a["priv1"], "terminer_plan", commentaire="Plan établi par le cabinet")
    resume.append((d, "Nouveau (filière PRIVÉE) · en attente de l'avis du topographe privé 1"))

    # ---- 9. Filière privée : avis envoyé, le topographe public a repris le dossier ----
    d = _dossier(a["priv2"], "Hadidja", "Madi", "CNI-0100109", 3, "Fomboni", "Nioumachoua", 700)
    _trace(d, a["priv2"], "hadidja-madi", _rect(FOMBONI, 300, 0, 30, 30))
    W.executer(d, a["priv2"], "terminer_plan")
    W.executer(d, a["priv2"], "envoyer_avis", commentaire="Avis favorable : limites matérialisées par des bornes, aucun litige signalé par les voisins.")
    resume.append((d, "Nouveau (filière PRIVÉE) · avis envoyé, saisie du RTC par le topographe public"))


def _division(a, resume):
    d = _dossier(a["pub"], "Abdou", "Mohamed", "CNI-0100110", 1, "Moroni", "Hamramba", 5000, ref="P-0710")
    origine = (MORONI[0] + 0.004, MORONI[1] - 0.004)
    _trace(d, a["pub"], "abdou-mohamed", _rect(origine, 0, 0, 100, 50))
    _avancer(d, a, "certifie")
    ouest, est = _rect(origine, 0.5, 0.5, 48, 49), _rect(origine, 51.5, 0.5, 48, 49)
    form = _formulaire(type=C.MUT_DIVISION, date_acte="2026-09-10", reference_acte="Plan de division n°5/2026",
                       e_libelle=["Lot Ouest", "Lot Est"], e_superficie=["2400", "2400"])
    m, a_stocker = partages.construire_division(d, a["dom"], form, [_fichier_trace("lot-ouest", ouest), _fichier_trace("lot-est", est)])
    _stocker_et_valider(d, a, m, a_stocker)
    resume.append((d, "Nouveau · DIVISÉ en 2 lots (parcelle mère clôturée)"))
    for f in d.descendances:
        resume.append((f.resultat, f"Parcelle fille « {f.libelle} » · issue de la division, entre dans le circuit"))


def _chaine(a, resume):
    """Une parcelle qui a connu deux ventes puis une division : de quoi remplir l'arbre de traçabilité."""
    origine = (MORONI[0] - 0.006, MORONI[1] + 0.006)
    d = _dossier(a["pub"], "Youssouf", "Mdahoma", "CNI-0100112", 1, "Moroni", "Iconi", 1800)
    _trace(d, a["pub"], "youssouf-mdahoma", _rect(origine, 0, 0, 60, 30))
    _avancer(d, a, "certifie")
    _muter(d, a, _vendre(d, a, d.proprietaire, "tout", [("Karim", "Ali", "CNI-0500501", "1")], 6000000, "Acte notarié n°21/2022", date="2022-04-11"))
    d1 = d.descendances[0].resultat
    _muter(d1, a, _vendre(d1, a, d1.proprietaire, "1/2", [("Lamia", "Saïd", "CNI-0500502", "1/2")], 3500000, "Acte notarié n°8/2024", date="2024-09-02"))
    d2 = d1.descendances[0].resultat
    form = _formulaire(type=C.MUT_DIVISION, date_acte="2026-10-20", reference_acte="Plan de division n°9/2026", e_libelle=["Lot Nord", "Lot Sud"], e_superficie=["900", "900"])
    m, a_stocker = partages.construire_division(d2, a["dom"], form, [None, None])
    _stocker_et_valider(d2, a, m, a_stocker)
    resume.append((d, "ARBRE · parcelle d'origine, vendue (clôturée)"))
    resume.append((d1, "ARBRE · 1ʳᵉ vente, puis vente de la moitié (clôturé)"))
    resume.append((d2, "ARBRE · 2 copropriétaires, puis divisé en 2 lots (clôturé)"))
    for f in d2.descendances:
        resume.append((f.resultat, f"ARBRE · « {f.libelle} », issu de la division : ouvrez ce dossier pour voir l'arbre complet"))


def _fusion(a, resume):
    origine = (MORONI[0] + 0.01, MORONI[1] + 0.003)
    d1 = _dossier(a["pub"], "Said", "Ali", "CNI-0100111", 1, "Moroni", "Mtsangani", 1500)
    d2 = _dossier(a["pub"], "Said", "Ali", "CNI-0100111", 1, "Moroni", "Mtsangani", 1500)
    _trace(d1, a["pub"], "said-ali-1", _rect(origine, 0, 0, 50, 30))
    _trace(d2, a["pub"], "said-ali-2", _rect(origine, 50, 0, 50, 30))
    _avancer(d1, a, "certifie")
    _avancer(d2, a, "certifie")
    form = _formulaire(type=C.MUT_FUSION, date_acte="2026-10-01", reference_acte="Acte de fusion n°3/2026", source_numero=[d2.numero])
    m, a_stocker = partages.construire_fusion(d1, a["dom"], form, None)
    _stocker_et_valider(d1, a, m, a_stocker)
    resume.append((d1, "Nouveau · FUSIONNÉ avec le dossier voisin (clôturé)"))
    resume.append((d2, "Nouveau · FUSIONNÉ (clôturé)"))
    resume.append((d1.descendances[0].resultat, "Parcelle issue de la FUSION · entre dans le circuit"))


def _stocker_et_valider(d, a, m, a_stocker):
    for cle, octets, mime in a_stocker:
        Cm.get_storage().put(cle, octets, mime)
    db.session.add(m)
    db.session.flush()
    M.valider(a["dir"], m)


def _titrage(a, resume):
    """Dossiers à chaque étape du titrage : Journal officiel → plan de bornage → journal de clôture → titre foncier."""
    from datetime import date, timedelta
    from . import titrage as T

    if a["fp"] is None:                                  # sans compte « Fonction publique », pas de titrage démontré
        return
    acteurs = {C.FONCTION_PUBLIQUE: a["fp"], C.DOMAINE: a["dom"]}

    def avancer(d, nb):
        for i in range(nb):
            etape = d.etape_titre
            form = {"reference": {"journal_officiel": f"JO-2026/{d.id:03d}", "plan_bornage": f"PV-2026/{d.id:03d}", "journal_cloture": f"JC-2026/{d.id:03d}", "titrage": f"TF-{d.id:05d}"}[etape],
                    "date_etape": (date.today() - timedelta(days=40 - 10 * i)).isoformat()}
            T.avancer(d, acteurs[C.TITRAGE_ACTEUR[etape]], form, {}, [])

    for i, (prenom, nom, village, nb, texte) in enumerate([
        ("Hadidja", "Mmadi", "Hamramba", 0, "Journal officiel à faire"),
        ("Soulé", "Ahamada", "Mitsudje", 1, "plan de bornage à faire"),
        ("Nadjat", "Said", "Ouzioini", 2, "journal de clôture à faire"),
        ("Mariama", "Bacar", "Itsandra", 3, "titre foncier à délivrer"),
        ("Zaïna", "Abdou", "Salimani", 4, "TITRÉ (titrage terminé)"),
    ]):
        d = _dossier(a["pub"], prenom, nom, f"CNI-09009{i:02d}", 1, "Moroni", village, 700)
        _trace(d, a["pub"], f"titrage-{i}", _rect(MORONI, 400 + 80 * i, 300, 40, 30))
        _avancer(d, a, "certifie")
        avancer(d, nb)
        resume.append((d, f"Nouveau · certifié, titrage : {texte}"))


def _anciens(a, resume):
    dom = a["dom"]

    def ancien(prenom, nom, identite, ile, commune, village, declaree, ref, annee, scans, titre="Titre foncier"):
        d = W.creer_ancien_dossier(
            dom, Parcelle(ile=_ile(ile), commune=commune, village=village, superficie_m2=declaree, type_titre=titre),
            Proprietaire(nom=nom, prenom=prenom, identite=identite), ref, annee, f"{MARQUE} Dossier papier numérisé (exemple fictif)")
        db.session.flush()
        cles = []
        Doc.enregistrer(d, scans, None, dom, cles)
        return d

    d = ancien("Ali", "Mohamed Abdou", "CNI-0001987", 1, "Moroni", "Badjanani", 800, "A-1987/045", 1987, [
        _scan("Titre foncier", "titre-A-1987-045.pdf", "TITRE FONCIER n° 1987/045", ["Propriétaire : Ali Mohamed Abdou", "Commune : Moroni (Badjanani)", "Superficie : 800 m²"]),
        _scan("Pièce d'identité", "cni-ali-mohamed.pdf", "CARTE NATIONALE D'IDENTITÉ", ["Titulaire : Ali Mohamed Abdou", "N° CNI-0001987"]),
        _scan("Autre", "bornage-1987.pdf", "PLAN DE BORNAGE 1987", ["Bornage contradictoire du 12/05/1987"], libelle="Plan de bornage 1987"),
    ])
    _trace(d, dom, "ali-mohamed-abdou", _rect(MORONI, -300, 100, 32, 25))
    resume.append((d, "ANCIEN · Moroni, 1987, 3 scans (dont « Plan de bornage 1987 ») et un tracé"))

    d = ancien("Hachim", "Saïd", "CNI-0001992", 2, "Mutsamudu", "Mjimandra", 1100, "B-1992/112", 1992, [
        _scan("Titre foncier", "titre-B-1992-112.pdf", "TITRE FONCIER n° 1992/112", ["Propriétaire : Hachim Saïd", "Mutsamudu (Mjimandra)", "Superficie : 1 100 m²"]),
        _scan("Acte (vente, donation, succession…)", "acte-vente-1992.pdf", "ACTE DE VENTE 1992", ["Vente d'un terrain à Hachim Saïd"]),
    ])
    resume.append((d, "ANCIEN · Mutsamudu, 1992, titre + acte de vente"))

    d = ancien("Rahamata", "Youssouf", "CNI-0002001", 3, "Fomboni", "Miringoni", 650, "C-2001/008", 2001, [
        _scan("Titre foncier", "titre-C-2001-008.pdf", "TITRE FONCIER n° 2001/008", ["Propriétaire : Rahamata Youssouf", "Fomboni (Miringoni)", "Superficie : 650 m²"]),
    ])
    resume.append((d, "ANCIEN · Fomboni, 2001, un seul scan"))

    d = ancien("Soifa", "Ben Ali", "CNI-0001979", 1, "Mitsamiouli", "Ouellah", 2000, "D-1979/021", 1979, [
        _scan("Titre foncier", "titre-D-1979-021.pdf", "TITRE FONCIER n° 1979/021", ["Propriétaire : Soifa Ben Ali"]),
        _scan("Acte (vente, donation, succession…)", "donation-1979.pdf", "ACTE DE DONATION", ["Donation de la parcelle"]),
    ])
    _muter(d, a, _vendre(d, a, d.proprietaire, "1/2", [("Ahmed", "Soifa", "CNI-0400401", "1/2")], 3000000, "Acte notarié n°77/2025", date="2025-08-14"))
    resume.append((d, "ANCIEN · clôturé : vente de la moitié (le nouveau dossier est dans « Dossiers »)"))
    resume.append((d.descendances[0].resultat, "Nouveau · issu de la VENTE d'un ancien dossier (2 copropriétaires)"))

    d = ancien("Mohamed", "Elarif", "CNI-0001995", 2, "Domoni", "Mro Mdjani", 450, "E-1995/230", 1995, [
        _scan("Titre foncier", "titre-E-1995-230.pdf", "TITRE FONCIER n° 1995/230", ["Propriétaire : Mohamed Elarif", "Domoni"]),
    ], titre="Immatriculation")
    resume.append((d, "ANCIEN · Domoni, 1995, sans tracé"))


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------
def deja_present():
    return Dossier.query.filter(Dossier.notes.like(f"%{MARQUE}%")).count()


def semer():
    """Crée les dossiers de démonstration. Retourne la liste [(dossier, description[, codes_qr])]."""
    a = _acteurs()
    resume = []
    for scenario in (_nouveaux, _division, _chaine, _fusion, _titrage, _anciens):
        try:
            scenario(a, resume)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return resume
