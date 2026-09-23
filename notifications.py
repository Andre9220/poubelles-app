"""Notifications WhatsApp.

On passe par CallMeBot, qui permet d'envoyer un WhatsApp sans compte Meta
Business ni modèle de message validé. Contrainte de leur API gratuite : elle est
strictement personnelle. Chaque coloc active donc **son propre** numéro et
renseigne **sa propre** clé depuis son profil ; l'app n'envoie jamais à un numéro
que son propriétaire n'a pas lui-même autorisé.

Activation (à faire une fois par coloc, depuis son téléphone) :
  1. enregistrer +34 623 78 95 95 dans ses contacts ;
  2. lui envoyer sur WhatsApp : « I allow callmebot to send me messages » ;
  3. recevoir la clé API en réponse (sous ~2 min) et la coller dans l'app.
"""

import os
import urllib.error
import urllib.parse
import urllib.request

import database as bd

API_URL = "https://api.callmebot.com/whatsapp.php"
NUMERO_ACTIVATION = "+34 623 78 95 95"
PHRASE_ACTIVATION = "I allow callmebot to send me messages"

TIMEOUT = 15
# L'API refuse les envois trop rapprochés : on ne répète pas un même message.
ANTI_REPETITION_MINUTES = 30

# Coupe-circuit global, pratique pour tester sans spammer les colocs.
ENVOI_ACTIF = os.environ.get("POUBELLES_WHATSAPP", "1") != "0"


def configure(coloc):
    """Vrai si ce coloc peut recevoir des WhatsApp."""
    return bool(coloc and coloc.get("telephone") and coloc.get("whatsapp_apikey"))


def _appeler_api(telephone, apikey, message):
    url = API_URL + "?" + urllib.parse.urlencode(
        {"phone": telephone, "text": message, "apikey": apikey}
    )
    requete = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:
        corps = reponse.read(500).decode("utf-8", "replace")
        if reponse.status != 200:
            raise urllib.error.HTTPError(url, reponse.status, corps, None, None)
        return corps


def envoyer_whatsapp(coloc, message, forcer=False):
    """Envoie un WhatsApp à `coloc`. Renvoie (succès, détail).

    Ne lève jamais : une notification qui échoue ne doit pas casser l'app.
    """
    if not configure(coloc):
        bd.journaliser_notification(
            coloc["id"] if coloc else None, "whatsapp", message, "non configuré"
        )
        return False, "WhatsApp non configuré pour ce colocataire."

    if not ENVOI_ACTIF:
        bd.journaliser_notification(coloc["id"], "whatsapp", message, "désactivé")
        return False, "Envoi WhatsApp désactivé (POUBELLES_WHATSAPP=0)."

    if not forcer and bd.notification_recente(
        coloc["id"], message, ANTI_REPETITION_MINUTES
    ):
        bd.journaliser_notification(coloc["id"], "whatsapp", message, "ignoré (doublon)")
        return False, "Message identique déjà envoyé récemment."

    try:
        corps = _appeler_api(coloc["telephone"], coloc["whatsapp_apikey"], message)
        bd.journaliser_notification(coloc["id"], "whatsapp", message, "envoyé")
        return True, corps.strip()[:200] or "Envoyé."
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}"
        if e.code == 403:
            detail += " — clé API invalide ou numéro non activé."
        bd.journaliser_notification(
            coloc["id"], "whatsapp", message, f"échec : {detail}"
        )
        return False, detail
    except (urllib.error.URLError, OSError, ValueError) as e:
        bd.journaliser_notification(coloc["id"], "whatsapp", message, f"échec : {e}")
        return False, str(e)


def diffuser(message, exclure=None):
    """Envoie le même message à tous les colocs ayant configuré WhatsApp.

    WhatsApp ne permet pas d'écrire dans un groupe via API : on simule la
    diffusion en envoyant à chacun individuellement. Renvoie (envoyés, ignorés).
    """
    envoyes, ignores = [], []
    for coloc in bd.get_colocs(inclure_absents=False):
        if exclure and coloc["id"] == exclure:
            continue
        if not configure(coloc):
            ignores.append(coloc["nom"])
            continue
        ok, _ = envoyer_whatsapp(coloc, message)
        (envoyes if ok else ignores).append(coloc["nom"])
    return envoyes, ignores


def message_recap(auteur, suivant):
    base = f"\U0001F5D1 {auteur['nom']} vient de descendre les poubelles !"
    if suivant and suivant["id"] != auteur["id"]:
        return base + f"\nProchain tour : {suivant['nom']}."
    return base


def annoncer_validation(auteur, suivant):
    """Prévient toute la coloc que c'est fait, et qui prendra le tour suivant."""
    return diffuser(message_recap(auteur, suivant), exclure=auteur["id"])


def message_ton_tour(coloc, jour_lisible):
    return (
        f"Salut {coloc['nom']} \U0001F44B\n"
        f"C'est ton tour de descendre les poubelles ({jour_lisible}) \U0001F5D1"
    )


def annoncer_demande(demandeur, designe):
    """Les poubelles viennent d'être signalées pleines : on prévient la coloc."""
    if not designe:
        return [], []
    perso = (
        f"Salut {designe['nom']} \U0001F44B\n"
        f"Les poubelles sont pleines ({demandeur['nom']} vient de le signaler) "
        f"et c'est ton tour de les descendre \U0001F5D1"
    )
    envoyer_whatsapp(bd.get_coloc(designe["id"]), perso)

    info = (
        f"\U0001F5D1 {demandeur['nom']} signale que les poubelles sont pleines.\n"
        f"C'est au tour de {designe['nom']} de les descendre."
    )
    envoyes, ignores = [], []
    for coloc in bd.get_colocs(inclure_absents=False):
        if coloc["id"] in (designe["id"], demandeur["id"]):
            continue
        if not configure(coloc):
            ignores.append(coloc["nom"])
            continue
        ok, _ = envoyer_whatsapp(coloc, info)
        (envoyes if ok else ignores).append(coloc["nom"])
    return envoyes, ignores


def notifier_nouveau_responsable(coloc, jour_lisible):
    """Prévient le coloc qui vient de devenir responsable."""
    if not coloc:
        return False, "Aucun responsable."
    return envoyer_whatsapp(coloc, message_ton_tour(coloc, jour_lisible))


def envoyer_test(coloc):
    return envoyer_whatsapp(
        coloc,
        f"Test Mission Poubelles \U0001F5D1 Salut {coloc['nom']}, "
        f"les notifications fonctionnent !",
        forcer=True,
    )
