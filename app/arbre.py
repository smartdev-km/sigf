"""Arbre de traçabilité d'une parcelle.

Relie les dossiers par leurs liens de filiation : transfert de propriété (vente, donation, héritage,
expropriation), division et fusion. On remonte jusqu'aux dossiers d'origine et on descend jusqu'aux
dossiers actuels. Une fusion réunit plusieurs branches : le dossier fusionné apparaît sous son premier
parent et est simplement rappelé (« déjà affiché ») sous les autres, ce qui garde un arbre lisible.
"""

from collections import defaultdict

from . import constants as C
from .extensions import db
from .models import Mutation

MAX_DOSSIERS = 150


def construire(d):
    """Retourne {"racines": [...], "nb": n, "tronque": bool} ; chaque nœud est un dict."""
    visites, liens, a_voir = {}, {}, [d]
    while a_voir and len(visites) < MAX_DOSSIERS:
        x = a_voir.pop()
        if x.id in visites:
            continue
        visites[x.id] = x
        for f in list(x.descendances) + list(x.origines):
            liens[f.id] = f
            for autre in (f.source, f.resultat):
                if autre.id not in visites:
                    a_voir.append(autre)
    tronque = bool(a_voir)

    enfants = defaultdict(list)
    for f in sorted(liens.values(), key=lambda f: f.id):
        if f.source_id in visites and f.resultat_id in visites:
            enfants[f.source_id].append(f)
    ont_un_parent = {f.resultat_id for f in liens.values() if f.source_id in visites}
    racines = sorted((i for i in visites if i not in ont_un_parent), key=lambda i: visites[i].id)

    deja = set()

    def noeud(i, lien):
        x = visites[i]
        mutation = db.session.get(Mutation, lien.mutation_id) if lien is not None and lien.mutation_id else None
        n = {
            "d": x,
            "lien": lien,
            "mutation": mutation,
            "courant": x.id == d.id,
            "proprietaire": x.proprietaire_label,
            "enfants": [],
            "rappel": False,
        }
        if i in deja:
            n["rappel"] = True  # déjà affiché sous un autre parent (fusion)
            return n
        deja.add(i)
        for f in enfants.get(i, []):
            n["enfants"].append(noeud(f.resultat_id, f))
        return n

    return {"racines": [noeud(r, None) for r in racines], "nb": len(visites), "tronque": tronque}


def a_une_histoire(arbre):
    """Faux si le dossier est seul : aucune transmission, division ni fusion à représenter."""
    return arbre["nb"] > 1


# Vocabulaire commun aux écrans
LIBELLES = {
    C.MUT_VENTE: "Vente", C.MUT_DONATION: "Donation", C.MUT_HERITAGE: "Héritage", C.MUT_EXPROPRIATION: "Expropriation",
    C.MUT_DIVISION: "Division", C.MUT_FUSION: "Fusion",
}
PASSE = {
    C.MUT_VENTE: "Vendue", C.MUT_DONATION: "Donnée", C.MUT_HERITAGE: "Transmise par héritage", C.MUT_EXPROPRIATION: "Expropriée",
    C.MUT_DIVISION: "Divisée", C.MUT_FUSION: "Fusionnée",
}
ORIGINE = {
    C.MUT_VENTE: "d'une vente", C.MUT_DONATION: "d'une donation", C.MUT_HERITAGE: "d'un héritage", C.MUT_EXPROPRIATION: "d'une expropriation",
    C.MUT_DIVISION: "d'une division", C.MUT_FUSION: "d'une fusion",
}
ICONES = {
    C.MUT_VENTE: "cash-coin", C.MUT_DONATION: "gift", C.MUT_HERITAGE: "people", C.MUT_EXPROPRIATION: "bank",
    C.MUT_DIVISION: "diagram-2", C.MUT_FUSION: "intersect",
}
