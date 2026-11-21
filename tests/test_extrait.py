import re
from io import BytesIO

from pypdf import PdfReader

from app import constants as C
from app import historique as H
from app import mutations as M
from app.extensions import db
from app.models import DocumentEmis, Dossier, Filiation

from test_carte import LAT, LON, kml, points, televerser
from test_corrections import corriger
from test_mutations import benef, derniere_mutation_id, dossier_certifie, proprio_id, saisir, vendre, vivant
from test_partages import cree, deux_voisines, derniere_id, diviser, enfants, fusionner, numero, valider
from test_verification import dernier_code, imprimer
from conftest import agir


def pages(reponse):
    assert reponse.status_code == 200 and reponse.mimetype == "application/pdf"
    return [re.sub(r"\s+", " ", p.extract_text()) for p in PdfReader(BytesIO(reponse.data)).pages]


def extrait(client, did):
    return pages(client.get(f"/dossiers/{did}/imprimer/extrait"))


def tout(client, did):
    return " ".join(extrait(client, did))


def hypotheque(app, login, did, creancier="Banque", montant="12500000", ref="Acte hyp. n°1"):
    saisir(login("dom"), did, "hypotheque", creancier_nom=creancier, montant=montant, reference_acte=ref)
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    return derniere_mutation_id(app)


def certifier(login, did):
    agir(login("pub"), did, "terminer_plan")
    agir(login("pub"), did, "saisir_rsp", valeur="RTC-9")
    agir(login("dom"), did, "saisir_rcp", valeur="RTS-9")
    agir(login("dir"), did, "valider")


# ================================================================= sections de base
def test_extrait_d_une_parcelle_sans_histoire(app, login):
    did = dossier_certifie(login)
    t = tout(login("dir"), did)
    for attendu in ("EXTRAIT DU DOSSIER FONCIER", "Charges en vigueur", "Aucune hypothèque ni charge inscrite", "Origine de propriété",
                    "ne descend d'aucun autre dossier", "Mutations enregistrées sur ce dossier", "Aucune mutation enregistrée",
                    "Historique du traitement", "Dossier ouvert", "Certificat validé par le Directeur", "Le Conservateur", "Scannez pour vérifier"):
        assert attendu in t, attendu
    assert t.index("Charges en vigueur") < t.index("Origine de propriété") < t.index("Mutations enregistrées") < t.index("Historique du traitement")


def test_charges_en_vigueur_et_mainlevee(app, login):
    did = dossier_certifie(login)
    dire = login("dir")
    hyp_id = hypotheque(app, login, did)
    t = tout(dire, did)
    assert "Banque" in t and "12 500 000 KMF" in t and "Acte hyp. n°1" in t and "Aucune hypothèque" not in t
    assert "Hypothèque inscrite" in t and "au profit de Banque" in t
    saisir(login("dom"), did, "mainlevee", lie_a_id=str(hyp_id), reference_acte="Mainlevée n°7")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    t = tout(dire, did)
    assert "Aucune hypothèque ni charge" in t and "Mainlevée d'hypothèque" in t and "levée de l'hypothèque du" in t


def test_corrections_lisibles_et_aucun_point_d_interrogation_parasite(app, login):
    did = dossier_certifie(login)
    hypotheque(app, login, did)
    corriger(login("dom"), did, p_commune="Itsandra", p_village="Bambao")
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    t = tout(login("dir"), did)
    assert "Correction des données" in t and "Commune : Moroni devient Itsandra" in t and "Village / quartier : — devient Bambao" in t
    assert "?" not in t                                                         # la police du PDF ne sait pas écrire « → » : pas de caractère de remplacement


def test_mutations_annulees_rejetees_et_en_attente(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    hypotheque(app, login, did)
    saisir(dom, did, "annulation", notes="Inscrite par erreur")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    saisir(dom, did, "rectification", notes="Rejetée un jour")
    dire.post(f"/mutations/{derniere_mutation_id(app)}/rejeter", data={"motif": "Non"})
    saisir(dom, did, "rectification", notes="En attente de validation")           # ni rejetée ni validée
    t = tout(dire, did)
    assert "annulée par n° 2" in t and "Annulation" in t and "de la mutation n° 1" in t
    assert "Aucune hypothèque ni charge" in t                                       # l'hypothèque annulée n'est plus une charge
    assert "Rejetée un jour" not in t and "En attente de validation" not in t       # seules les mutations VALIDÉES figurent


def test_transfert_encore_dans_le_dossier_donnees_anciennes(app, login):
    """Un transfert validé AVANT le modèle « nouveau dossier » (données existantes) reste lisible dans l'extrait."""
    did = dossier_certifie(login)
    said = proprio_id(app, did)
    from werkzeug.datastructures import MultiDict
    from app.models import User

    with app.app_context():
        dom, dire = User.query.filter_by(username="dom").one(), User.query.filter_by(username="dir").one()
        d = db.session.get(Dossier, did)
        champs = {"type": "vente", "date_acte": "2019-03-12", "reference_acte": "Acte ancien n°5", "montant": "3000000", f"cede_{said}": "1/2",
                  "b_existant": [""], "b_nom": ["Acheteur"], "b_prenom": [""], "b_identite": [""], "b_telephone": [""], "b_part": ["1/2"]}
        form = MultiDict([(k, x) for k, v in champs.items() for x in (v if isinstance(v, list) else [v])])
        m = M.construire(d, dom, form)
        db.session.add(m)
        db.session.flush()
        M.sceller(d, m, dire)                                    # scellée dans le dossier, sans créer de successeur (comme avant)
        db.session.commit()
    t = tout(login("dir"), did)
    assert "Vente" in t and "Said Ali (1/2) cède à Acheteur (1/2)" in t and "Acte ancien n°5" in t and "3 000 000 KMF" in t


# ================================================================= lignée
def test_lignee_apres_une_vente(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    nouveau = vivant(app, did)
    t = tout(login("dir"), nouveau)
    assert numero(app, did) in t and "Said Ali" in t and "Vente du 01/05/2020" in t and "Acte notarié n°12" in t and "5 000 000 KMF" in t
    assert f"(dossier {numero(app, nouveau)})" in t and "Ce dossier" in t and "Clôturé" in t
    assert "ne descend d'aucun autre dossier" not in t
    assert t.index(numero(app, did)) < t.index(f"{numero(app, nouveau)} Ce dossier")           # l'ancêtre avant le dossier courant


def test_lignee_de_trois_generations_dans_l_ordre(app, login):
    did = dossier_certifie(login)
    vendre(app, login, did, proprio_id(app, did), "1/2", ("Moussa", "1/2"))
    d1 = vivant(app, did)
    vendre(app, login, did, proprio_id(app, did, "Moussa"), "1/4", ("Nadia", "1/4"))
    d2 = vivant(app, did)
    t = tout(login("dir"), d2)
    debut = t.index("Origine de propriété")                                      # (le numéro du dossier courant figure aussi dans le titre)
    positions = [t.index(numero(app, x), debut) for x in (did, d1, d2)]
    assert positions == sorted(positions)
    assert t.count("Vente du 01/05/2020") == 2 and "Moussa (1/2) ; Said Ali (1/2)" in t.replace("  ", " ")
    lignee = None
    with app.app_context():
        lignee = H.lignee(db.session.get(Dossier, d2))
        assert [l["numero"] for l in lignee] == [numero(app, did), numero(app, d1), numero(app, d2)]
        assert [l["courant"] for l in lignee] == [False, False, True]
        assert lignee[-1]["evenements"] == [] and len(lignee[0]["evenements"]) == 1


def test_lignee_d_une_parcelle_fille(app, login):
    did = dossier_certifie(login)
    diviser(login("dom"), did, [{"nom": "Lot A"}, {"nom": "Lot B"}])
    valider(login, derniere_id(app, C.MUT_DIVISION))
    (_, lot_a), _ = enfants(app, did)
    certifier(login, lot_a)
    t = tout(login("dir"), lot_a)
    assert numero(app, did) in t and "Division (Lot A) du 01/05/2020" in t and "Plan de division 2020" in t and f"(dossier {numero(app, lot_a)})" in t


def test_lignee_d_une_fusion_reunit_les_deux_branches(app, login):
    a, b = deux_voisines(app, login)
    fusionner(login("dom"), a, [numero(app, b)])
    valider(login, derniere_id(app, C.MUT_FUSION))
    with app.app_context():
        nid = Filiation.query.filter_by(type="fusion").first().resultat_id
    certifier(login, nid)
    t = tout(login("dir"), nid)
    assert numero(app, a) in t and numero(app, b) in t and t.count("Fusion du 01/05/2020") == 2     # un lien depuis chaque parcelle d'origine
    with app.app_context():
        assert [l["numero"] for l in H.lignee(db.session.get(Dossier, nid))] == sorted([numero(app, a), numero(app, b)]) + [numero(app, nid)]


def test_l_hypotheque_reportee_apparait_avec_son_origine(app, login):
    did = dossier_certifie(login)
    hypotheque(app, login, did)
    vendre(app, login, did, proprio_id(app, did), "tout", ("Moussa", "1"))
    nouveau = vivant(app, did)
    t = tout(login("dir"), nouveau)
    assert "reportée d'un dossier antérieur" in t and "Hypothèque reportée" in t and f"reportée depuis le dossier {numero(app, did)}" in t
    assert "Banque" in t and "12 500 000 KMF" in t


# ================================================================= QR et mise à jour
def test_un_extrait_imprime_devient_non_a_jour_apres_une_nouvelle_mutation(app, login):
    did = dossier_certifie(login)
    dire, anonyme = login("dir"), app.test_client()
    imprimer(dire, did, "extrait")
    code = dernier_code(app)
    page = anonyme.get(f"/verifier/{code}").data.decode()
    assert "Document authentique" in page and "Charges inscrites" in page and "Aucune" in page and "Mutations enregistrées" in page

    hypotheque(app, login, did, creancier="Banque Secrète")
    page = anonyme.get(f"/verifier/{code}").data.decode()
    assert "non à jour" in page and "Document authentique" not in page
    assert "Banque Secrète" not in page                                          # le public ne voit jamais le nom du créancier

    imprimer(dire, did, "extrait")                                               # nouvel exemplaire : à jour, et il indique la charge
    neuf = anonyme.get(f"/verifier/{dernier_code(app)}").data.decode()
    assert "Document authentique" in neuf and "1 hypothèque(s) en vigueur" in neuf and "Banque Secrète" not in neuf


def test_une_correction_valide_apres_impression_rend_l_extrait_non_a_jour(app, login):
    did = dossier_certifie(login)
    imprimer(login("dir"), did, "extrait")
    code = dernier_code(app)
    corriger(login("dom"), did, p_commune="Itsandra")
    login("dir").post(f"/mutations/{derniere_mutation_id(app)}/valider")
    assert "non à jour" in app.test_client().get(f"/verifier/{code}").data.decode()


def test_les_autres_documents_ne_dependent_pas_des_charges(app, login):
    """L'attestation, elle, ne porte pas les charges : une hypothèque ne la rend pas « non à jour »."""
    did = dossier_certifie(login)
    imprimer(login("dir"), did, "attestation")
    code = dernier_code(app)
    hypotheque(app, login, did)
    assert "Document authentique" in app.test_client().get(f"/verifier/{code}").data.decode()


def test_tete_de_chaine(app, login):
    did = dossier_certifie(login)
    with app.app_context():
        assert H.tete_de_chaine(db.session.get(Dossier, did)) is None
    hypotheque(app, login, did)
    with app.app_context():
        t1 = H.tete_de_chaine(db.session.get(Dossier, did))
    hypotheque(app, login, did, creancier="Autre")
    with app.app_context():
        t2 = H.tete_de_chaine(db.session.get(Dossier, did))
    assert t1 and t2 and t1 != t2 and len(t1) == 12


# ================================================================= mise en page
def test_un_dossier_tres_charge_passe_sur_plusieurs_pages_sans_titre_orphelin(app, login):
    did = dossier_certifie(login)
    dom, dire = login("dom"), login("dir")
    for i in range(34):
        saisir(dom, did, "rectification", notes=f"Rectification n°{i}", reference_acte=f"Acte de rectification numéro {i}")
        dire.post(f"/mutations/{derniere_mutation_id(app)}/valider")
    p = extrait(dire, did)
    assert len(p) >= 2
    assert all(f"Page {i}" in page for i, page in enumerate(p, 1))                          # pied de page sur chaque page
    assert sum("Acte du" in page and "Nature" in page for page in p) >= 2                   # en-tête du tableau répété sur chaque page
    assert "Le Conservateur" in p[-1] and "Le Conservateur" not in " ".join(p[:-1])
    assert "Scannez pour vérifier" in p[0] and "Scannez pour vérifier" not in " ".join(p[1:])                # le QR code est en haut de la première page
    # « Historique du traitement » n'est jamais seul en bas de page : il est sur la même page que sa première ligne
    page_titre = next(page for page in p if "Historique du traitement" in page)
    assert "Dossier ouvert" in page_titre
    assert "Rectification n°0" in " ".join(p) and "Rectification n°33" in " ".join(p)


def test_textes_hostiles_ou_non_latins_ne_cassent_pas_l_extrait(app, login):
    did = dossier_certifie(login)
    hypotheque(app, login, did, creancier="<b>Banque</b> & Fils «étrange» محمد", ref='<para>"x"</para> ' + "très long " * 15)
    t = tout(login("dir"), did)
    assert "<b>Banque</b> &amp;" not in t and "&lt;" not in t                      # affiché tel quel, jamais interprété ni double-échappé
    assert "<b>Banque</b> & Fils" in t and "<para>" in t
    assert "très long" in t


def test_le_qr_code_de_l_extrait_est_en_haut_a_droite(app, login):
    """Le QR code est dans l'en-tête de la première page, à droite, au-dessus du titre ; plus de QR en bas."""
    from io import BytesIO

    from pypdf import PdfReader

    did = dossier_certifie(login)
    pdf = PdfReader(BytesIO(login("dir").get(f"/dossiers/{did}/imprimer/extrait").data))
    positions = []

    def visiteur(texte, cm, tm, fd, fs):
        if texte.strip() in ("Scannez pour vérifier l'authenticité", "EXTRAIT DU DOSSIER FONCIER", "Le Conservateur"):
            positions.append((texte.strip(), tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5]))

    pdf.pages[0].extract_text(visitor_text=visiteur)
    scan = next(p for p in positions if p[0].startswith("Scannez"))
    titre = next(p for p in positions if p[0].startswith("EXTRAIT"))
    largeur = float(pdf.pages[0].mediabox[2])
    assert scan[1] > largeur * 0.6                                         # dans la moitié droite de la page
    assert scan[2] > titre[2]                                              # au-dessus du titre (haut de page)
    assert sum(1 for page in pdf.pages for p in [page.extract_text()] if "Scannez pour vérifier" in p) == 1
