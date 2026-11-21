# SIGF — Système Intégré de Gestion Foncière

Application web de gestion foncière pour l'Union des Comores (trois îles : Ngazidja, Ndzuwani, Mwali).
Elle numérise les dossiers papier existants, conduit les nouveaux dossiers de bout en bout (plan croquis → certificat),
enregistre toutes les mutations (ventes, héritages, hypothèques…) avec un historique infalsifiable, et imprime des
documents officiels portant un **QR code vérifiable publiquement**.

Interface entièrement en français. Python / Flask, hébergement prévu sur Render.

---

## Sommaire

1. [Fonctionnalités](#fonctionnalités)
2. [Comptes et droits](#comptes-et-droits)
3. [Installation en local](#installation-en-local)
4. [Configuration](#configuration)
5. [Commandes utiles](#commandes-utiles)
6. [Tests](#tests)
7. [Déploiement sur Render](#déploiement-sur-render)
8. [Architecture](#architecture)
9. [Principes à connaître](#principes-à-connaître)
10. [Reste à faire](#reste-à-faire)

---

## Fonctionnalités

**Circuit d'un nouveau dossier**
plan croquis (topographe) → avis du topographe privé (filière privée) → numéro RTC (topographe public) → numéro RTS (Domaine) →
validation du Directeur → **certificat**. Chaque étape est tracée (qui, quand, motif en cas de rejet).

**Anciens dossiers**
Reprise des dossiers papier par numérisation : scans classés par type de document (titre foncier, acte, pièce d'identité…),
situation actuelle uniquement. Ils vivent dans leur propre page, séparée des nouveaux dossiers.

**Mutations et traçabilité**
Vente, donation, héritage, expropriation, hypothèque, mainlevée, rectification, annulation, **division** et **fusion** de parcelles.
Le Domaine saisit, le Directeur valide. Chaque transfert de propriété **crée un nouveau dossier** et clôture l'ancien ;
la fiche montre la **lignée** sous forme d'arbre. L'historique est une chaîne signée (HMAC) : toute altération est détectée.

**Corrections**
Directes pendant le circuit ; sous forme de demande validée par le Directeur une fois le dossier certifié.

**Documents officiels (PDF)**
Plan croquis (une seule feuille A3 paysage : croquis à gauche, informations et signatures à droite ; dessiné à l'échelle depuis le tracé ; les lignes (routes, canaux), points (tourelle, fontaine) et polygones dont le nom commence par « Annexe » (bâtiment, plantation…) présents dans le KMZ y sont dessinés avec leur nom, avec bornes en UTM 38 Sud, longueurs et azimuts des côtés, parcelles voisines détectées, limitrophes, nature et usage du terrain, levé et observations saisis par le topographe dans « Description du terrain »), certificat de propriété, extrait (avec mutations, charges et lignée ; QR code en haut à droite), attestation.
Dès qu'un nouveau dossier est certifié (validation du Directeur, ou dossier issu d'un transfert de propriété), le système **génère
automatiquement tous ses documents** ; ils apparaissent dans la section *Documents* de la fiche, avec la possibilité d'ajouter
d'autres documents à la main. De même, chaque mutation validée qui cède des parts ou grève la parcelle (vente, donation, héritage,
expropriation, hypothèque, mainlevée) produit son **acte**, prêt à imprimer. Chaque exemplaire est numéroté avec son **QR code** et archivé
(il se rouvre à l'identique) ; la page publique `/verifier` indique si le document est
authentique, non à jour, suspendu, révoqué ou clôturé. Le Directeur peut y faire figurer sa **signature numérique**.

**Carte**
Import de tracés KML / KMZ, calcul de superficie, détection de chevauchements, export vers Google Earth,
carte générale (recherche, filtres, légende par statut, mesure de distance et de surface), carte des biens d'un propriétaire.

**Propriétaires**
Les personnes sont reconnues par leur n° de pièce d'identité (obligatoire à la création d'un dossier). Profil : parcelles,
anciens biens, hypothèques, parcours, carte. Modification des coordonnées par demande du Domaine, validée par le Directeur.

**Blocages**
Tout agent peut signaler un problème sur un dossier (24 types : litige, double vente, soupçon de faux, chevauchement…).
Le dossier est alors figé jusqu'à ce que le Directeur lève le blocage, preuve à l'appui.

**Pilotage**
Page de bienvenue, tableau de bord (statistiques, délais, dossiers en retard), journal des mouvements de chaque compte,
profil utilisateur, gestion des comptes.

---

## Comptes et droits

| Rôle | Ce qu'il fait |
|---|---|
| **Administrateur** | Gère tous les comptes, voit toutes les îles, consulte les signatures des Directeurs. Jamais limité à une île. |
| **Directeur** | Valide les dossiers, mutations et corrections ; lève les blocages ; dépose sa signature (page *Paramètres*) ; crée les topographes privés. |
| **Domaine** | Saisit le numéro RTS et les mutations, importe les anciens dossiers, propose corrections et modifications de propriétaires. |
| **Topographe public** | Ouvre les dossiers publics, termine le plan croquis, saisit le RTC, dépose les tracés. |
| **Topographe privé** | Ne voit que **ses** dossiers et leurs propriétaires ; donne son avis (il peut y joindre jusqu'à 5 documents) puis le dossier passe en lecture seule pour lui. |
| **Fonction publique** | Ne voit que les dossiers entrés en **titrage** (de son île) : enregistre le Journal officiel et le Journal de clôture. |

Après la certification, le dossier entre en **titrage** : Journal officiel (Fonction publique) → Plan de bornage (Domaine) →
Journal de clôture (Fonction publique) → Titré (Domaine). Chaque étape enregistre sa preuve (n° de parution / de procès-verbal / de
titre, date, scans facultatifs) ; l'acteur d'une étape peut renvoyer le dossier à l'étape précédente avec un motif. Le statut reste
« Certificat émis » (les mutations restent possibles) ; seuls les dossiers certifiés après la mise en place du titrage y entrent.
Depuis les étapes du titrage, on peut **bloquer le dossier** (signaler un problème) tant qu'il n'est pas titré ; un dossier titré ne peut plus être bloqué.
La page **Titrage** (menu) suit les dossiers par étape (Journal officiel, Plan de bornage, Journal de clôture, À titrer, Titrés), met en avant ceux qui
revient au compte connecté et affiche le délai passé dans l'étape. Un clic sur un dossier ouvre son **suivi de titrage** (étapes, saisie, historique, scans), page indépendante de la fiche complète ; la Fonction publique n'a accès qu'à cette page. `TITRAGE_DELAI_BORNAGE_JOURS` impose, si besoin, un délai minimal entre le Journal officiel et le bornage.

Règles transversales :

- **Une île par compte** : sauf l'administrateur, chaque compte ne voit que les informations de son île (dossiers, propriétaires,
  mutations, carte, statistiques). Les comptes créés avant cette règle n'ont pas d'île et ne sont pas limités : l'administrateur doit
  leur en attribuer une.
- Toutes les permissions sont appliquées **côté serveur** ; un dossier hors périmètre répond « introuvable » (404).
- Une personne ne valide jamais sa propre saisie.

---

## Installation en local

Prérequis : Python 3.13.

```bash
cd SIGF
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt        # requirements.txt + outils de test

cp .env.example .env                       # puis adapter si besoin (voir « Configuration »)

flask --app run db upgrade                 # crée / met à jour la base (SQLite par défaut : instance/sigf.db)
flask --app run init-data                  # crée les trois îles
flask --app run create-admin               # crée le premier compte Administrateur

python run.py                              # http://127.0.0.1:5003  (--port pour changer)
```

Ensuite, connectez-vous en administrateur et créez les autres comptes depuis **Utilisateurs → Nouveau compte**
(identifiant + mot de passe provisoire ; chaque compte non administrateur reçoit une île).
Chaque nouvel utilisateur doit changer son mot de passe à sa première connexion.

**Données de démonstration** (dossiers fictifs, nouveaux et anciens, avec mutations, divisions, fusions et documents) :

```bash
flask --app run seed-demo
```

Il faut d'abord avoir au moins : 1 Directeur, 1 Domaine, 1 topographe public et 2 topographes privés.
Cette commande est refusée en production.

---

## Configuration

Les réglages se font par variables d'environnement, ou dans un fichier `.env` en local (voir `.env.example`).
Les vraies variables d'environnement l'emportent sur le `.env`.

| Variable | Rôle | Défaut / remarque |
|---|---|---|
| `SECRET_KEY` | Signe les sessions | **Obligatoire en production** |
| `QR_SECRET` | Signe les QR codes et la chaîne des mutations | **Obligatoire en production. Ne jamais le changer** une fois des documents imprimés, ne jamais le publier |
| `PUBLIC_BASE_URL` | Adresse publique du site, encodée dans les QR | ex. `https://sigf.onrender.com` |
| `DATABASE_URL` | Base de données | SQLite locale ; PostgreSQL en production |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | Stockage des scans et des signatures (S3, Cloudflare R2, Backblaze B2…) | **Obligatoire sur Render** (disque éphémère). Vide en local : fichiers dans `instance/uploads` |
| `UPLOAD_DIR` | Dossier des fichiers en local | `instance/uploads` |
| `MAX_FICHIER_MO` | Taille maximale d'un scan | 25 |
| `DOCUMENTS_AUTOMATIQUES` | Génération automatique des documents à la certification d'un dossier | `1` (actif) ; `0` pour la désactiver |
| `GOOGLE_MAPS_API_KEY` | Satellite Google (facultatif) | Sans clé : imagerie Esri. La clé s'affiche dans la page : la **restreindre** aux adresses du site dans Google Cloud |
| `TUILES_SATELLITE_URL`, `TUILES_PLAN_URL`, `TUILES_SATELLITE_ZOOM_NATIF`… | Fonds de carte | Esri / OpenStreetMap ; zoom natif 17 |
| `ENTETE_PAYS`, `ENTETE_DEVISE`, `ENTETE_SERVICE` | En-tête des documents imprimés | « UNION DES COMORES »… |

Autres valeurs fixes : session de 8 h, mot de passe de 10 caractères minimum, cookies `Secure` sur Render, jetons CSRF sur tous les formulaires.

---

## Commandes utiles

```bash
flask --app run db upgrade                 # appliquer les migrations
flask --app run db migrate -m "message"    # générer une migration après un changement de modèle
flask --app run init-data                  # données de référence (idempotent)
flask --app run create-admin               # nouveau compte Administrateur
flask --app run seed-demo [--ajouter]      # données fictives (jamais en production)
flask --app run generer-documents --utilisateur <directeur>   # génère les documents des dossiers déjà certifiés (rattrapage, idempotent)
```

Les migrations sont dans `migrations/versions/`. **Toujours sauvegarder la base avant `db upgrade`** sur des données réelles.

---

## Tests

```bash
pytest -q
```

Environ 500 tests (SQLite en mémoire, stockage S3 simulé avec `moto`). Comptez 6 à 8 minutes pour la suite complète ;
chaque fichier se lance séparément (`pytest tests/test_mutations.py -q`). Ne modifiez pas le code pendant une exécution complète :
cela peut produire des échecs passagers.

Ils couvrent le circuit, les droits par rôle et par île, les mutations et la chaîne signée, les PDF et QR, la vérification publique,
la carte, les propriétaires, les blocages, les comptes et la signature.

---

## Déploiement sur Render

Le fichier `render.yaml` décrit le service web (gunicorn) et la base PostgreSQL. Au déploiement, la commande de build applique
les migrations et crée les données de référence :

```
pip install -r requirements.txt && flask --app wsgi db upgrade && flask --app wsgi init-data
```

Étapes :

1. Créer un bucket S3 compatible (Cloudflare R2 ou Backblaze B2) et renseigner les variables `S3_*` dans le tableau de bord Render.
2. Renseigner `PUBLIC_BASE_URL` (adresse publique du site). `SECRET_KEY` et `QR_SECRET` sont générés par Render :
   **notez `QR_SECRET` et conservez-le** — le perdre ou le changer rend invérifiables tous les documents déjà imprimés.
3. Premier déploiement, puis création du premier administrateur via le *Shell* Render :
   `flask --app wsgi create-admin`
4. Mettre en place les **sauvegardes** de la base PostgreSQL et du bucket.
5. Restreindre la clé Google Maps (si utilisée) aux adresses du site.

À chaque mise à jour contenant une migration, elle est appliquée automatiquement par la commande de build.

---

## Architecture

```
SIGF/
├── app/
│   ├── __init__.py        fabrique de l'application, commandes CLI, contexte des gabarits
│   ├── models.py          modèles SQLAlchemy
│   ├── constants.py       rôles, statuts, types de mutation, types de problème…
│   ├── workflow.py        circuit des dossiers + règles de visibilité (rôle, île)
│   ├── mutations.py       mutations, état du terrain, chaîne signée (HMAC)
│   ├── transmission.py    création du dossier successeur lors d'un transfert
│   ├── partages.py        division et fusion de parcelles
│   ├── corrections.py     corrections directes ou soumises à validation
│   ├── historique.py      charges en vigueur, lignée, mutations d'un dossier
│   ├── arbre.py           arbre de lignée des dossiers
│   ├── personnes.py       regroupement des fiches propriétaires, patrimoine, carte des biens
│   ├── proprietaires.py   pages Propriétaires
│   ├── blocages.py        blocage d'un dossier jusqu'à preuve du contraire
│   ├── signature.py       signature numérique du Directeur (page Paramètres)
│   ├── documents.py       scans : dépôt, validation, consultation
│   ├── pdf.py             documents officiels (reportlab)
│   ├── verification.py    QR codes, vérification publique, révocation
│   ├── carte.py, geo.py   tracés KML/KMZ, superficies, chevauchements, cartes
│   ├── stats.py           statistiques du tableau de bord
│   ├── activite.py        journal et chiffres d'activité des comptes
│   ├── storage.py         stockage local ou S3
│   ├── auth.py, users.py  connexion, profil, gestion des comptes
│   ├── views.py           accueil, tableau de bord, listes et fiche d'un dossier
│   ├── demo.py            données de démonstration
│   ├── templates/         gabarits Jinja (Bootstrap 5)
│   └── static/            CSS, JavaScript (cartes, graphiques, cadre de signature), icônes
├── migrations/            migrations Alembic
├── tests/                 tests pytest
├── config.py              configuration par variables d'environnement
├── run.py                 serveur de développement
├── wsgi.py                point d'entrée de production (gunicorn)
├── render.yaml            déploiement Render
└── requirements*.txt
```

Technologies : Flask 3, SQLAlchemy 2, Flask-Login, Flask-WTF (CSRF), Flask-Migrate, gunicorn, reportlab (PDF et QR), Pillow,
shapely et pyproj (géométrie), boto3 (S3), Bootstrap 5, Leaflet.

---

## Principes à connaître

- **Historique infalsifiable.** Chaque mutation validée est scellée avec l'empreinte de la précédente (HMAC-SHA256, clé `QR_SECRET`).
  Elle n'est jamais modifiée ni supprimée : une erreur se corrige par une mutation d'annulation. Les identifiants des fiches
  propriétaires entrent dans l'empreinte : on ne fusionne donc jamais deux fiches en base, on les **regroupe** par numéro de pièce d'identité.
- **Un transfert = un nouveau dossier.** La vente, la donation, l'héritage ou l'expropriation clôturent l'ancien dossier et en créent un
  nouveau (tracé et charges reportés). Seul le dossier « à jour » est modifiable.
- **Le QR code prouve l'authenticité, pas la signature.** La signature numérique du Directeur est un élément visuel : la changer
  ne rend pas « non à jour » les documents déjà imprimés.
- **Vérification publique.** `/verifier` est accessible sans compte ; elle n'affiche jamais la nature d'un blocage.
- **Heures en UTC.** Les dates et heures affichées sont en UTC (3 heures de moins que l'heure des Comores).
- **Fichiers.** PDF, JPG, PNG ou TIFF, 25 Mo maximum chacun, contenu vérifié. Les signatures sont ré-encodées en PNG.
- **Journal d'audit.** Connexions, créations, validations, impressions, consultations de profils, modifications de comptes… sont enregistrées.

---

## Reste à faire

- Limitation des tentatives de connexion et réinitialisation d'un mot de passe oublié.
- Page de consultation du journal d'audit pour l'administrateur (aujourd'hui : mouvements par compte).
- Carte générale : chargement par zone (limitée à 3 000 parcelles affichées).
- Documents : police arabe, modèles officiels définitifs, lignée sur le certificat.
- Déploiement en production : stockage S3, sauvegardes, nom de domaine.
- Passage à l'heure locale des Comores pour l'affichage.
