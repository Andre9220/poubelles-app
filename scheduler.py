"""Roulement des poubelles : qui, et pour quand.

Règle : rotation stricte dans l'ordre défini, en repartant de celui qui a validé
en dernier. Cela garantit mécaniquement les deux contraintes demandées — jamais
deux tours d'affilée, et une répartition parfaitement équitable.
"""

from datetime import timedelta

import database as bd

JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def jours_collecte():
    """Jours de la semaine où les poubelles sortent (0 = lundi)."""
    brut = bd.get_reglage("jours_collecte", "0,2,4,6")
    jours = sorted({int(j) for j in brut.split(",") if j.strip().isdigit() and 0 <= int(j) <= 6})
    return jours or [0, 2, 4, 6]


def prochaine_date(depuis=None):
    """Prochain jour de collecte, aujourd'hui inclus."""
    depuis = depuis or bd.aujourdhui()
    jours = jours_collecte()
    for delta in range(8):
        j = depuis + timedelta(days=delta)
        if j.weekday() in jours:
            return j
    return depuis


def date_lisible(j):
    return f"{JOURS_FR[j.weekday()]} {j.day} {MOIS_FR[j.month - 1]}"


def colocs_disponibles():
    """Ceux qui peuvent être désignés : actifs et pas en vacances."""
    return bd.get_colocs(inclure_absents=False)


def prochain_coloc():
    """Le coloc dont c'est le tour, ou None s'il n'y a personne de disponible."""
    # Un tour imposé par l'admin (absence, empêchement) prime sur le roulement.
    force = bd.get_tour_force()
    if force:
        return force

    colocs = colocs_disponibles()
    if not colocs:
        return None
    if len(colocs) == 1:
        return colocs[0]

    derniere = bd.derniere_sortie()
    if not derniere:
        return colocs[0]

    # On cherche le dernier valideur par id : contrairement à la v1 qui
    # cherchait par nom, un renommage ou une suppression ne casse plus rien.
    positions = {c["id"]: i for i, c in enumerate(colocs)}
    index = positions.get(derniere["coloc_id"])
    if index is None:
        # Dernier valideur parti ou absent : on cherche le premier qui le suit
        # dans l'ordre global, pour ne pas toujours retomber sur le premier.
        tous = bd.get_colocs()
        pos_globales = {c["id"]: i for i, c in enumerate(tous)}
        depart = pos_globales.get(derniere["coloc_id"], -1)
        for decalage in range(1, len(tous) + 1):
            candidat = tous[(depart + decalage) % len(tous)]
            if any(c["id"] == candidat["id"] for c in colocs):
                return candidat
        return colocs[0]
    return colocs[(index + 1) % len(colocs)]


def passer_tour():
    """Fait sauter le tour de la personne désignée, sans enregistrer de sortie."""
    colocs = colocs_disponibles()
    if len(colocs) < 2:
        raise bd.ErreurBase("Il faut au moins deux personnes disponibles.")
    actuel = prochain_coloc()
    index = {c["id"]: i for i, c in enumerate(colocs)}.get(actuel["id"], -1)
    suivant = colocs[(index + 1) % len(colocs)]
    bd.definir_tour(suivant["id"])
    return suivant


def serie_actuelle():
    """Sorties consécutives faites dans le délai, en repartant de la plus récente."""
    limite = bd.delai_max_heures()
    serie = 0
    for entree in bd.get_historique(limite=100):
        delai = entree["delai_heures"]
        if delai is None:
            break  # sortie sans signalement rattaché : non mesurable
        if delai <= limite:
            serie += 1
        else:
            break
    return serie


def delai_lisible(heures):
    if heures is None:
        return "?"
    minutes = int(round(heures * 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 60 * 24:
        return f"{minutes // 60} h {minutes % 60:02d}"
    jours, reste = divmod(minutes, 60 * 24)
    return f"{jours} j {reste // 60} h"


def peut_valider(coloc):
    """Anti-triche : seul le coloc désigné peut valider son tour."""
    designe = prochain_coloc()
    if not designe or not coloc:
        return False
    return designe["id"] == coloc["id"]
