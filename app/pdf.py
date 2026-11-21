"""Documents officiels imprimables (PDF) : plan croquis, certificat, extrait, attestation.

Mise en page provisoire, à ajuster d'après les modèles officiels. L'emplacement du QR code
de sécurité est réservé sur chaque document ; le QR lui-même sera branché plus tard.
"""

import math
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from flask import current_app
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen.canvas import FILL_EVEN_ODD
from reportlab.platypus import CondPageBreak, Flowable, Image, KeepInFrame, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import constants as C
from . import geo

BLEU = colors.HexColor("#17324d")
GRIS = colors.HexColor("#6b7a8a")

TITRES = {
    "plan_croquis": "Plan croquis",
    "certificat": "Certificat de propriété",
    "extrait": "Extrait du dossier foncier",
    "attestation": "Attestation",
    **C.TYPES_ACTES,                                   # actes des mutations : vente, donation, héritage, expropriation, hypothèque, mainlevée
}


def _t(valeur):
    """Texte sûr pour ReportLab : échappe le balisage et remplace ce que la police ne sait pas écrire."""
    if valeur is None or valeur == "":
        return "-"
    texte = str(valeur).encode("cp1252", "replace").decode("cp1252")
    return escape(texte)


def _style(nom, **kw):
    base = dict(fontName="Helvetica", fontSize=10, leading=14, textColor=colors.black)
    base.update(kw)
    return ParagraphStyle(nom, **base)


S_CENTRE = _style("centre", alignment=TA_CENTER)
S_PAYS = _style("pays", alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=12, textColor=BLEU)
S_DEVISE = _style("devise", alignment=TA_CENTER, fontSize=8, textColor=GRIS)
S_SERVICE = _style("service", alignment=TA_CENTER, fontSize=9)
S_TITRE = _style("titre", alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=17, leading=22, textColor=BLEU)
S_NUM = _style("num", alignment=TA_CENTER, fontSize=9, textColor=GRIS)
S_TEXTE = _style("texte", alignment=TA_JUSTIFY, leading=18, fontSize=11)
S_LABEL = _style("label", fontSize=9, textColor=GRIS)
S_VAL = _style("val", fontName="Helvetica-Bold")
S_PETIT = _style("petit", fontSize=8, textColor=GRIS, leading=10)
S_PETIT_CENTRE = _style("petit_centre", fontSize=8, textColor=GRIS, leading=10, alignment=TA_CENTER)
S_QR = _style("qr", fontSize=6.5, leading=8, alignment=TA_CENTER, textColor=GRIS)
S_QR_CODE = _style("qrcode", fontName="Courier", fontSize=6.5, leading=8, alignment=TA_CENTER)


class CadreQR(Flowable):
    """Emplacement réservé au QR code de sécurité."""

    def __init__(self, taille=3.2 * cm):
        super().__init__()
        self.width = self.height = taille

    def draw(self):
        c = self.canv
        c.setStrokeColor(GRIS)
        c.setDash(3, 3)
        c.rect(0, 0, self.width, self.height)
        c.setDash()
        c.setFillColor(GRIS)
        c.setFont("Helvetica", 7)
        c.drawCentredString(self.width / 2, self.height / 2 + 2, "QR code")
        c.drawCentredString(self.width / 2, self.height / 2 - 8, "de sécurité")


ECHELLES = [100, 200, 250, 500, 1000, 1500, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000]
PTS_PAR_METRE_A_1 = 72 / 2.54 * 100  # 1 m réel à l'échelle 1/1 occupe 2834,6 points sur le papier
MARGE_CROQUIS = 1.2 * cm
BANDE_BAS = 1.1 * cm  # réservée à la barre d'échelle


def _anneaux_voisin(geom):
    """Contours extérieurs d'une géométrie shapely (Polygon ou MultiPolygon), en listes de (lon, lat)."""
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return [list(p.exterior.coords) for p in polys if p.geom_type == "Polygon"]


def _elements_de(g):
    """Repères nommés (lignes, points, zones) lus dans le fichier du tracé."""
    import json

    try:
        texte = getattr(g, "elements_json", None)
        return json.loads(texte) if texte else []
    except ValueError:
        return []


def mise_en_page_croquis(g, largeur, hauteur, voisins=()):
    """Échelle normalisée (1/N) et projection du tracé dans le cadre, ou None sans tracé."""
    if g is None:
        return None
    geom = geo.depuis_geojson(g.geojson)
    minx, miny, maxx, maxy = geom.bounds
    elements = _elements_de(g)
    # le cadre s'élargit pour montrer les repères proches (routes, points…) ; les plus lointains sont simplement coupés par le cadre
    marge_x = max((maxx - minx) * 0.35, 15 / 111320.0)
    marge_y = max((maxy - miny) * 0.35, 15 / 110574.0)
    reperes = [e["coords"] for e in elements]
    for coords in reperes:
        for x, y in coords:
            if minx - marge_x <= x <= maxx + marge_x and miny - marge_y <= y <= maxy + marge_y:
                minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
    lon0, lat0 = (minx + maxx) / 2, (miny + maxy) / 2
    largeur_m = (maxx - minx) * 111320.0 * math.cos(math.radians(lat0))
    hauteur_m = (maxy - miny) * 110574.0
    marge = MARGE_CROQUIS * 1.8 if voisins else MARGE_CROQUIS        # de la place autour de la parcelle pour montrer les voisines
    zone_l = largeur - 2 * marge
    zone_h = hauteur - 2 * marge - BANDE_BAS
    pts_max = min(zone_l / max(largeur_m, 1e-6), zone_h / max(hauteur_m, 1e-6))
    n_min = PTS_PAR_METRE_A_1 / pts_max
    n = next((e for e in ECHELLES if e >= n_min), int(math.ceil(n_min / 100000.0) * 100000))
    return {"geom": geom, "lon0": lon0, "lat0": lat0, "n": n, "pts_par_m": PTS_PAR_METRE_A_1 / n, "elements": elements, "voisins": list(voisins)}


class CadreCroquis(Flowable):
    """Zone de dessin du croquis : tracé de la parcelle à l'échelle, barre d'échelle, flèche du Nord."""

    def __init__(self, largeur, hauteur, g=None, voisins=()):
        super().__init__()
        self.width, self.height = largeur, hauteur
        self.g = g
        self.plan = mise_en_page_croquis(g, largeur, hauteur, voisins)

    def _dessiner_voisins(self, c, pt):
        """Parcelles voisines enregistrées dans le système : gris pointillé, avec leur n° de dossier."""
        if not self.plan["voisins"]:
            return
        c.saveState()
        zone = c.beginPath()
        zone.rect(1, 1, self.width - 2, self.height - 2)
        c.clipPath(zone, stroke=0, fill=0)
        c.setStrokeColor(colors.Color(0.4, 0.4, 0.4))
        c.setFillColor(colors.Color(0.5, 0.5, 0.5, alpha=0.10))
        c.setLineWidth(0.8)
        c.setDash(3, 2)
        for _, geom in self.plan["voisins"]:
            for contour in _anneaux_voisin(geom):
                pts = [pt(x, y) for x, y in contour]
                chemin = c.beginPath()
                chemin.moveTo(*pts[0])
                for x, y in pts[1:-1]:
                    chemin.lineTo(x, y)
                chemin.close()
                c.drawPath(chemin, stroke=1, fill=1)
        c.setDash(1, 0)
        c.setFillColor(colors.Color(0.3, 0.3, 0.3))
        c.setFont("Helvetica", 6.5)
        from shapely.geometry import Polygon as _Poly, box

        visible = box(2, BANDE_BAS + 2, self.width - 2, self.height - 2)
        for numero, geom in self.plan["voisins"]:
            for contour in _anneaux_voisin(geom):
                morceau = _Poly([pt(x, y) for x, y in contour]).buffer(0).intersection(visible)
                if morceau.is_empty or morceau.area < 150:
                    continue
                point = morceau.representative_point()
                largeur_texte = c.stringWidth(numero, "Helvetica", 6.5)
                minx, miny, maxx, maxy = morceau.bounds
                c.saveState()
                c.translate(point.x, point.y)
                if (maxx - minx) < largeur_texte + 6 <= (maxy - miny):          # bande étroite et haute : on écrit à la verticale
                    c.rotate(90)
                c.drawCentredString(0, -2, numero)
                c.restoreState()
                break
        c.restoreState()

    def _dessiner_reperes(self, c, pt):
        """Zones annexes (hachurées), lignes (routes, canaux…) et points nommés, coupés par le cadre."""
        elements = self.plan["elements"]
        if not elements:
            return
        c.saveState()
        zone = c.beginPath()
        zone.rect(1, 1, self.width - 2, self.height - 2)
        c.clipPath(zone, stroke=0, fill=0)
        etiquettes = []
        for e in [e for e in elements if e["type"] == "zone"]:
            pts = [pt(x, y) for x, y in e["coords"]]
            chemin = c.beginPath()
            chemin.moveTo(*pts[0])
            for x, y in pts[1:]:
                chemin.lineTo(x, y)
            chemin.close()
            c.saveState()
            c.clipPath(chemin, stroke=0, fill=0)
            c.setStrokeColor(colors.Color(0.45, 0.45, 0.45))
            c.setLineWidth(0.4)
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            k = min(xs) - (max(ys) - min(ys))
            while k < max(xs):
                c.line(k, min(ys), k + (max(ys) - min(ys)), max(ys))
                k += 4
            c.restoreState()
            c.setStrokeColor(colors.Color(0.35, 0.35, 0.35))
            c.setLineWidth(0.7)
            c.drawPath(chemin, stroke=1, fill=0)
            etiquettes.append(("zone", e["nom"], sum(xs) / len(xs), sum(ys) / len(ys), 0))
        for e in [e for e in elements if e["type"] == "ligne"]:
            pts = [pt(x, y) for x, y in e["coords"]]
            c.setStrokeColor(colors.Color(0.15, 0.15, 0.15))
            c.setLineWidth(0.9)
            c.setDash(1, 0)
            chemin = c.beginPath()
            chemin.moveTo(*pts[0])
            for x, y in pts[1:]:
                chemin.lineTo(x, y)
            c.drawPath(chemin, stroke=1, fill=0)
            # étiquette au milieu du tronçon le plus long, dans le sens de lecture
            i = max(range(len(pts) - 1), key=lambda j: math.hypot(pts[j + 1][0] - pts[j][0], pts[j + 1][1] - pts[j][1]))
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if angle > 90 or angle < -90:
                angle += 180
            etiquettes.append(("ligne", e["nom"], (x1 + x2) / 2, (y1 + y2) / 2, angle))
        for e in [e for e in elements if e["type"] == "point"]:
            x, y = pt(*e["coords"][0])
            c.setStrokeColor(colors.Color(0.15, 0.15, 0.15))
            c.setFillColor(colors.white)
            c.setLineWidth(0.9)
            c.circle(x, y, 2.6, stroke=1, fill=1)
            c.setFillColor(colors.Color(0.15, 0.15, 0.15))
            c.circle(x, y, 0.9, stroke=0, fill=1)
            etiquettes.append(("point", e["nom"], x + 5, y - 2.5, 0))
        c.setFillColor(colors.Color(0.1, 0.1, 0.1))
        c.setFont("Helvetica-Oblique", 7)
        for genre, nom, x, y, angle in etiquettes:
            if not nom:
                continue
            c.saveState()
            c.translate(x, y)
            c.rotate(angle)
            if genre == "point":
                c.drawString(0, 0, nom)
            else:
                c.drawCentredString(0, 2 if genre == "ligne" else -2, nom)
            c.restoreState()
        c.restoreState()

    def _dessiner_trace(self, c):
        p = self.plan
        centre_x = self.width / 2
        centre_y = BANDE_BAS + (self.height - BANDE_BAS) / 2

        def pt(lon, lat):
            x, y = geo.vers_metres(lon, lat, p["lon0"], p["lat0"])
            return centre_x + x * p["pts_par_m"], centre_y + y * p["pts_par_m"]

        self._dessiner_voisins(c, pt)
        self._dessiner_reperes(c, pt)
        c.setStrokeColor(BLEU)
        c.setFillColor(colors.Color(0.05, 0.43, 0.99, alpha=0.12))
        c.setLineWidth(1.4)
        polygones = list(p["geom"].geoms) if p["geom"].geom_type == "MultiPolygon" else [p["geom"]]
        for poly in polygones:
            chemin = c.beginPath()
            for anneau in [poly.exterior, *poly.interiors]:
                coords = list(anneau.coords)
                chemin.moveTo(*pt(*coords[0]))
                for lon, lat in coords[1:-1]:
                    chemin.lineTo(*pt(lon, lat))
                chemin.close()
            c.drawPath(chemin, stroke=1, fill=1, fillMode=FILL_EVEN_ODD)
        # bornes numérotées (A1, A2… si plusieurs parties) et longueur de chaque côté, si le contour reste lisible
        c.setFont("Helvetica", 6.5)
        for partie in geo.bornes(p["geom"]):
            bs = partie["bornes"]
            if len(bs) > 30:
                continue
            pts = [pt(b["lon"], b["lat"]) for b in bs]
            cx = sum(x for x, _ in pts) / len(pts)
            cy = sum(y for _, y in pts) / len(pts)
            for b, (x, y) in zip(bs, pts):
                c.setFillColor(BLEU)
                c.circle(x, y, 1.6, stroke=0, fill=1)
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 6.5)
                c.drawString(x + 2.5, y + 2.5, str(b["n"]))
            if len(bs) <= 12:
                c.setFont("Helvetica", 6)
                c.setFillColor(colors.Color(0.2, 0.2, 0.2))
                for i, cote in enumerate(partie["cotes"]):
                    (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
                    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                    dx, dy = x2 - x1, y2 - y1
                    norme = math.hypot(dx, dy) or 1
                    if norme < 0.9 * cm:
                        continue
                    nx, ny = -dy / norme, dx / norme            # normale ; on la tourne vers l'extérieur du polygone
                    if (mx + nx - cx) ** 2 + (my + ny - cy) ** 2 < (mx - cx) ** 2 + (my - cy) ** 2:
                        nx, ny = -nx, -ny
                    angle = math.degrees(math.atan2(dy, dx))
                    if angle > 90 or angle < -90:
                        angle += 180
                    c.saveState()
                    c.translate(mx + nx * 5, my + ny * 5)
                    c.rotate(angle)
                    c.drawCentredString(0, -2, f"{cote['longueur_m']:.2f} m".replace(".", ","))
                    c.restoreState()

        # barre d'échelle
        longueur_m = max((m for m in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000) if m * p["pts_par_m"] <= 4.5 * cm), default=5)
        x0, y0 = 0.5 * cm, 0.55 * cm
        L = longueur_m * p["pts_par_m"]
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(x0, y0, x0 + L, y0)
        c.line(x0, y0 - 3, x0, y0 + 3)
        c.line(x0 + L, y0 - 3, x0 + L, y0 + 3)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7)
        c.drawString(x0 + L + 4, y0 - 2.5, f"{longueur_m} m   (échelle 1/{p['n']})")
        if self.g:
            c.setFont("Helvetica-Bold", 8)
            c.drawRightString(self.width - 0.4 * cm, 0.35 * cm, f"Superficie mesurée : {self.g.superficie_m2:,.0f} m²".replace(",", " "))

    def draw(self):
        c = self.canv
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.8)
        c.rect(0, 0, self.width, self.height)
        if self.plan:
            self._dessiner_trace(c)
        # flèche du Nord
        x, y = self.width - 1.2 * cm, self.height - 2.2 * cm
        c.line(x, y, x, y + 1.4 * cm)
        c.line(x, y + 1.4 * cm, x - 0.18 * cm, y + 1.0 * cm)
        c.line(x, y + 1.4 * cm, x + 0.18 * cm, y + 1.0 * cm)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(x, y + 1.55 * cm, "N")
        if not self.plan:
            c.setFillColor(GRIS)
            c.setFont("Helvetica", 8)
            c.drawString(0.3 * cm, 0.3 * cm, "Croquis de la parcelle")


def _pied(canvas, doc, d, user, qr=None):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(GRIS)
    canvas.drawString(
        doc.leftMargin,
        1.0 * cm,
        (
            f"Dossier {d.numero} - édité le {datetime.now():%d/%m/%Y à %H:%M} par {user.nom}"
            + (f" - copie n° {qr['copie']}" if qr else "")
        ).encode("cp1252", "replace").decode("cp1252"),
    )
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 1.0 * cm, f"Page {doc.page}")
    canvas.restoreState()


def _entete(titre, d):
    cfg = current_app.config
    return [
        Paragraph(_t(cfg["ENTETE_PAYS"]), S_PAYS),
        Paragraph(_t(cfg["ENTETE_DEVISE"]), S_DEVISE),
        Spacer(1, 4),
        Paragraph(_t(f"{d.parcelle.ile.nom}  -  {cfg['ENTETE_SERVICE']}"), S_SERVICE),
        Spacer(1, 22),
        Paragraph(_t(titre.upper()), S_TITRE),
        Paragraph(_t(f"Dossier n° {d.numero}"), S_NUM),
        Spacer(1, 18),
    ]


def _entete_qr_en_haut(titre, d, qr):
    """En-tête avec le QR code en haut de la page, à droite ; le titre du document vient dessous, sur toute la largeur."""
    cfg = current_app.config
    gauche = [
        Paragraph(_t(cfg["ENTETE_PAYS"]), S_PAYS),
        Paragraph(_t(cfg["ENTETE_DEVISE"]), S_DEVISE),
        Spacer(1, 4),
        Paragraph(_t(f"{d.parcelle.ile.nom}  -  {cfg['ENTETE_SERVICE']}"), S_SERVICE),
    ]
    bloc = _bloc_qr(qr)
    bloc = bloc if isinstance(bloc, list) else [bloc]
    haut = Table([[gauche, bloc]], colWidths=[13 * cm, 4 * cm])
    haut.setStyle(TableStyle([("VALIGN", (0, 0), (0, 0), "MIDDLE"), ("VALIGN", (1, 0), (1, 0), "TOP"), ("ALIGN", (1, 0), (1, 0), "CENTER"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return [
        haut,
        Spacer(1, 8),
        Paragraph(_t(titre.upper()), S_TITRE),
        Paragraph(_t(f"Dossier n° {d.numero}"), S_NUM),
        Spacer(1, 10),
    ]


def _fiche(lignes, marge=5, largeurs=(5, 12)):
    """Tableau « libellé / valeur »."""
    data = [[Paragraph(_t(k), S_LABEL), Paragraph(_t(v), S_VAL)] for k, v in lignes]
    t = Table(data, colWidths=[largeurs[0] * cm, largeurs[1] * cm], hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd8e3")),
                ("TOPPADDING", (0, 0), (-1, -1), marge),
                ("BOTTOMPADDING", (0, 0), (-1, -1), marge),
            ]
        )
    )
    return t


def _superficie(p):
    return f"{p.superficie_m2:,.0f} m²".replace(",", " ") if p.superficie_m2 else None


def _lignes_dossier(d):
    p = d.parcelle
    actuels = d.proprietaires_actuels
    identites = " ; ".join(o.identite for o, _ in actuels if o.identite)
    return [
        ("Propriétaire(s)" if len(actuels) > 1 else "Propriétaire", d.proprietaire_label),
        ("Pièce(s) d'identité" if len(actuels) > 1 else "Pièce d'identité", identites),
        ("Île", p.ile.nom),
        ("Commune", p.commune),
        ("Village / quartier", p.village),
        ("Référence de la parcelle", p.reference),
        ("Superficie", _superficie(p)),
        ("Type de titre", p.type_titre),
        ("N° RTC", d.rsp),
        ("N° RTS", d.rcp),
    ]


def _dessin_qr(url, taille):
    """QR code vectoriel (niveau de correction M) à la taille demandée."""
    widget = QrCodeWidget(url, barLevel="M", barBorder=0)
    x0, y0, x1, y1 = widget.getBounds()
    dessin = Drawing(taille, taille, transform=[taille / (x1 - x0), 0, 0, taille / (y1 - y0), 0, 0])
    dessin.add(widget)
    return dessin


def _bloc_qr(qr):
    if not qr:
        return CadreQR()
    code = qr["code"]
    moitie = len(code) // 2 + 1
    return [
        _dessin_qr(qr["url"], 2.7 * cm),
        Paragraph(_t("Scannez pour vérifier l'authenticité"), S_QR),
        Paragraph(_t(code[:moitie].rstrip("-")), S_QR_CODE),
        Paragraph(_t(code[moitie:]), S_QR_CODE),
    ]


def _image_signature(signature, largeur_max=5.2 * cm):
    """Signature numérique du Directeur (png, mention, nom) -> image à l'échelle dans la zone de signature ; None si illisible."""
    from PIL import Image as PilImage

    try:
        png = signature[0]
        largeur, hauteur = PilImage.open(BytesIO(png)).size
        echelle = min(largeur_max / largeur, 2.1 * cm / hauteur)
        return Image(BytesIO(png), width=largeur * echelle, height=hauteur * echelle)
    except Exception:  # noqa: BLE001 - une signature illisible ne doit jamais empêcher l'impression du document
        return None


def _signatures(libelles, qr=None, avec_qr=True, signature=None, index=0, largeur=17 * cm, en_cellule=False, espace=2.2 * cm):
    """`signature` : (png, mention, nom) du Directeur ; elle remplace l'espace de signature manuscrite du libellé numéro `index`."""
    n = len(libelles)
    largeur_qr = 4 * cm if avec_qr else 0
    colonne = (largeur - largeur_qr) / n
    image = _image_signature(signature, min(5.2 * cm, colonne - 0.6 * cm)) if signature else None
    libelles = list(libelles)
    if image is not None:
        libelles[index] = signature[1]
    cellules = [[Paragraph(_t(lib), S_CENTRE) for lib in libelles] + ([""] if avec_qr else [])]
    zones = [Spacer(1, espace)] * n
    if image is not None:
        image.hAlign = "CENTER"
        zones[index] = image
    cellules.append(zones + ([_bloc_qr(qr)] if avec_qr else []))
    if image is not None:
        noms = [""] * n
        noms[index] = Paragraph(_t(signature[2]), S_PETIT_CENTRE)
        cellules.append(noms + ([""] if avec_qr else []))
    t = Table(cellules, colWidths=[colonne] * n + ([largeur_qr] if avec_qr else []))
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (-1, 1), (-1, 1), "CENTER"), ("VALIGN", (0, 1), (-2 if avec_qr else -1, 1), "MIDDLE"),
                           ("ALIGN", (0, 1), (n - 1, -1), "CENTER")]))
    return t if en_cellule else KeepTogether(t)               # une cellule de tableau ne peut pas contenir de KeepTogether


def _signature_du_directeur(d, user):
    from . import signature

    return signature.pour_impression(d, user)


# ---------------------------------------------------------------- documents
# ------------------------------------------------------------------ plan croquis : une seule feuille A3 (paysage)
PAGE_PLAN = landscape(A3)                      # 42 x 29,7 cm
MARGES_PLAN = {"gauche": 1.5 * cm, "droite": 1.5 * cm, "haut": 1.4 * cm, "bas": 1.8 * cm}
LARGEUR_GAUCHE, ECART, LARGEUR_DROITE = 24 * cm, 0.8 * cm, 14.2 * cm
HAUTEUR_ENTETE_PLAN = 2.6 * cm
HAUTEUR_SIGNATURES = 5.4 * cm
MAX_BORNES_TABLEAU = 60
LIGNES_PAR_TABLEAU_MAX = 12                    # au-delà, les bornes sont réparties sur deux tableaux côte à côte
HAUTEUR_LIGNE_BORNES = 0.5 * cm


def _fmt(v, n=2):
    return f"{v:,.{n}f}".replace(",", " ").replace(".", ",")


def _voisins_pour_dessin(g):
    """[(n° de dossier, géométrie shapely)] des parcelles voisines enregistrées."""
    if g is None:
        return []
    from . import carte

    sortie = []
    for v in carte.voisins_detectes(g):
        gv = v["dossier"].geometrie_courante
        if gv is not None:
            sortie.append((v["dossier"].numero, geo.depuis_geojson(gv.geojson)))
    return sortie


def _titre_section(titre, sous_titre=None):
    elements = [Spacer(1, 9), Paragraph(_t(titre), S_SECTION)]
    if sous_titre:
        elements.append(Paragraph(_t(sous_titre), S_CELLULE_MUTE))
    elements.append(Spacer(1, 3))
    return elements


def _tables_bornes(g):
    """Bornes (UTM 38 Sud), côtés, longueurs et azimuts. Retourne (éléments, hauteur estimée en points)."""
    parties = geo.bornes(geo.depuis_geojson(g.geojson))
    lignes, total = [], 0.0
    for partie in parties:
        total += partie["perimetre_m"]
        for b, c in zip(partie["bornes"], partie["cotes"]):
            lignes.append([
                _p(str(b["n"]), S_CELLULE_GRAS), _p(_fmt(b["est"])), _p(_fmt(b["nord"])),
                _p(f"{c['de']} – {c['vers']}"), _p(_fmt(c["longueur_m"]) + " m"), _p(_fmt(c["azimut"], 1) + "°"),
            ])
    if not lignes:
        return [], 0
    tronque = len(lignes) > MAX_BORNES_TABLEAU
    lignes = lignes[:MAX_BORNES_TABLEAU]
    entetes = ["Borne", "Est (m)", "Nord (m)", "Côté", "Longueur", "Azimut"]
    largeurs = [1.2, 2.6, 2.9, 1.7, 1.9, 1.5]                       # 11,8 cm
    moitie = -(-len(lignes) // 2) if len(lignes) > LIGNES_PAR_TABLEAU_MAX else len(lignes)
    blocs = [lignes[:moitie]] + ([lignes[moitie:]] if lignes[moitie:] else [])
    tableaux = [_tableau(entetes, bloc, largeurs) for bloc in blocs]
    rangee = Table([[tableaux[0], "", tableaux[1] if len(tableaux) > 1 else ""]], colWidths=[11.8 * cm, 0.4 * cm, 11.8 * cm])
    rangee.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    pied = f"Périmètre : <b>{_t(_fmt(total))} m</b>" + (f" &nbsp;·&nbsp; tableau limité aux {MAX_BORNES_TABLEAU} premières bornes" if tronque else "")
    elements = [
        Paragraph(_t("Bornes, côtés et orientations"), S_SECTION),
        Paragraph(_t("Coordonnées en UTM fuseau 38 Sud (WGS 84) ; azimuts depuis le Nord, en degrés"), S_CELLULE_MUTE),
        Spacer(1, 3), rangee, Spacer(1, 3), Paragraph(pied, S_CELLULE),
    ]
    hauteur = 1.3 * cm + (moitie + 1) * HAUTEUR_LIGNE_BORNES + 0.9 * cm
    return elements, hauteur


def _colonne_droite(d, g):
    """Fiche du dossier, limitrophes, voisins détectés, description du terrain, levé, observations."""
    from . import carte, terrain

    lignes = _lignes_dossier(d)
    if g:
        lignes = [("Superficie déclarée", v) if k == "Superficie" else (k, v) for k, v in lignes]
        lignes.insert(6, ("Superficie mesurée (tracé)", f"{g.superficie_m2:,.0f} m²".replace(",", " ")))
    elements = [_fiche(lignes, marge=2.2, largeurs=(4.4, 9.8))]

    elements += _titre_section("Limitrophes", "Indiqués par le topographe")
    elements.append(_fiche([("Nord", d.limitrophe_nord or "—"), ("Sud", d.limitrophe_sud or "—"),
                            ("Est", d.limitrophe_est or "—"), ("Ouest", d.limitrophe_ouest or "—")], marge=2.2, largeurs=(2.4, 11.8)))
    if g:
        voisins = carte.voisins_detectes(g)
        elements += _titre_section("Parcelles voisines enregistrées dans le système", "À la date d'impression")
        if voisins:
            rangees = [[_p(v["cote"]), _p(v["dossier"].numero, S_CELLULE_GRAS), _p(v["dossier"].proprietaire_label or "—"),
                        _p(_fmt(v["distance_m"], 1) + " m"), _p("Chevauche" if v["chevauche"] else "")] for v in voisins]
            elements.append(_tableau(["Côté", "Dossier", "Propriétaire", "Distance", ""], rangees, [1.5, 3.3, 5.6, 1.9, 1.9]))
        else:
            elements.append(Paragraph(_t("Aucune parcelle voisine enregistrée dans le système."), S_VIDE))

    terrain_lignes = [("Nature du terrain", d.nature_terrain), ("Usage / mise en valeur", d.usage_terrain), ("Accès", d.acces_terrain)]
    if any(v for _, v in terrain_lignes):
        elements += _titre_section("Description du terrain")
        elements.append(_fiche([(k, v or "—") for k, v in terrain_lignes], marge=2.2, largeurs=(4.4, 9.8)))
    topo = terrain.topographe_du_dossier(d)
    leve = [("Date du levé", f"{d.date_leve:%d/%m/%Y}" if d.date_leve else None), ("Mode de levé", d.mode_leve),
            ("Topographe", topo.nom if topo and (d.date_leve or d.mode_leve) else None),
            ("N° d'agrément", topo.agrement if topo and (d.date_leve or d.mode_leve) else None)]
    if any(v for _, v in leve):
        elements += _titre_section("Levé")
        elements.append(_fiche([(k, v or "—") for k, v in leve], marge=2.2, largeurs=(4.4, 9.8)))
    if d.observations_plan:
        elements += _titre_section("Observations")
        for ligne in d.observations_plan.split("\n"):
            elements.append(Paragraph(_t(ligne) or "&nbsp;", S_CELLULE))
    return elements


def _entete_plan(d, echelle=None):
    cfg = current_app.config
    gauche = [Paragraph(_t(cfg["ENTETE_PAYS"]), _style("pays_g", fontName="Helvetica-Bold", fontSize=13, textColor=BLEU)),
              Paragraph(_t(cfg["ENTETE_DEVISE"]), _style("devise_g", fontSize=8.5, textColor=GRIS)),
              Spacer(1, 3), Paragraph(_t(f"{d.parcelle.ile.nom}  -  {cfg['ENTETE_SERVICE']}"), _style("service_g", fontSize=9.5))]
    centre = [Paragraph(_t("PLAN CROQUIS"), _style("titre_a3", alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=BLEU)),
              Paragraph(_t(f"Dossier n° {d.numero}"), _style("num_a3", alignment=TA_CENTER, fontSize=11, textColor=GRIS))]
    droite = [Paragraph(_t(f"Échelle : 1/{echelle}" if echelle else "Échelle : 1/ ________"), _style("ech_d", alignment=2, fontName="Helvetica-Bold", fontSize=14, leading=18)),
              Paragraph(_t(f"{d.parcelle.commune}" + (f" · {d.parcelle.village}" if d.parcelle.village else "")), _style("com_d", alignment=2, fontSize=9, textColor=GRIS))]
    t = Table([[gauche, centre, droite]], colWidths=[13 * cm, 13 * cm, 13 * cm], rowHeights=[HAUTEUR_ENTETE_PLAN])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, 0), 1, BLEU),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _plan_croquis(d, qr=None, user=None):
    """Une seule feuille A3 paysage : le croquis à gauche (grand), les bornes sous le croquis, toutes les informations à droite,
    signatures et QR code en bas à droite. Le contenu se réduit au besoin pour ne jamais déborder."""
    g = d.geometrie_courante
    hauteur_corps = PAGE_PLAN[1] - MARGES_PLAN["haut"] - MARGES_PLAN["bas"] - HAUTEUR_ENTETE_PLAN - 0.9 * cm

    bornes, hauteur_bornes = _tables_bornes(g) if g else ([], 0)
    hauteur_cadre = max(hauteur_corps - hauteur_bornes - 0.8 * cm, 11 * cm) if bornes else hauteur_corps - 0.8 * cm
    cadre = CadreCroquis(LARGEUR_GAUCHE, hauteur_cadre, g, _voisins_pour_dessin(g))
    gauche = [cadre]
    if bornes:
        gauche += [Spacer(1, 4)] + bornes
    gauche = KeepInFrame(LARGEUR_GAUCHE, hauteur_corps, gauche, mode="shrink", vAlign="TOP")

    info = KeepInFrame(LARGEUR_DROITE, hauteur_corps - HAUTEUR_SIGNATURES, _colonne_droite(d, g), mode="shrink", vAlign="TOP")
    signatures = _signatures(["Le topographe", "Visa du Domaine"], qr, largeur=LARGEUR_DROITE, en_cellule=True)
    droite = Table([[info], [signatures]], colWidths=[LARGEUR_DROITE], rowHeights=[hauteur_corps - HAUTEUR_SIGNATURES, HAUTEUR_SIGNATURES])
    droite.setStyle(TableStyle([("VALIGN", (0, 0), (-1, 0), "TOP"), ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

    corps = Table([[gauche, "", droite]], colWidths=[LARGEUR_GAUCHE, ECART, LARGEUR_DROITE], rowHeights=[hauteur_corps])
    corps.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return [_entete_plan(d, cadre.plan["n"] if cadre.plan else None), Spacer(1, 0.3 * cm), corps]


def _certificat(d, qr=None, user=None):
    p = d.parcelle
    village = ", village de " + _t(p.village) if p.village else ""
    superficie = ", d'une superficie de " + _t(_superficie(p)) if p.superficie_m2 else ""
    texte = (
        f"Le Directeur certifie que la parcelle de terrain située à {_t(p.commune)}{village}, "
        f"île de {_t(p.ile.nom)}{superficie}, "
        f"enregistrée sous le n° RTC <b>{_t(d.rsp)}</b> et le n° RTS <b>{_t(d.rcp)}</b>, "
        f"est la propriété de <b>{_t(d.proprietaire_label)}</b>."
    )
    valide = (
        f"Validé par {_t(d.valide_par.nom)} le {d.valide_at:%d/%m/%Y}" if d.valide_par and d.valide_at else ""
    )
    return [
        Paragraph(texte, S_TEXTE),
        Spacer(1, 18),
        _fiche(_lignes_dossier(d)),
        Spacer(1, 18),
        Paragraph(_t(valide), S_PETIT),
        Spacer(1, 22),
        _signatures(["Le Directeur"], qr, signature=_signature_du_directeur(d, user)),
    ]


# Ce qui a été FAIT à chaque transition (et non l'étape atteinte)
ACTIONS_ACCOMPLIES = {
    (None, C.PLAN_CROQUIS): "Dossier ouvert",
    (C.PLAN_CROQUIS, C.ATTENTE_AVIS): "Plan croquis terminé",
    (C.PLAN_CROQUIS, C.SAISIE_RSP): "Plan croquis terminé",
    (C.ATTENTE_AVIS, C.SAISIE_RSP): "Avis du topographe privé envoyé",
    (C.SAISIE_RSP, C.ENREGISTREMENT_RCP): "Numéro RTC saisi",
    (C.ENREGISTREMENT_RCP, C.VALIDATION_DIRECTEUR): "Numéro RTS saisi (Domaine)",
    (C.VALIDATION_DIRECTEUR, C.CERTIFIE): "Certificat validé par le Directeur",
}


S_SECTION = _style("section", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=BLEU)
S_CELLULE = _style("cellule", fontSize=8.5, leading=10.5)
S_CELLULE_GRAS = _style("cellule_gras", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5)
S_CELLULE_MUTE = _style("cellule_mute", fontSize=8, leading=10, textColor=GRIS)
S_ENTETE = _style("entete_tab", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=GRIS)
S_VIDE = _style("vide", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor=GRIS)


def _section(titre, sous_titre=None):
    # CondPageBreak : un titre de section n'est jamais seul en bas de page, séparé de son tableau
    elements = [CondPageBreak(4.5 * cm), Spacer(1, 14), Paragraph(_t(titre), S_SECTION)]
    if sous_titre:
        elements.append(Paragraph(_t(sous_titre), S_CELLULE_MUTE))
    elements.append(Spacer(1, 5))
    return elements


def _tableau(entetes, lignes, largeurs, surlignees=()):
    """Tableau à lignes qui passent à la ligne, en-tête répété sur chaque page."""
    donnees = [[Paragraph(_t(e), S_ENTETE) for e in entetes]] + lignes
    t = Table(donnees, colWidths=[l * cm for l in largeurs], repeatRows=1, hAlign="LEFT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd8e3")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for i in surlignees:
        style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f3f8ff")))
    t.setStyle(TableStyle(style))
    return t


def _p(texte, style=S_CELLULE):
    return Paragraph(_t(texte), style)


def _date(d):
    return f"{d:%d/%m/%Y}" if d else "-"


def _extrait(d, qr=None, user=None):
    from . import historique as H

    # ---- charges en vigueur ----
    charges = H.charges_en_vigueur(d)
    if charges:
        bloc_charges = _tableau(
            ["Créancier", "Montant garanti", "Acte du", "Référence de l'acte"],
            [[_p(c["creancier"], S_CELLULE_GRAS), _p(H._montant(c["montant"]) or "-"), _p(_date(c["date_acte"])),
              _p((c["reference"] or "-") + (" (reportée d'un dossier antérieur)" if c["reportee"] else ""))] for c in charges],
            [6.2, 3.3, 2.4, 5.1],
        )
    else:
        bloc_charges = Paragraph(_t("Aucune hypothèque ni charge inscrite sur cette parcelle."), S_VIDE)

    # ---- lignée : origine de propriété ----
    lignee = H.lignee(d)
    if lignee:
        lignes = []
        for l in lignee:
            suite = "<br/>".join(escape(_texte(e)) for e in l["evenements"]) if l["evenements"] else "-"
            dossier = f"<b>{escape(_texte(l['numero']))}</b>" + ("<br/>Ce dossier" if l["courant"] else f"<br/>{escape(_texte(l['statut']))}")
            lignes.append([Paragraph(dossier, S_CELLULE), _p(l["proprietaires"]), Paragraph(suite if not l["courant"] else "-", S_CELLULE)])
        bloc_lignee = _tableau(["Dossier", "Propriétaire(s) à l'époque", "Mutation donnant le dossier suivant"], lignes, [3.4, 5.4, 8.2], surlignees=[len(lignee)])
    else:
        bloc_lignee = Paragraph(_t("Ce dossier est celui de la parcelle d'origine : il ne descend d'aucun autre dossier (ni transfert, ni division, ni fusion)."), S_VIDE)

    # ---- mutations enregistrées sur ce dossier ----
    mutations = H.mutations_du_dossier(d)
    if mutations:
        lignes = []
        for m in mutations:
            nature = m["nature"] + (f" (annulée par n° {m['annulee_par']})" if m["annulee_par"] else "")
            reference = m["reference"] or "-"
            if m["montant"]:
                reference += f" ; {m['montant']}"
            lignes.append([_p(str(m["seq"])), _p(_date(m["date"])), _p(nature, S_CELLULE_GRAS), _p(m["detail"] or "-"), _p(reference), _p(_date(m["validee"]))])
        bloc_mutations = _tableau(["N°", "Acte du", "Nature", "Détail", "Référence ; montant", "Validée le"], lignes, [0.8, 2.1, 2.9, 5.2, 3.9, 2.1])
    else:
        bloc_mutations = Paragraph(_t("Aucune mutation enregistrée sur ce dossier depuis son établissement."), S_VIDE)

    # ---- traitement du dossier (circuit) ----
    historique = [[Paragraph(_t(x), S_ENTETE) for x in ("Date", "Étape", "Par")]]
    for e in d.etapes:
        if e.type in ("avance", "derive"):
            historique.append(
                [
                    Paragraph(_t(f"{e.created_at:%d/%m/%Y}"), S_CELLULE),
                    Paragraph(_t(ACTIONS_ACCOMPLIES.get((e.de_statut, e.vers_statut)) or (f"Dossier créé par {e.commentaire.split(' par ', 1)[1]}" if e.type == "derive" and e.commentaire and " par " in e.commentaire else C.STATUTS.get(e.vers_statut))), S_CELLULE),
                    Paragraph(_t(e.user.nom), S_CELLULE),
                ]
            )
    tableau_circuit = Table(historique, colWidths=[3 * cm, 9 * cm, 5 * cm], repeatRows=1)
    tableau_circuit.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd8e3")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return (
        [_fiche(_lignes_dossier(d), marge=2)]
        + _section("Charges en vigueur")
        + [bloc_charges]
        + _section("Origine de propriété", "Dossiers dont celui-ci descend, du plus ancien au plus récent")
        + [bloc_lignee]
        + _section("Mutations enregistrées sur ce dossier")
        + [bloc_mutations]
        + _section("Historique du traitement")
        + [tableau_circuit, Spacer(1, 12), _signatures(["Le Conservateur"], qr, avec_qr=False, espace=1.7 * cm)]        # le QR code est en haut de la page
    )


def _texte(valeur):
    """Texte compatible avec la police du PDF (les caractères hors cp1252 deviennent « ? »)."""
    return str(valeur or "").encode("cp1252", "replace").decode("cp1252")


def _attestation(d, qr=None, user=None):
    texte = (
        f"Nous soussignés, attestons que le dossier foncier n° <b>{_t(d.numero)}</b>, "
        f"relatif à la parcelle située à {_t(d.parcelle.commune)} ({_t(d.parcelle.ile.nom)}), "
        f"enregistrée sous les n° RTC <b>{_t(d.rsp)}</b> et RTS <b>{_t(d.rcp)}</b>, "
        f"est établi au nom de <b>{_t(d.proprietaire_label)}</b>."
        "<br/><br/>La présente attestation est délivrée à l'intéressé(e) pour servir et valoir ce que de droit."
    )
    return [
        Paragraph(texte, S_TEXTE),
        Spacer(1, 26),
        Paragraph(_t(f"Fait à {d.parcelle.ile.nom}, le {datetime.now():%d/%m/%Y}"), S_CENTRE),
        Spacer(1, 22),
        _signatures(["Le Directeur"], qr, signature=_signature_du_directeur(d, user)),
    ]


def _m2(valeur):
    from .historique import formater_m2

    return formater_m2(valeur)


def _montant(valeur):
    """« 5 000 000 KMF » avec des espaces insécables : le prix ne se coupe jamais en fin de ligne."""
    return f"{valeur:,}".replace(",", "\u00a0") + "\u00a0KMF" if valeur else None


def _date_fr(iso):
    return "/".join(reversed(iso.split("-")))


ROLES_ACTE = {C.ROLE_CEDANT: "Cédant", C.ROLE_BENEFICIAIRE: "Bénéficiaire", C.ROLE_CREANCIER: "Créancier"}
VERBES_ACTE = {
    C.MUT_VENTE: "vendue",
    C.MUT_DONATION: "donnée",
    C.MUT_HERITAGE: "transmise par succession",
    C.MUT_EXPROPRIATION: "transférée par expropriation",
}


def _noms(parties, role):
    return ", ".join(p["nom"] for p in parties if p["role"] == role)


def _acte(m, d, qr=None, user=None):
    """Acte d'une mutation : généré par le système à partir des données signées de la mutation validée."""
    from . import mutations as M
    from . import verification as V

    c = V.donnees_mutation(m)
    p = d.parcelle
    village = ", village de " + _t(c["village"]) if c["village"] else ""
    superficie = f", d'une superficie de {_m2(c['superficie_m2'])}" if c["superficie_m2"] else ""
    parcelle = f"la parcelle de terrain située à {_t(c['commune'])}{village}, île de {_t(c['ile'])}{superficie}, enregistrée sous le n° RTC <b>{_t(c['rsp'])}</b> et le n° RTS <b>{_t(c['rcp'])}</b>"
    jour, ref = _date_fr(c["date_acte"]), _t(c["reference"] or "sans référence")
    cedants, beneficiaires = _noms(c["parties"], C.ROLE_CEDANT), _noms(c["parties"], C.ROLE_BENEFICIAIRE)
    creancier = _noms(c["parties"], C.ROLE_CREANCIER)
    proprietaires = ", ".join(pp.nom_complet for pp, _ in M.etat(d).parts)

    if m.type in VERBES_ACTE:
        texte = f"Selon l'acte du {jour} ({ref}), la propriété de {parcelle}, a été {VERBES_ACTE[m.type]} par <b>{_t(cedants)}</b> au profit de <b>{_t(beneficiaires)}</b>."
        if c["montant"]:
            texte += f" Prix : <b>{_t(_montant(c['montant']))}</b>."
        lignes = [[_p(ROLES_ACTE[x["role"]]), _p(x["nom"], S_CELLULE_GRAS), _p(x["identite"] or "-"), _p(x["part"] or "-"), _p(_m2(x["m2"]))] for x in c["parties"]]
        entetes, largeurs = ["Rôle", "Nom", "Pièce d'identité", "Quote-part", "Superficie"], [2.6, 5.2, 3.6, 2.4, 3.2]
        sigs = ["Le cédant", "Le bénéficiaire", "Le Directeur"]
    elif m.type == C.MUT_HYPOTHEQUE:
        texte = (
            f"Selon l'acte du {jour} ({ref}), une hypothèque de <b>{_t(_montant(c['montant']))}</b> a été inscrite sur {parcelle}, "
            f"au profit de <b>{_t(creancier)}</b>, sur la propriété de <b>{_t(proprietaires)}</b>."
        )
        lignes = [[_p(ROLES_ACTE[x["role"]]), _p(x["nom"], S_CELLULE_GRAS), _p(x["identite"] or "-"), _p(_montant(c["montant"]) or "-")] for x in c["parties"]]
        aire = c["superficie_m2"]
        for pp, f in M.etat(d).parts:
            lignes.append([_p("Propriétaire"), _p(pp.nom_complet, S_CELLULE_GRAS), _p(pp.identite or "-"), _p(M.formater_part(f) + (f" · {_m2(round(float(f) * aire, 2))}" if aire else ""))])
        entetes, largeurs = ["Rôle", "Nom", "Pièce d'identité", "Montant garanti / quote-part"], [2.6, 5.2, 3.6, 5.6]
        sigs = ["Le créancier", "Le propriétaire", "Le Directeur"]
    else:  # mainlevée
        origine = f"du {_date_fr(c['hypotheque_du'])}" if c.get("hypotheque_du") else ""
        creanciers = ", ".join(x.proprietaire.nom_complet for x in m.lie_a.parties if x.role == C.ROLE_CREANCIER) if m.lie_a else "-"
        montant_hyp = _montant(c.get("hypotheque_montant")) or "-"
        texte = (
            f"Selon l'acte du {jour} ({ref}), mainlevée a été donnée de l'hypothèque {origine} d'un montant de <b>{_t(montant_hyp)}</b> "
            f"inscrite sur {parcelle}, au profit de <b>{_t(creanciers)}</b>."
        )
        lignes = [[_p("Créancier"), _p(creanciers, S_CELLULE_GRAS), _p(montant_hyp)]]
        entetes, largeurs = ["Rôle", "Nom", "Montant de l'hypothèque levée"], [3.0, 8.0, 6.0]
        sigs = ["Le créancier", "Le Directeur"]

    fiche = [
        ("Mutation", f"{C.MUTATIONS[m.type]} - n° {m.seq} de l'historique"),
        ("Date et référence de l'acte", f"{jour} - {c['reference'] or 'sans référence'}"),
        ("Parcelle", f"{c['commune']}{(' - ' + c['village']) if c['village'] else ''}, {c['ile']}"),
        ("Superficie", _m2(c["superficie_m2"]) if c["superficie_m2"] else "non renseignée"),
    ]
    if c.get("nouveau_dossier"):
        fiche.append(("Nouveau dossier", c["nouveau_dossier"]))
    validation = f"Validé par {_t(m.decision_par.nom)} le {m.decision_at:%d/%m/%Y}" if m.decision_par and m.decision_at else ""
    return [
        Paragraph(texte, S_TEXTE),
        Spacer(1, 14),
        _tableau(entetes, lignes, largeurs),
        Spacer(1, 14),
        _fiche(fiche, marge=3),
        Spacer(1, 10),
        Paragraph(_t(f"{validation} - empreinte {c['empreinte']}" if c["empreinte"] else validation), S_PETIT),
        Spacer(1, 18),
        _signatures(sigs, qr, signature=_signature_du_acte(m, d, user), index=len(sigs) - 1),
    ]


def _signature_du_acte(m, d, user):
    from . import signature

    return signature.pour_impression(d, user, candidats=(m.decision_par, user))


CONSTRUCTEURS = {
    "plan_croquis": _plan_croquis,
    "certificat": _certificat,
    "extrait": _extrait,
    "attestation": _attestation,
}


def generer(type_doc, d, user, qr=None, mutation=None):
    """Retourne le PDF (bytes) du document demandé.

    `qr` = {"url", "code", "copie"} pour imprimer le QR de sécurité ; sans lui, un cadre vide est réservé.
    """
    titre = TITRES[type_doc]
    tampon = BytesIO()
    if type_doc == "plan_croquis":                    # une seule feuille A3 paysage, tout est placé dessus
        doc = SimpleDocTemplate(
            tampon, pagesize=PAGE_PLAN, leftMargin=MARGES_PLAN["gauche"], rightMargin=MARGES_PLAN["droite"],
            topMargin=MARGES_PLAN["haut"], bottomMargin=MARGES_PLAN["bas"], title=f"{titre} - {d.numero}", author="SIGF",
        )
        pied = lambda c, dc: _pied(c, dc, d, user, qr)  # noqa: E731
        doc.build(_plan_croquis(d, qr, user), onFirstPage=pied, onLaterPages=pied)
        return tampon.getvalue()
    doc = SimpleDocTemplate(
        tampon,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.8 * cm,
        bottomMargin=2 * cm,
        title=f"{titre} - {d.numero}",
        author="SIGF",
    )
    if type_doc in C.TYPES_ACTES:
        histoire = _entete(titre, d) + _acte(mutation, d, qr, user)
    elif type_doc == "extrait":                       # le QR code de l'extrait est en haut de la page, à droite
        histoire = _entete_qr_en_haut(titre, d, qr) + CONSTRUCTEURS[type_doc](d, qr, user)
    else:
        histoire = _entete(titre, d) + CONSTRUCTEURS[type_doc](d, qr, user)
    pied = lambda c, dc: _pied(c, dc, d, user, qr)  # noqa: E731
    doc.build(histoire, onFirstPage=pied, onLaterPages=pied)
    return tampon.getvalue()
