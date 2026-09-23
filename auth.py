"""Authentification : bcrypt + session persistante côté Streamlit.

`st.session_state` est vidé à chaque rechargement de page, ce qui déconnecterait
tout le monde au moindre refresh sur téléphone. On stocke donc un token opaque
en base et on le remet dans l'URL (`?t=...`) pour retrouver la session.
"""

import os

import bcrypt
import streamlit as st
import streamlit.components.v1 as components

import database as bd

CLE_NAVIGATEUR = "poubelles_token"
DUREE_COOKIE = 60 * 60 * 24 * bd.DUREE_SESSION_JOURS


def _js(code):
    """Exécute du JS dans le navigateur via une iframe de composant.

    L'iframe est en srcdoc, donc de même origine que la page : un cookie qu'elle
    pose vaut pour toute l'application.
    """
    components.html(f"<script>{code}</script>", height=1, width=1)


def memoriser_navigateur(token):
    """Pose le jeton en cookie.

    La lecture, elle, se fait côté serveur via `st.context.cookies` : le
    navigateur renvoie le cookie à chaque chargement, quelle que soit l'URL
    utilisée (favori, écran d'accueil, adresse tapée à la main).
    """
    if not token:
        return
    _js(
        f"""
        try {{
          var c = {CLE_NAVIGATEUR!r} + '=' + {token!r}
                + '; path=/; max-age={DUREE_COOKIE}; SameSite=Lax';
          document.cookie = c;
          try {{ window.parent.document.cookie = c; }} catch (e) {{}}
        }} catch (e) {{}}
        """
    )


def oublier_navigateur():
    _js(
        f"""
        try {{
          var c = {CLE_NAVIGATEUR!r} + '=; path=/; max-age=0; SameSite=Lax';
          document.cookie = c;
          try {{ window.parent.document.cookie = c; }} catch (e) {{}}
        }} catch (e) {{}}
        """
    )


def _jeton_cookie():
    try:
        return st.context.cookies.get(CLE_NAVIGATEUR)
    except Exception:
        return None

# Code demandé à l'inscription, pour éviter qu'un inconnu se crée un compte.
CODE_INVITATION = os.environ.get("POUBELLES_CODE_INVITATION", "coloc2026")
# Pseudo qui obtient les droits admin à l'inscription.
PSEUDO_ADMIN = os.environ.get("POUBELLES_ADMIN", "andre").lower()

MDP_MIN = 6


def hacher(mot_de_passe):
    return bcrypt.hashpw(mot_de_passe.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verifier(mot_de_passe, hash_stocke):
    if not hash_stocke:
        return False
    try:
        return bcrypt.checkpw(
            mot_de_passe.encode("utf-8"), hash_stocke.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def valider_mot_de_passe(mdp, confirmation):
    if not mdp or len(mdp) < MDP_MIN:
        raise bd.ErreurBase(f"Le mot de passe doit faire au moins {MDP_MIN} caractères.")
    if mdp != confirmation:
        raise bd.ErreurBase("Les deux mots de passe ne correspondent pas.")
    return mdp


def colocs_sans_compte():
    """Colocs créés par l'ancienne version, qui n'ont pas encore d'identifiants."""
    return [c for c in bd.get_colocs() if not c["password_hash"]]


def inscrire(nom, pseudo, mdp, confirmation, code, photo=None, coloc_existant=None):
    if code != CODE_INVITATION:
        raise bd.ErreurBase("Code d'invitation incorrect. Demande-le à ton coloc admin.")
    valider_mot_de_passe(mdp, confirmation)
    hash_mdp = hacher(mdp)

    if coloc_existant:
        bd.rattacher_compte(coloc_existant, pseudo, hash_mdp, photo)
        coloc_id = coloc_existant
    else:
        est_admin = bd.valider_pseudo(pseudo).lower() == PSEUDO_ADMIN
        coloc_id = bd.creer_coloc(nom, pseudo, hash_mdp, photo, is_admin=est_admin)

    # Le tout premier compte devient admin, sinon personne ne pourrait administrer.
    if not any(c["is_admin"] for c in bd.get_colocs(actifs_seulement=False)):
        bd.maj_coloc(coloc_id, is_admin=1)

    return coloc_id


def connecter(pseudo, mdp):
    coloc = bd.get_coloc_par_pseudo((pseudo or "").strip())
    # Message identique dans les deux cas : ne pas révéler quels pseudos existent.
    if not coloc or not verifier(mdp, coloc["password_hash"]):
        raise bd.ErreurBase("Pseudo ou mot de passe incorrect.")
    if not coloc["actif"]:
        raise bd.ErreurBase("Ce compte a été désactivé.")
    token = bd.creer_session(coloc["id"])
    st.session_state["token"] = token
    # Volontairement pas dans l'URL : un lien partagé donnerait accès au compte.
    # La persistance passe par le cookie posé au premier rendu.
    return coloc


def deconnecter():
    token = st.session_state.get("token") or st.query_params.get("t")
    bd.supprimer_session(token)
    st.session_state.pop("token", None)
    st.session_state["oublier"] = True
    if "t" in st.query_params:
        del st.query_params["t"]


def utilisateur_courant():
    """Renvoie le coloc connecté (dict) ou None.

    Trois sources, dans l'ordre : la session Streamlit (navigation courante),
    le cookie (visites suivantes), l'URL (compatibilité avec les liens déjà
    partagés).
    """
    token = (
        st.session_state.get("token")
        or _jeton_cookie()
        or st.query_params.get("t")
    )
    if not token:
        return None
    coloc = bd.coloc_par_session(token)
    if coloc is None:
        # Jeton périmé ou révoqué : on purge aussi la copie du navigateur,
        # sinon chaque ouverture rejouerait la même redirection inutile.
        st.session_state.pop("token", None)
        st.session_state["oublier"] = True
        return None
    # Le token vient peut-être de l'URL : on le réaligne dans les deux sens.
    st.session_state["token"] = token
    return coloc


def changer_mot_de_passe(coloc_id, ancien, nouveau, confirmation):
    coloc = bd.get_coloc(coloc_id)
    if not coloc or not verifier(ancien, coloc["password_hash"]):
        raise bd.ErreurBase("Ancien mot de passe incorrect.")
    valider_mot_de_passe(nouveau, confirmation)
    bd.maj_coloc(coloc_id, password_hash=hacher(nouveau))
