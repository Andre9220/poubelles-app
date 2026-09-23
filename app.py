"""Mission Poubelles — application Streamlit de la coloc."""

import html
import io
from datetime import datetime

import streamlit as st
from PIL import Image, UnidentifiedImageError

import auth
import database as bd
import notifications as notif
import scheduler as sched

st.set_page_config(
    page_title="Mission Poubelles",
    page_icon="🗑️",
    layout="centered",
    initial_sidebar_state="collapsed",
)


@st.cache_resource(show_spinner=False)
def _preparer_base():
    # Une fois par processus, pas à chaque clic : sur Turso, chaque vérification
    # de schéma coûterait un aller-retour réseau.
    bd.init_db()
    return True



# Interface pensée pour le téléphone : gros boutons, cartes lisibles.
st.markdown(
    """
    <style>
      .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 720px;}
      div.stButton > button {border-radius: 12px; font-weight: 600; min-height: 3rem;}
      .carte {
        border: 1px solid rgba(128,128,128,.25); border-radius: 16px;
        padding: 1rem; text-align: center; height: 100%;
      }
      .carte .valeur {font-size: 1.5rem; font-weight: 700; line-height: 1.3;}
      .carte .titre {font-size: .8rem; opacity: .7; text-transform: uppercase;}
    </style>
    """,
    unsafe_allow_html=True,
)

TAILLE_MAX_PHOTO = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

def enregistrer_photo(fichier, dossier=None):
    """Compresse une photo et la stocke en base. Renvoie sa référence ou None.

    `dossier` ne sert plus : conservé pour ne pas toucher aux appels existants.
    """
    if fichier is None:
        return None
    if fichier.size > TAILLE_MAX_PHOTO:
        raise bd.ErreurBase("Photo trop lourde (5 Mo maximum).")
    try:
        image = Image.open(fichier)
        image.thumbnail((1280, 1280))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        tampon = io.BytesIO()
        image.save(tampon, format="JPEG", quality=80)
    except (UnidentifiedImageError, OSError):
        raise bd.ErreurBase("Fichier image illisible.")
    return bd.stocker_photo(tampon.getvalue())


def avatar(coloc, taille=90):
    contenu = bd.lire_photo(coloc.get("photo") if coloc else None)
    if contenu:
        st.image(contenu, width=taille)
    else:
        st.markdown(
            f"<div style='font-size:{taille}px; line-height:1'>👤</div>",
            unsafe_allow_html=True,
        )


def carte(titre, valeur):
    st.markdown(
        f"<div class='carte'><div class='titre'>{titre}</div>"
        f"<div class='valeur'>{valeur}</div></div>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Écran de connexion
# --------------------------------------------------------------------------

def ecran_connexion():
    st.title("🗑️ Mission Poubelles")
    st.caption("La coloc, mais organisée.")

    onglet_connexion, onglet_inscription = st.tabs(["Connexion", "Créer mon compte"])

    with onglet_connexion:
        with st.form("connexion"):
            pseudo = st.text_input("Pseudo")
            mdp = st.text_input("Mot de passe", type="password")
            if st.form_submit_button("Se connecter", use_container_width=True):
                try:
                    auth.connecter(pseudo, mdp)
                    st.rerun()
                except bd.ErreurBase as e:
                    st.error(str(e))

    with onglet_inscription:
        a_reclamer = auth.colocs_sans_compte()
        choix = None
        if a_reclamer:
            st.info("Tu es déjà dans la liste de la coloc ? Récupère ton compte.")
            options = {"➕ Je suis nouveau": None}
            options.update({c["nom"]: c["id"] for c in a_reclamer})
            libelle = st.selectbox("Qui es-tu ?", list(options))
            choix = options[libelle]

        with st.form("inscription"):
            nom = st.text_input("Prénom", disabled=choix is not None)
            pseudo = st.text_input("Pseudo")
            mdp = st.text_input("Mot de passe", type="password")
            confirmation = st.text_input("Confirmer le mot de passe", type="password")
            code = st.text_input("Code d'invitation", type="password")
            photo = st.file_uploader(
                "Photo de profil (optionnel)", type=["png", "jpg", "jpeg", "webp"]
            )
            if st.form_submit_button("Créer mon compte", use_container_width=True):
                try:
                    chemin = enregistrer_photo(photo, "profils")
                    coloc_id = auth.inscrire(
                        nom, pseudo, mdp, confirmation, code, chemin, choix
                    )
                    auth.connecter(pseudo, mdp)
                    st.success(f"Bienvenue ! Compte créé (#{coloc_id}).")
                    st.rerun()
                except bd.ErreurBase as e:
                    st.error(str(e))


# --------------------------------------------------------------------------
# Accueil
# --------------------------------------------------------------------------

def page_accueil(moi):
    # Bilan de la validation précédente, survivant au st.rerun().
    if "dernier_envoi" in st.session_state:
        titre, envoyes, ignores = st.session_state.pop("dernier_envoi")
        st.success(titre)
        if envoyes:
            st.caption("📲 Coloc prévenue : " + ", ".join(envoyes))
        if ignores:
            st.caption(
                "⚠️ Pas prévenus, WhatsApp non configuré : " + ", ".join(ignores)
            )

    designe = sched.prochain_coloc()
    if not designe:
        if bd.get_colocs():
            st.warning(
                "Tout le monde est marqué absent : personne ne peut être désigné. "
                "Un admin doit marquer quelqu'un de retour."
            )
        else:
            st.warning("Aucun colocataire actif. Ajoute du monde depuis la page Admin.")
        return

    demande = bd.demande_ouverte()

    if demande is None:
        # --- Rien à faire : n'importe qui peut signaler ----------------------
        st.success("✅ Rien à faire — les poubelles ne sont pas pleines.")
        st.caption(
            f"Prochaine personne de tour : **{designe['nom']}** "
            "(elle sera prévenue au signalement)."
        )
        if st.button(
            "🔔 Les poubelles sont pleines", use_container_width=True, type="primary"
        ):
            try:
                bd.ouvrir_demande(moi["id"])
                envoyes, ignores = notif.annoncer_demande(moi, designe)
                st.session_state["dernier_envoi"] = (
                    f"{designe['nom']} a été prévenu·e",
                    envoyes,
                    ignores,
                )
                st.rerun()
            except bd.ErreurBase as e:
                st.error(str(e))
    else:
        # --- Signalement en cours : la personne de tour doit valider ---------
        depuis = sched.delai_lisible(
            (bd.heure() - datetime.fromisoformat(demande["ts"])).total_seconds()
            / 3600
        )
        st.error(f"🗑️ À descendre ! Signalé par **{demande['nom_demandeur']}**, "
                 f"il y a {depuis}.")

        gauche, droite = st.columns([1, 2])
        with gauche:
            avatar(designe)
        with droite:
            st.markdown(f"### {designe['nom']}")
            st.markdown("🎯 *C'est ton tour !*" if designe["id"] == moi["id"]
                        else "C'est à cette personne de s'en charger.")
            prochaine = sched.prochaine_date()
            st.caption(f"Prochaine collecte : {sched.date_lisible(prochaine)}")

        if sched.peut_valider(moi):
            photo = st.file_uploader(
                "📸 Photo de la poubelle sortie (optionnel, ça rassure tout le monde)",
                type=["png", "jpg", "jpeg", "webp"],
                key="preuve",
            )
            if st.button(
                "🗑️ J'ai descendu les poubelles",
                use_container_width=True,
                type="primary",
            ):
                try:
                    chemin = enregistrer_photo(photo, "poubelles")
                    bd.ajouter_sortie(moi["id"], chemin, demande)
                    suivant = sched.prochain_coloc()
                    envoyes, ignores = notif.annoncer_validation(moi, suivant)
                    # Conservé en session : st.rerun() efface tout ce qui précède.
                    st.session_state["dernier_envoi"] = (
                        f"Validé ! +{bd.POINTS_PAR_SORTIE} points 🎉",
                        envoyes,
                        ignores,
                    )
                    st.balloons()
                    st.rerun()
                except bd.ErreurBase as e:
                    st.error(str(e))
        else:
            st.info(f"Seul·e **{designe['nom']}** peut valider ce tour.")

        with st.expander("Fausse alerte ?"):
            st.caption("Annule le signalement sans enregistrer de sortie.")
            if st.button("Annuler le signalement"):
                bd.annuler_demande()
                st.rerun()

    st.divider()

    st.subheader("Tableau de bord")
    c1, c2, c3 = st.columns(3)
    with c1:
        carte("👤 De tour", designe["nom"])
    with c2:
        carte("🗑️ État", "À descendre" if demande else "Rien à faire")
    with c3:
        carte("🔥 Série", f"{sched.serie_actuelle()}")

    derniere = bd.derniere_sortie()
    if derniere:
        st.caption(
            f"Dernière validation faite par **{derniere['personne']}** "
            f"le {formater_ts(derniere['ts'])}."
        )

    with st.expander("📋 Ordre de passage"):
        if bd.get_tour_force():
            st.caption("⚠️ Tour imposé par l'admin, le roulement reprend ensuite.")
        for i, coloc in enumerate(bd.get_colocs()):
            marque = "👉 " if coloc["id"] == designe["id"] else ""
            absent = " — 🏖️ absent·e" if coloc["absent"] else ""
            st.write(f"{marque}{i + 1}. {coloc['nom']}{absent}")


def formater_ts(ts):
    try:
        d = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ts or "?"
    return f"{d.day} {sched.MOIS_FR[d.month - 1]} {d.year} {d:%H:%M}"


# --------------------------------------------------------------------------
# Historique et classement
# --------------------------------------------------------------------------

def page_historique():
    st.subheader("📜 Historique")
    entrees = bd.get_historique(limite=60)
    if not entrees:
        st.info("Rien à afficher pour le moment.")
        return

    limite = bd.delai_max_heures()
    for entree in entrees:
        delai = entree["delai_heures"]
        if delai is None:
            mention = ""
        elif delai > limite:
            mention = f" ⏰ *{sched.delai_lisible(delai)} après le signalement*"
        else:
            mention = f" · en {sched.delai_lisible(delai)}"
        saisie = (
            f" · ✍️ saisi par {entree['saisi_par']}"
            if entree["saisi_par"] and entree["enregistre_par"] != entree["coloc_id"]
            else ""
        )
        st.markdown(
            f"✅ **{entree['personne']}** — {formater_ts(entree['ts'])}{mention}{saisie}"
        )
        preuve = bd.lire_photo(entree["photo"])
        if preuve:
            st.image(preuve, width=220)


def page_classement():
    st.subheader("🏆 Classement des meilleurs colocataires")
    medailles = ["🥇", "🥈", "🥉"]
    lignes = bd.classement()
    if not lignes:
        st.info("Pas encore de colocataires.")
        return
    for i, ligne in enumerate(lignes):
        rang = medailles[i] if i < len(medailles) else f"{i + 1}."
        gauche, droite = st.columns([1, 4])
        with gauche:
            avatar(ligne, taille=56)
        with droite:
            st.markdown(
                f"**{rang} {ligne['nom']}** — {ligne['points']} pts "
                f"({ligne['sorties']} sorties)"
            )


# --------------------------------------------------------------------------
# Mon compte
# --------------------------------------------------------------------------

def page_compte(moi):
    st.subheader("⚙️ Mon compte")
    st.write(f"Connecté en tant que **{moi['nom']}** (`{moi['pseudo']}`)")
    avatar(moi, taille=100)

    with st.form("photo_profil"):
        photo = st.file_uploader(
            "Changer ma photo de profil", type=["png", "jpg", "jpeg", "webp"]
        )
        if st.form_submit_button("Mettre à jour la photo"):
            try:
                chemin = enregistrer_photo(photo, "profils")
                if chemin:
                    bd.maj_coloc(moi["id"], photo=chemin)
                    st.success("Photo mise à jour.")
                    st.rerun()
                else:
                    st.warning("Choisis d'abord une image.")
            except bd.ErreurBase as e:
                st.error(str(e))

    st.divider()
    st.markdown("#### 📲 Notifications WhatsApp")
    if notif.configure(moi):
        st.success(f"Activées sur le {moi['telephone']}.")
    else:
        st.warning("Pas encore activées : tu ne seras pas prévenu de ton tour.")

    with st.expander("Comment activer (à faire une seule fois)"):
        st.markdown(
            f"""
1. Enregistre **{notif.NUMERO_ACTIVATION}** dans tes contacts.
2. Envoie-lui sur WhatsApp, mot pour mot :
   `{notif.PHRASE_ACTIVATION}`
3. Il te répond ta **clé API** (quelques chiffres) en 2 minutes environ.
4. Recopie ton numéro et cette clé ci-dessous.

L'API gratuite est personnelle : chacun active son propre numéro, personne
ne peut en inscrire un autre à sa place.
            """
        )

    with st.form("whatsapp"):
        tel = st.text_input(
            "Mon numéro (format international)",
            value=moi["telephone"] or "",
            placeholder="+33612345678",
        )
        cle = st.text_input(
            "Ma clé CallMeBot",
            value=moi["whatsapp_apikey"] or "",
            type="password",
        )
        enregistrer, tester = st.columns(2)
        valider = enregistrer.form_submit_button("Enregistrer", use_container_width=True)
        essai = tester.form_submit_button("Envoyer un test", use_container_width=True)

        if valider or essai:
            try:
                bd.maj_coloc(
                    moi["id"],
                    telephone=bd.valider_telephone(tel),
                    whatsapp_apikey=bd.valider_apikey(cle),
                )
                if essai:
                    ok, detail = notif.envoyer_test(bd.get_coloc(moi["id"]))
                    if ok:
                        st.success(f"Message envoyé ! Regarde ton WhatsApp. ({detail})")
                    else:
                        st.error(f"Échec de l'envoi : {detail}")
                else:
                    st.success("Enregistré.")
                    st.rerun()
            except bd.ErreurBase as e:
                st.error(str(e))

    st.divider()
    with st.form("mot_de_passe"):
        ancien = st.text_input("Mot de passe actuel", type="password")
        nouveau = st.text_input("Nouveau mot de passe", type="password")
        confirmation = st.text_input("Confirmer", type="password")
        if st.form_submit_button("Changer mon mot de passe"):
            try:
                auth.changer_mot_de_passe(moi["id"], ancien, nouveau, confirmation)
                st.success("Mot de passe modifié.")
            except bd.ErreurBase as e:
                st.error(str(e))


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def page_admin(moi):
    if not moi["is_admin"]:
        st.error("Accès réservé à l'administrateur.")
        return

    st.subheader("🛠️ Administration")
    libelles_mode = {
        "turso-replique": "☁️ Turso — réplique locale (rapide)",
        "turso-direct": "☁️ Turso — accès direct (réplique indisponible, un peu plus lent)",
        "sqlite": "💾 Fichier SQLite local",
    }
    st.caption(f"Base de données : {libelles_mode.get(bd.MODE, bd.MODE)}")

    # ---- Vue d'ensemble ---------------------------------------------------
    stats = bd.stats_globales()
    st.markdown("#### 📊 Vue d'ensemble")
    l1 = st.columns(3)
    l1[0].metric("Sorties totales", stats["total"])
    l1[1].metric("Ce mois-ci", stats["ce_mois"])
    l1[2].metric("Série en cours", sched.serie_actuelle())

    l2 = st.columns(3)
    l2[0].metric(
        "Ponctualité",
        f"{stats['ponctualite']}%" if stats["ponctualite"] is not None else "—",
        help="Part des sorties faites au plus tard le jour prévu.",
    )
    l2[1].metric(
        "Comptes créés",
        f"{stats['avec_compte']}/{stats['actifs']}",
        help="Colocataires actifs ayant des identifiants.",
    )
    l2[2].metric(
        "Preuves photo",
        stats["avec_photo"],
        help="Validations accompagnées d'une photo.",
    )

    designe = sched.prochain_coloc()
    demande = bd.demande_ouverte()
    if designe and demande:
        st.warning(
            f"🗑️ Signalé plein par {demande['nom_demandeur']} "
            f"({formater_ts(demande['ts'])}) — au tour de **{designe['nom']}**."
        )
    elif designe:
        st.info(f"Rien à descendre. Prochain de tour : **{designe['nom']}**.")

    # ---- Détail par colocataire ------------------------------------------
    st.markdown("#### 👥 Activité par colocataire")
    detail = bd.stats_par_coloc()
    # Barres en CSS plutôt que st.bar_chart : la conversion Altair/pyarrow
    # provoque un crash natif du processus après quelques rendus successifs.
    maxi = max((d["sorties"] for d in detail), default=0)
    for d in detail:
        derniere = formater_ts(d["derniere"]) if d["derniere"] else "jamais"
        retards = d["retards"] or 0
        largeur = round(100 * d["sorties"] / maxi) if maxi else 0
        alerte = f" · ⏰ {retards} en retard" if retards else ""
        st.markdown(
            f"""
            <div style="margin-bottom:.6rem">
              <div style="display:flex;justify-content:space-between;font-size:.9rem">
                <b>{html.escape(d['nom'])}</b><span>{d['sorties']} sorties</span>
              </div>
              <div style="background:rgba(128,128,128,.18);border-radius:6px;height:10px">
                <div style="width:{largeur}%;background:#2e7d32;height:10px;
                            border-radius:6px"></div>
              </div>
              <div style="font-size:.75rem;opacity:.7">
                {d['points']} pts{alerte} · dernière : {html.escape(derniere)}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("#### Colocataires")
    appareils = bd.sessions_actives()
    for coloc in bd.get_colocs(actifs_seulement=False):
        etat = "" if coloc["actif"] else " · désactivé"
        admin = " 👑" if coloc["is_admin"] else ""
        wa = " 📲" if notif.configure(coloc) else ""
        titre = (
            f"{coloc['nom']}{admin}{wa} — {coloc['pseudo'] or 'sans compte'}"
            f" · {coloc['points']} pts{etat}"
        )
        with st.expander(titre):
            st.caption(
                f"Appareils connectés : {appareils.get(coloc['id'], 0)} · "
                f"créé le {coloc['cree_le'] or '?'}"
            )

            if coloc["actif"]:
                # Absence : hors roulement et sans notification, points conservés.
                if coloc["absent"]:
                    st.warning("🏖️ Absent·e — hors roulement.")
                    if st.button("Marquer de retour", key=f"back{coloc['id']}"):
                        bd.maj_coloc(coloc["id"], absent=0)
                        bd.journaliser_action(moi["id"], "Retour de vacances", coloc["nom"])
                        st.rerun()
                elif st.button("🏖️ Marquer absent·e (vacances)", key=f"away{coloc['id']}"):
                    if len(sched.colocs_disponibles()) <= 1:
                        st.error("Impossible : il ne resterait personne pour sortir les poubelles.")
                    else:
                        bd.maj_coloc(coloc["id"], absent=1)
                        bd.journaliser_action(moi["id"], "Marqué absent", coloc["nom"])
                        if designe and designe["id"] == coloc["id"]:
                            bd.definir_tour(None)  # le roulement recalculera
                        st.rerun()

                a1, a2 = st.columns(2)
                if coloc["id"] != moi["id"]:
                    if a1.button("Retirer de la coloc", key=f"del{coloc['id']}"):
                        bd.supprimer_coloc(coloc["id"])
                        bd.journaliser_action(moi["id"], "Coloc retiré", coloc["nom"])
                        st.rerun()
                    libelle = "Retirer admin" if coloc["is_admin"] else "Passer admin"
                    if a2.button(libelle, key=f"adm{coloc['id']}"):
                        bd.maj_coloc(coloc["id"], is_admin=0 if coloc["is_admin"] else 1)
                        bd.journaliser_action(moi["id"], "Droits admin " + ("retirés" if coloc["is_admin"] else "donnés"),
                                              coloc["nom"])
                        st.rerun()
                else:
                    a1.caption("C'est toi : tu ne peux ni te retirer ni te rétrograder.")
                if appareils.get(coloc["id"]):
                    if st.button("Déconnecter tous ses appareils", key=f"out{coloc['id']}"):
                        bd.deconnecter_partout(coloc["id"])
                        bd.journaliser_action(moi["id"], "Appareils déconnectés", coloc["nom"])
                        st.rerun()
            else:
                if st.button("Réactiver", key=f"on{coloc['id']}"):
                    bd.maj_coloc(coloc["id"], actif=1)
                    bd.journaliser_action(moi["id"], "Coloc réactivé", coloc["nom"])
                    st.rerun()

            # Pas d'email dans l'app : la réinitialisation passe par l'admin.
            if coloc["pseudo"]:
                with st.form(f"mdp{coloc['id']}"):
                    nouveau = st.text_input(
                        "Nouveau mot de passe", type="password", key=f"np{coloc['id']}"
                    )
                    if st.form_submit_button("Réinitialiser son mot de passe"):
                        try:
                            auth.valider_mot_de_passe(nouveau, nouveau)
                            bd.maj_coloc(coloc["id"], password_hash=auth.hacher(nouveau))
                            bd.deconnecter_partout(coloc["id"])
                            bd.journaliser_action(moi["id"], "Mot de passe réinitialisé", coloc["nom"])
                            st.success(
                                f"Mot de passe de {coloc['nom']} changé. "
                                "Ses appareils ont été déconnectés."
                            )
                        except bd.ErreurBase as e:
                            st.error(str(e))

    with st.form("ajout_coloc"):
        nom = st.text_input("Ajouter un colocataire (sans compte pour l'instant)")
        if st.form_submit_button("Ajouter"):
            try:
                bd.creer_coloc(nom)
                bd.journaliser_action(moi["id"], "Coloc ajouté", nom.strip())
                st.rerun()
            except bd.ErreurBase as e:
                st.error(str(e))

    st.markdown("#### Ordre de passage")
    actifs = bd.get_colocs()
    noms = [c["nom"] for c in actifs]
    nouvel_ordre = st.multiselect(
        "Glisse les prénoms dans l'ordre souhaité (tous doivent être listés)",
        noms,
        default=noms,
    )
    if st.button("Enregistrer l'ordre"):
        if sorted(nouvel_ordre) != sorted(noms):
            st.error("Il faut lister tous les colocataires, une seule fois chacun.")
        else:
            index = {c["nom"]: c["id"] for c in actifs}
            bd.definir_ordre([index[n] for n in nouvel_ordre])
            bd.journaliser_action(moi["id"], "Ordre modifié", " → ".join(nouvel_ordre))
            st.success("Ordre mis à jour.")
            st.rerun()

    st.markdown("#### Délai de réaction")
    delai = st.number_input(
        "Heures après un signalement avant de compter la sortie en retard",
        min_value=1.0,
        max_value=168.0,
        value=bd.delai_max_heures(),
        step=1.0,
    )
    if st.button("Enregistrer le délai"):
        bd.set_reglage("delai_max_heures", delai)
        bd.journaliser_action(moi["id"], "Délai modifié", f"{delai:g} h")
        st.success("Délai mis à jour.")
        st.rerun()

    st.markdown("#### Jours de collecte (information)")
    st.caption(
        "Sert seulement à afficher la prochaine collecte de la ville. "
        "Les poubelles se descendent sur signalement, pas à ces dates."
    )
    jours_actuels = sched.jours_collecte()
    choix = st.multiselect(
        "Jours de ramassage",
        options=list(range(7)),
        default=jours_actuels,
        format_func=lambda i: sched.JOURS_FR[i].capitalize(),
    )
    if st.button("Enregistrer les jours"):
        if not choix:
            st.error("Choisis au moins un jour.")
        else:
            bd.set_reglage("jours_collecte", ",".join(str(j) for j in sorted(choix)))
            st.success("Jours mis à jour.")
            st.rerun()

    st.markdown("#### Notifications")
    if designe and st.button("📣 Rappeler son tour à toute la coloc"):
        rappel = (
            f"\U0001F5D1 Rappel : c'est au tour de {designe['nom']} "
            "de descendre les poubelles."
        )
        envoyes, ignores = notif.diffuser(rappel)
        if envoyes:
            st.success("Envoyé à : " + ", ".join(envoyes))
        if ignores:
            st.warning("Non joignables : " + ", ".join(ignores))


    manquants = [c["nom"] for c in bd.get_colocs() if not notif.configure(c)]
    if manquants:
        st.caption(
            "Sans WhatsApp configuré (ils ne seront pas prévenus) : "
            + ", ".join(manquants)
        )
    journal = bd.get_notifications()
    if journal:
        for n in journal:
            st.caption(f"[{n['ts']}] → {n['nom'] or '?'} : {n['statut']}")
    else:
        st.caption("Aucune notification pour l'instant.")

    st.markdown("#### Gestion du tour")
    g1, g2 = st.columns(2)
    if designe and g1.button(f"⏭️ Passer le tour de {designe['nom']}"):
        try:
            suivant = sched.passer_tour()
            bd.journaliser_action(moi["id"], "Tour passé", f"{designe['nom']} → {suivant['nom']}")
            st.success(f"Tour passé à {suivant['nom']}.")
            st.rerun()
        except bd.ErreurBase as e:
            st.error(str(e))

    dispos = sched.colocs_disponibles()

    with st.expander("✅ Enregistrer une sortie pour quelqu'un"):
        st.caption(
            "Quand un coloc a sorti les poubelles sans passer par l'app. "
            "La saisie est tracée dans l'historique, pour rester honnête."
        )
        if dispos:
            noms_ok = {c["nom"]: c["id"] for c in dispos}
            defaut = 0
            courant = sched.prochain_coloc()
            if courant and courant["nom"] in noms_ok:
                defaut = list(noms_ok).index(courant["nom"])
            qui = st.selectbox("Qui l'a fait ?", list(noms_ok), index=defaut,
                               key="saisie_qui")
            if st.button("Enregistrer la sortie", key="saisie_go"):
                try:
                    cible = noms_ok[qui]
                    bd.ajouter_sortie(cible, None, bd.demande_ouverte(),
                                      enregistre_par=moi["id"])
                    bd.journaliser_action(moi["id"], "Sortie saisie", qui)
                    suivant = sched.prochain_coloc()
                    notif.annoncer_validation(bd.get_coloc(cible), suivant)
                    st.success(
                        f"Sortie enregistrée pour {qui} (+{bd.POINTS_PAR_SORTIE} pts). "
                        + (f"Au tour de {suivant['nom']}." if suivant else "")
                    )
                    st.rerun()
                except bd.ErreurBase as e:
                    st.error(str(e))
        else:
            st.caption("Personne de disponible.")

    if dispos:
        with g2.popover("🎯 Imposer qui fait le prochain tour"):
            noms = {c["nom"]: c["id"] for c in dispos}
            choisi = st.selectbox("Personne", list(noms), key="forcer_tour")
            if st.button("Imposer"):
                bd.definir_tour(noms[choisi])
                bd.journaliser_action(moi["id"], "Tour imposé", choisi)
                st.rerun()
            if bd.get_tour_force() and st.button("Revenir au roulement normal"):
                bd.definir_tour(None)
                st.rerun()
    if bd.get_tour_force():
        st.caption(
            f"⚠️ Tour imposé à **{bd.get_tour_force()['nom']}** — "
            "le roulement reprendra après sa validation."
        )

    st.markdown("#### Historique complet")
    tout = bd.get_historique(limite=10000)
    if tout:
        with st.expander(f"Voir et corriger les {len(tout)} sorties"):
            st.caption(
                "Annuler une sortie retire les points et rouvre le signalement : "
                "à utiliser si quelqu'un a validé sans rien descendre."
            )
            for e in tout:
                marque = "📷" if e["photo"] else "　"
                ligne, action = st.columns([4, 1])
                ligne.caption(f"{marque} {e['personne']} — {formater_ts(e['ts'])}")
                if action.button("Annuler", key=f"annul{e['id']}"):
                    try:
                        photo = bd.annuler_sortie(e["id"])
                        bd.supprimer_photo(photo)
                        bd.journaliser_action(
                            moi["id"], "Sortie annulée",
                            f"{e['personne']} — {formater_ts(e['ts'])}",
                        )
                        st.success(f"Sortie de {e['personne']} annulée.")
                        st.rerun()
                    except bd.ErreurBase as err:
                        st.error(str(err))
        csv = "date;colocataire;date_prevue;photo\n" + "\n".join(
            f"{e['ts']};{e['personne']};{e['date_prevue'] or ''};{e['photo'] or ''}"
            for e in tout
        )
        st.download_button(
            "⬇️ Exporter en CSV",
            csv.encode("utf-8"),
            file_name="poubelles-historique.csv",
            mime="text/csv",
        )
    else:
        st.caption("Aucune sortie enregistrée.")

    st.markdown("#### 📣 Annonce à la coloc")
    with st.form("annonce"):
        texte = st.text_area(
            "Message WhatsApp envoyé à tous ceux qui ont activé leurs notifications",
            max_chars=500,
            placeholder="Ex. : Grand ménage samedi 10 h, tout le monde est attendu 🧹",
        )
        if st.form_submit_button("Envoyer à toute la coloc"):
            if not texte.strip():
                st.error("Le message est vide.")
            else:
                envoyes, ignores = notif.diffuser(f"📣 {moi['nom']} : {texte.strip()}")
                bd.journaliser_action(
                    moi["id"], "Annonce envoyée",
                    f"{len(envoyes)} destinataire(s) — {texte.strip()[:80]}",
                )
                if envoyes:
                    st.success("Envoyé à : " + ", ".join(envoyes))
                if ignores:
                    st.warning("Non joignables (WhatsApp non configuré) : " + ", ".join(ignores))

    st.markdown("#### ⭐ Bonus / malus de points")
    tous = bd.get_colocs()
    if tous:
        with st.form("points"):
            noms_pts = {c["nom"]: c["id"] for c in tous}
            qui_pts = st.selectbox("Colocataire", list(noms_pts))
            delta = st.number_input(
                "Points à ajouter (négatif pour retirer)",
                min_value=-500, max_value=500, value=10, step=5,
            )
            motif = st.text_input("Motif (obligatoire, visible dans le journal)",
                                  max_chars=120)
            if st.form_submit_button("Appliquer"):
                try:
                    total = bd.ajuster_points(noms_pts[qui_pts], delta, motif, moi["id"])
                    st.success(f"{qui_pts} : {int(delta):+d} pts → {total} pts.")
                except bd.ErreurBase as e:
                    st.error(str(e))

    st.markdown("#### 🔑 Code d'invitation")
    st.caption(
        f"Code actuel : `{auth.code_invitation()}` — à donner aux nouveaux colocs. "
        "Change-le si tu penses qu'il a circulé."
    )
    with st.form("code_invitation"):
        nouveau_code = st.text_input("Nouveau code", max_chars=40)
        if st.form_submit_button("Changer le code"):
            nouveau_code = nouveau_code.strip()
            if len(nouveau_code) < 6:
                st.error("Le code doit faire au moins 6 caractères.")
            else:
                bd.set_reglage("code_invitation", nouveau_code)
                bd.journaliser_action(moi["id"], "Code d'invitation changé")
                st.success("Code mis à jour.")
                st.rerun()

    st.markdown("#### 🗂️ Journal des actions admin")
    journal_admin = bd.get_audit(limite=50)
    if journal_admin:
        with st.expander(f"Voir les {len(journal_admin)} dernières actions"):
            for a in journal_admin:
                detail = f" — {a['detail']}" if a["detail"] else ""
                st.caption(
                    f"{formater_ts(a['ts'])} · **{a['nom_admin'] or '?'}** · "
                    f"{a['action']}{html.escape(detail)}"
                )
    else:
        st.caption("Aucune action enregistrée pour l'instant.")

    st.markdown("#### 💾 Sauvegarde")
    st.caption(
        "Télécharge toute la base (comptes, historique, points, photos). "
        "⚠️ Le fichier contient les mots de passe hachés et les clés WhatsApp : "
        "garde-le pour toi."
    )
    if st.button("Préparer la sauvegarde"):
        st.session_state["sauvegarde"] = bd.exporter()
        bd.journaliser_action(moi["id"], "Sauvegarde téléchargée")
    if st.session_state.get("sauvegarde"):
        st.download_button(
            "⬇️ Télécharger la sauvegarde (.json)",
            st.session_state["sauvegarde"].encode("utf-8"),
            file_name=f"sauvegarde-poubelles-{bd.aujourdhui().isoformat()}.json",
            mime="application/json",
        )

    st.markdown("#### Zone rouge")
    with st.expander("Vider l'historique / réinitialiser le planning"):
        st.caption(
            "Remet tous les points à zéro. Coche la case pour effacer en plus "
            "toutes les sorties enregistrées."
        )
        effacer = st.checkbox("Vider tout l'historique (irréversible)")
        confirmation = st.text_input("Tape RESET pour confirmer")
        if st.button("Réinitialiser", type="primary"):
            if confirmation != "RESET":
                st.error("Confirmation incorrecte.")
            else:
                bd.reset_planning(effacer_historique=effacer)
                bd.journaliser_action(moi["id"], "Planning réinitialisé",
                                      "historique vidé" if effacer else "points remis à zéro")
                st.success("Planning réinitialisé.")
                st.rerun()


# --------------------------------------------------------------------------
# Routage
# --------------------------------------------------------------------------

    with st.expander("Restaurer une sauvegarde"):
        st.caption(
            "Remplace **toute** la base par le contenu du fichier. Tout le monde "
            "sera déconnecté et devra se reconnecter avec les mots de passe "
            "de la sauvegarde — toi compris."
        )
        fichier = st.file_uploader("Fichier de sauvegarde (.json)", type=["json"],
                                   key="restauration")
        confirmer = st.text_input("Tape RESTAURER pour confirmer", key="conf_restau")
        if st.button("Restaurer", type="primary", key="go_restau"):
            if fichier is None:
                st.error("Choisis d'abord un fichier.")
            elif confirmer != "RESTAURER":
                st.error("Confirmation incorrecte.")
            else:
                try:
                    bilan = bd.restaurer(fichier.getvalue().decode("utf-8"))
                    # Après restauration, l'id de l'admin peut ne plus exister :
                    # on journalise sans auteur plutôt que d'en attribuer un faux.
                    bd.journaliser_action(
                        None, "Base restaurée",
                        ", ".join(f"{t} {n}" for t, n in bilan.items()),
                    )
                    st.success("Restauration terminée. Reconnecte-toi.")
                    auth.deconnecter()
                    st.rerun()
                except (bd.ErreurBase, UnicodeDecodeError) as e:
                    st.error(f"Restauration impossible : {e}")

def application_connectee(moi):
    entete, bouton = st.columns([4, 1])
    entete.title("🗑️ Mission Poubelles")
    if bouton.button("Quitter"):
        auth.deconnecter()
        st.rerun()

    onglets = ["🏠 Accueil", "📜 Historique", "🏆 Classement", "⚙️ Compte"]
    if moi["is_admin"]:
        onglets.append("🛠️ Admin")

    vues = st.tabs(onglets)
    with vues[0]:
        page_accueil(moi)
    with vues[1]:
        page_historique()
    with vues[2]:
        page_classement()
    with vues[3]:
        page_compte(moi)
    if moi["is_admin"]:
        with vues[4]:
            page_admin(moi)


def main():
    moi = auth.utilisateur_courant()
    if moi is None:
        if st.session_state.pop("oublier", False):
            auth.oublier_navigateur()
        ecran_connexion()
    else:
        # Réécrit le cookie à chaque visite : l'échéance repart de zéro.
        auth.memoriser_navigateur(st.session_state.get("token"))
        application_connectee(moi)


try:
    _preparer_base()
    main()
except bd.ErreurBase as erreur:
    # Coupure réseau vers la base, par exemple : un message plutôt qu'une trace.
    st.error(f"😵 {erreur}")
    st.caption("Si ça persiste, recharge la page dans une minute.")
