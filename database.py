"""Couche d'accès SQLite pour Mission Poubelles.

Toutes les fonctions ouvrent une connexion courte et la referment : Streamlit
exécute les scripts dans plusieurs threads, une connexion globale ne serait pas
sûre. Le mode WAL permet aux lectures de ne pas bloquer les écritures.
"""

import os
import re
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta

DB = os.environ.get("POUBELLES_DB", "/app/data/poubelles.db")
UPLOADS = os.environ.get("POUBELLES_UPLOADS", "/app/data/uploads")

POINTS_PAR_SORTIE = 10
DUREE_SESSION_JOURS = 180

# Paramètres modifiables depuis la page admin, avec leurs valeurs par défaut.
REGLAGES_DEFAUT = {
    "jours_collecte": "0,2,4,6",  # lundi, mercredi, vendredi, dimanche
    # Au-delà de ce délai après un signalement, la sortie est comptée en retard.
    "delai_max_heures": "24",
}


class ErreurBase(Exception):
    """Erreur métier lisible, à afficher telle quelle à l'utilisateur."""


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise ErreurBase(f"Erreur base de données : {e}") from e
    finally:
        conn.close()


def maintenant():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


# --------------------------------------------------------------------------
# Schéma et migration
# --------------------------------------------------------------------------

def _colonnes(cur, table):
    return {r["name"] for r in cur.execute(f"PRAGMA table_info({table})")}


def _ajouter_colonne(cur, table, nom, definition):
    """ALTER TABLE idempotent : ne fait rien si la colonne existe déjà."""
    if nom not in _colonnes(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {definition}")


def init_db():
    """Crée le schéma et migre l'ancienne base sans perdre de données."""
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(os.path.join(UPLOADS, "profils"), exist_ok=True)
    os.makedirs(os.path.join(UPLOADS, "poubelles"), exist_ok=True)

    with db() as conn:
        cur = conn.cursor()

        # Tables historiques de la v1 : on les crée si absentes, sinon on les
        # complète par ALTER TABLE juste en dessous.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS colocataires(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS historique(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                personne TEXT,
                date TEXT
            )
        """)

        for nom, definition in [
            ("pseudo", "TEXT"),
            ("password_hash", "TEXT"),
            ("photo", "TEXT"),
            ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
            ("points", "INTEGER NOT NULL DEFAULT 0"),
            ("ordre", "INTEGER NOT NULL DEFAULT 0"),
            ("actif", "INTEGER NOT NULL DEFAULT 1"),
            ("cree_le", "TEXT"),
            ("telephone", "TEXT"),
            ("whatsapp_apikey", "TEXT"),
            # Absent = hors rotation et sans notification, mais garde ses points.
            ("absent", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            _ajouter_colonne(cur, "colocataires", nom, definition)

        for nom, definition in [
            ("coloc_id", "INTEGER"),
            ("ts", "TEXT"),
            ("photo", "TEXT"),
            ("date_prevue", "TEXT"),
            ("points", "INTEGER NOT NULL DEFAULT 0"),
            ("demande_id", "INTEGER"),
            # Renseigné quand un admin saisit la sortie à la place du coloc.
            ("enregistre_par", "INTEGER"),
        ]:
            _ajouter_colonne(cur, "historique", nom, definition)

        # Les poubelles ne se descendent pas à date fixe : quelqu'un signale
        # qu'elles sont pleines, et la personne de tour valide ensuite.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS demandes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coloc_id INTEGER,
                ts TEXT NOT NULL,
                cloturee_le TEXT,
                historique_id INTEGER
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_demande_ouverte "
            "ON demandes(cloturee_le) WHERE cloturee_le IS NULL"
        )

        # Un pseudo doit rester unique, mais SQLite ne sait pas ajouter de
        # contrainte UNIQUE par ALTER TABLE : on passe par un index partiel.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pseudo_unique
            ON colocataires(pseudo) WHERE pseudo IS NOT NULL
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hist_ts ON historique(ts DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                coloc_id INTEGER NOT NULL,
                cree_le TEXT NOT NULL,
                expire_le TEXT NOT NULL,
                FOREIGN KEY(coloc_id) REFERENCES colocataires(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reglages(
                cle TEXT PRIMARY KEY,
                valeur TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coloc_id INTEGER,
                canal TEXT NOT NULL,
                message TEXT NOT NULL,
                ts TEXT NOT NULL,
                statut TEXT NOT NULL
            )
        """)

        for cle, valeur in REGLAGES_DEFAUT.items():
            cur.execute(
                "INSERT OR IGNORE INTO reglages(cle, valeur) VALUES (?, ?)",
                (cle, valeur),
            )

        _backfill(cur)


def _backfill(cur):
    """Remplit les colonnes ajoutées pour les lignes créées par la v1."""
    cur.execute("UPDATE colocataires SET ordre = id WHERE ordre = 0")
    cur.execute("UPDATE colocataires SET cree_le = ? WHERE cree_le IS NULL", (maintenant(),))

    # L'historique v1 stockait la date en '%d/%m/%Y %H:%M', non triable.
    # On la convertit en ISO pour pouvoir trier et comparer.
    for row in cur.execute("SELECT id, date FROM historique WHERE ts IS NULL").fetchall():
        try:
            ts = datetime.strptime(row["date"], "%d/%m/%Y %H:%M").isoformat(sep=" ")
        except (ValueError, TypeError):
            ts = "1970-01-01 00:00:00"
        cur.execute("UPDATE historique SET ts = ? WHERE id = ?", (ts, row["id"]))

    cur.execute("""
        UPDATE historique SET coloc_id = (
            SELECT c.id FROM colocataires c WHERE c.nom = historique.personne
        ) WHERE coloc_id IS NULL
    """)


# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------

def get_reglage(cle, defaut=None):
    with db() as conn:
        row = conn.execute("SELECT valeur FROM reglages WHERE cle = ?", (cle,)).fetchone()
    if row:
        return row["valeur"]
    return REGLAGES_DEFAUT.get(cle, defaut)


def set_reglage(cle, valeur):
    with db() as conn:
        conn.execute(
            "INSERT INTO reglages(cle, valeur) VALUES (?, ?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
            (cle, str(valeur)),
        )


# --------------------------------------------------------------------------
# Validation des entrées
# --------------------------------------------------------------------------

RE_PSEUDO = re.compile(r"^[a-zA-Z0-9_.-]{3,20}$")
RE_NOM = re.compile(r"^[\w' -]{2,30}$", re.UNICODE)
# Format international E.164, seul accepté par l'API WhatsApp.
RE_TEL = re.compile(r"^\+[1-9]\d{7,14}$")
RE_APIKEY = re.compile(r"^[0-9]{4,12}$")


def valider_pseudo(pseudo):
    pseudo = (pseudo or "").strip()
    if not RE_PSEUDO.match(pseudo):
        raise ErreurBase(
            "Le pseudo doit faire 3 à 20 caractères (lettres, chiffres, . _ -)."
        )
    return pseudo


def valider_nom(nom):
    nom = (nom or "").strip()
    if not RE_NOM.match(nom):
        raise ErreurBase("Le prénom doit faire 2 à 30 caractères.")
    return nom


def valider_telephone(tel):
    """Normalise un numéro au format E.164 (+33612345678)."""
    tel = re.sub(r"[\s.\-()]", "", tel or "")
    if tel.startswith("00"):
        tel = "+" + tel[2:]
    # CallMeBot renvoie le numéro sans « + » (ex. 33767661843) : on l'accepte.
    # Un numéro local comme 0612345678 reste refusé, le pays est indevinable.
    elif tel.isdigit() and not tel.startswith("0"):
        tel = "+" + tel
    if not RE_TEL.match(tel):
        raise ErreurBase(
            "Numéro invalide. Utilise le format international, ex. +33612345678."
        )
    return tel


def valider_apikey(cle):
    cle = (cle or "").strip()
    if not RE_APIKEY.match(cle):
        raise ErreurBase("La clé CallMeBot est un nombre de 4 à 12 chiffres.")
    return cle


def notification_recente(coloc_id, message, minutes=30):
    """Évite de renvoyer le même message trop souvent (quotas CallMeBot)."""
    seuil = (datetime.now() - timedelta(minutes=minutes)).replace(
        microsecond=0
    ).isoformat(sep=" ")
    with db() as conn:
        row = conn.execute(
            """SELECT 1 FROM notifications
               WHERE coloc_id = ? AND message = ? AND statut = 'envoyé' AND ts >= ?
               LIMIT 1""",
            (coloc_id, message, seuil),
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Colocataires
# --------------------------------------------------------------------------

def get_colocs(actifs_seulement=True, inclure_absents=True):
    """Colocataires triés par ordre de passage.

    `inclure_absents=False` sert au roulement et aux notifications : quelqu'un
    en vacances ne doit ni être désigné ni recevoir de messages.
    """
    conditions = []
    if actifs_seulement:
        conditions.append("actif = 1")
    if not inclure_absents:
        conditions.append("absent = 0")
    q = "SELECT * FROM colocataires"
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY ordre, id"
    with db() as conn:
        return [dict(r) for r in conn.execute(q)]


def get_coloc(coloc_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM colocataires WHERE id = ?", (coloc_id,)).fetchone()
    return dict(row) if row else None


def get_coloc_par_pseudo(pseudo):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM colocataires WHERE pseudo = ? COLLATE NOCASE", (pseudo,)
        ).fetchone()
    return dict(row) if row else None


def get_coloc_par_nom(nom):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM colocataires WHERE nom = ? COLLATE NOCASE", (nom,)
        ).fetchone()
    return dict(row) if row else None


def creer_coloc(nom, pseudo=None, password_hash=None, photo=None, is_admin=0):
    nom = valider_nom(nom)
    if pseudo is not None:
        pseudo = valider_pseudo(pseudo)
    with db() as conn:
        existant = conn.execute(
            "SELECT id FROM colocataires WHERE nom = ? COLLATE NOCASE", (nom,)
        ).fetchone()
        if existant:
            raise ErreurBase(f"Un colocataire nommé « {nom} » existe déjà.")
        if pseudo and conn.execute(
            "SELECT id FROM colocataires WHERE pseudo = ? COLLATE NOCASE", (pseudo,)
        ).fetchone():
            raise ErreurBase("Ce pseudo est déjà pris.")
        ordre = (conn.execute(
            "SELECT COALESCE(MAX(ordre), 0) AS m FROM colocataires"
        ).fetchone()["m"]) + 1
        cur = conn.execute(
            """INSERT INTO colocataires(nom, pseudo, password_hash, photo,
                                        is_admin, points, ordre, actif, cree_le)
               VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?)""",
            (nom, pseudo, password_hash, photo, int(is_admin), ordre, maintenant()),
        )
        return cur.lastrowid


def rattacher_compte(coloc_id, pseudo, password_hash, photo=None):
    """Donne des identifiants à un coloc créé par la v1 (sans compte)."""
    pseudo = valider_pseudo(pseudo)
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM colocataires WHERE id = ?", (coloc_id,)
        ).fetchone()
        if row is None:
            raise ErreurBase("Colocataire introuvable.")
        if row["password_hash"]:
            raise ErreurBase("Ce colocataire a déjà un compte.")
        if conn.execute(
            "SELECT id FROM colocataires WHERE pseudo = ? COLLATE NOCASE", (pseudo,)
        ).fetchone():
            raise ErreurBase("Ce pseudo est déjà pris.")
        conn.execute(
            "UPDATE colocataires SET pseudo = ?, password_hash = ?, "
            "photo = COALESCE(?, photo) WHERE id = ?",
            (pseudo, password_hash, photo, coloc_id),
        )


def maj_coloc(coloc_id, **champs):
    autorises = {"nom", "photo", "is_admin", "actif", "ordre", "password_hash",
                 "points", "telephone", "whatsapp_apikey", "absent"}
    champs = {k: v for k, v in champs.items() if k in autorises}
    if not champs:
        return
    if "nom" in champs:
        champs["nom"] = valider_nom(champs["nom"])
    sets = ", ".join(f"{k} = ?" for k in champs)
    with db() as conn:
        conn.execute(
            f"UPDATE colocataires SET {sets} WHERE id = ?",
            (*champs.values(), coloc_id),
        )


def supprimer_coloc(coloc_id):
    """Désactive le coloc plutôt que de le supprimer : l'historique reste lisible."""
    with db() as conn:
        conn.execute("UPDATE colocataires SET actif = 0 WHERE id = ?", (coloc_id,))
        conn.execute("DELETE FROM sessions WHERE coloc_id = ?", (coloc_id,))


def definir_ordre(ids_ordonnes):
    with db() as conn:
        for position, coloc_id in enumerate(ids_ordonnes, start=1):
            conn.execute(
                "UPDATE colocataires SET ordre = ? WHERE id = ?", (position, coloc_id)
            )


# --------------------------------------------------------------------------
# Historique
# --------------------------------------------------------------------------

def demande_ouverte():
    """Le signalement en cours, ou None si les poubelles n'ont pas besoin de sortir."""
    with db() as conn:
        row = conn.execute(
            """SELECT d.*, c.nom AS nom_demandeur FROM demandes d
               LEFT JOIN colocataires c ON c.id = d.coloc_id
               WHERE d.cloturee_le IS NULL ORDER BY d.id DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def ouvrir_demande(coloc_id):
    """Signale que les poubelles sont pleines. Renvoie la demande créée."""
    with db() as conn:
        deja = conn.execute(
            "SELECT id FROM demandes WHERE cloturee_le IS NULL LIMIT 1"
        ).fetchone()
        if deja:
            raise ErreurBase("Les poubelles sont déjà signalées comme pleines.")
        conn.execute(
            "INSERT INTO demandes(coloc_id, ts) VALUES (?, ?)", (coloc_id, maintenant())
        )
    return demande_ouverte()


def annuler_demande():
    """Fausse alerte : on referme le signalement sans sortie enregistrée."""
    with db() as conn:
        conn.execute(
            "UPDATE demandes SET cloturee_le = ? WHERE cloturee_le IS NULL",
            (maintenant(),),
        )


def delai_max_heures():
    try:
        return float(get_reglage("delai_max_heures", "24"))
    except (TypeError, ValueError):
        return 24.0


def ajouter_sortie(coloc_id, photo=None, demande=None, enregistre_par=None):
    """Enregistre une sortie, crédite les points et clôt le signalement."""
    with db() as conn:
        coloc = conn.execute(
            "SELECT * FROM colocataires WHERE id = ?", (coloc_id,)
        ).fetchone()
        if coloc is None:
            raise ErreurBase("Colocataire introuvable.")
        ts = maintenant()
        cur = conn.execute(
            """INSERT INTO historique(personne, date, coloc_id, ts, photo,
                                      date_prevue, points, demande_id,
                                      enregistre_par)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coloc["nom"],
                datetime.now().strftime("%d/%m/%Y %H:%M"),
                coloc_id,
                ts,
                photo,
                demande["ts"][:10] if demande else None,
                POINTS_PAR_SORTIE,
                demande["id"] if demande else None,
                enregistre_par,
            ),
        )
        conn.execute(
            "UPDATE colocataires SET points = points + ? WHERE id = ?",
            (POINTS_PAR_SORTIE, coloc_id),
        )
        if demande:
            conn.execute(
                "UPDATE demandes SET cloturee_le = ?, historique_id = ? WHERE id = ?",
                (ts, cur.lastrowid, demande["id"]),
            )
        # Un tour imposé par l'admin est consommé par la validation.
        conn.execute(
            "INSERT INTO reglages(cle, valeur) VALUES ('tour_force', '') "
            "ON CONFLICT(cle) DO UPDATE SET valeur = ''"
        )
        return ts


def annuler_sortie(historique_id):
    """Supprime une sortie déclarée à tort.

    Retire les points, remet le signalement en cours (les poubelles n'ont pas
    été descendues) et renvoie le chemin de la photo à effacer, s'il y en a une.
    """
    with db() as conn:
        entree = conn.execute(
            "SELECT * FROM historique WHERE id = ?", (historique_id,)
        ).fetchone()
        if entree is None:
            raise ErreurBase("Cette sortie n'existe plus.")

        if entree["coloc_id"]:
            conn.execute(
                "UPDATE colocataires SET points = MAX(0, points - ?) WHERE id = ?",
                (entree["points"] or 0, entree["coloc_id"]),
            )
        if entree["demande_id"]:
            # Le signalement redevient ouvert : il reste à descendre.
            conn.execute(
                "UPDATE demandes SET cloturee_le = NULL, historique_id = NULL "
                "WHERE id = ?",
                (entree["demande_id"],),
            )
        conn.execute("DELETE FROM historique WHERE id = ?", (historique_id,))
        return entree["photo"]


def get_tour_force():
    """Colocataire imposé pour le prochain tour, ou None."""
    valeur = get_reglage("tour_force", "")
    if not valeur:
        return None
    coloc = get_coloc(int(valeur)) if str(valeur).isdigit() else None
    if not coloc or not coloc["actif"] or coloc["absent"]:
        return None
    return coloc


def definir_tour(coloc_id):
    """Impose (ou libère, avec None) la personne du prochain tour."""
    set_reglage("tour_force", str(coloc_id) if coloc_id else "")


def get_historique(limite=50):
    with db() as conn:
        rows = conn.execute(
            """SELECT h.*, c.photo AS photo_profil, d.ts AS demande_ts,
                      a.nom AS saisi_par,
                      (julianday(h.ts) - julianday(d.ts)) * 24 AS delai_heures
               FROM historique h
               LEFT JOIN colocataires c ON c.id = h.coloc_id
               LEFT JOIN demandes d ON d.id = h.demande_id
               LEFT JOIN colocataires a ON a.id = h.enregistre_par
               ORDER BY h.ts DESC, h.id DESC LIMIT ?""",
            (limite,),
        ).fetchall()
    return [dict(r) for r in rows]


def derniere_sortie():
    h = get_historique(limite=1)
    return h[0] if h else None


def classement():
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.nom, c.photo, c.points,
                      (SELECT COUNT(*) FROM historique h WHERE h.coloc_id = c.id) AS sorties
               FROM colocataires c
               WHERE c.actif = 1
               ORDER BY c.points DESC, sorties DESC, c.nom"""
        ).fetchall()
    return [dict(r) for r in rows]


def stats_globales():
    """Chiffres clés du tableau de bord admin."""
    debut_mois = date.today().replace(day=1).isoformat()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM historique").fetchone()["n"]
        ce_mois = conn.execute(
            "SELECT COUNT(*) AS n FROM historique WHERE ts >= ?", (debut_mois,)
        ).fetchone()["n"]
        # Ponctualité : délai entre le signalement et la sortie effective.
        # Seules les sorties rattachées à un signalement sont mesurables.
        limite = delai_max_heures()
        suivies = conn.execute(
            "SELECT COUNT(*) AS n FROM historique WHERE demande_id IS NOT NULL"
        ).fetchone()["n"]
        a_lheure = conn.execute(
            """SELECT COUNT(*) AS n FROM historique h JOIN demandes d ON d.id = h.demande_id
               WHERE (julianday(h.ts) - julianday(d.ts)) * 24 <= ?""",
            (limite,),
        ).fetchone()["n"]
        actifs = conn.execute(
            "SELECT COUNT(*) AS n FROM colocataires WHERE actif = 1"
        ).fetchone()["n"]
        avec_compte = conn.execute(
            "SELECT COUNT(*) AS n FROM colocataires "
            "WHERE actif = 1 AND password_hash IS NOT NULL"
        ).fetchone()["n"]
        avec_photo = conn.execute(
            "SELECT COUNT(*) AS n FROM historique WHERE photo IS NOT NULL"
        ).fetchone()["n"]
    return {
        "total": total,
        "ce_mois": ce_mois,
        "suivies": suivies,
        "a_lheure": a_lheure,
        "ponctualite": round(100 * a_lheure / suivies) if suivies else None,
        "actifs": actifs,
        "avec_compte": avec_compte,
        "avec_photo": avec_photo,
    }


def stats_par_coloc():
    """Par colocataire : sorties, retards, dernière participation."""
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.nom, c.points, c.actif,
                      COUNT(h.id) AS sorties,
                      SUM(CASE WHEN d.id IS NOT NULL
                                AND (julianday(h.ts) - julianday(d.ts)) * 24 > ?
                               THEN 1 ELSE 0 END) AS retards,
                      MAX(h.ts) AS derniere
               FROM colocataires c
               LEFT JOIN historique h ON h.coloc_id = c.id
               LEFT JOIN demandes d ON d.id = h.demande_id
               WHERE c.actif = 1
               GROUP BY c.id
               ORDER BY sorties DESC, c.nom""",
            (delai_max_heures(),),
        ).fetchall()
    return [dict(r) for r in rows]


def sessions_actives():
    """Nombre d'appareils connectés par colocataire."""
    with db() as conn:
        rows = conn.execute(
            """SELECT coloc_id, COUNT(*) AS n FROM sessions
               WHERE expire_le >= ? GROUP BY coloc_id""",
            (maintenant(),),
        ).fetchall()
    return {r["coloc_id"]: r["n"] for r in rows}


def deconnecter_partout(coloc_id):
    """Invalide toutes les sessions d'un coloc (appareil perdu, compte partagé…)."""
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE coloc_id = ?", (coloc_id,))


def reset_planning(effacer_historique=False):
    """Remet les compteurs à zéro. L'historique n'est effacé que sur demande."""
    with db() as conn:
        conn.execute("UPDATE colocataires SET points = 0")
        conn.execute("UPDATE reglages SET valeur = '' WHERE cle = 'tour_force'")
        if effacer_historique:
            conn.execute("DELETE FROM historique")
            # Sans ça, des signalements orphelins resteraient ouverts.
            conn.execute("DELETE FROM demandes")


# --------------------------------------------------------------------------
# Sessions persistantes
# --------------------------------------------------------------------------

def creer_session(coloc_id):
    token = secrets.token_urlsafe(32)
    expire = datetime.now() + timedelta(days=DUREE_SESSION_JOURS)
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions(token, coloc_id, cree_le, expire_le) VALUES (?, ?, ?, ?)",
            (token, coloc_id, maintenant(), expire.replace(microsecond=0).isoformat(sep=" ")),
        )
    return token


def coloc_par_session(token):
    if not token:
        return None
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expire_le < ?", (maintenant(),))
        row = conn.execute(
            """SELECT c.* FROM sessions s JOIN colocataires c ON c.id = s.coloc_id
               WHERE s.token = ? AND s.expire_le >= ? AND c.actif = 1""",
            (token, maintenant()),
        ).fetchone()
        if row:
            # Fenêtre glissante : tant qu'on s'en sert, la session ne périme pas.
            expire = datetime.now() + timedelta(days=DUREE_SESSION_JOURS)
            conn.execute(
                "UPDATE sessions SET expire_le = ? WHERE token = ?",
                (expire.replace(microsecond=0).isoformat(sep=" "), token),
            )
    return dict(row) if row else None


def supprimer_session(token):
    if not token:
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --------------------------------------------------------------------------
# Journal des notifications
# --------------------------------------------------------------------------

def journaliser_notification(coloc_id, canal, message, statut):
    with db() as conn:
        conn.execute(
            "INSERT INTO notifications(coloc_id, canal, message, ts, statut) "
            "VALUES (?, ?, ?, ?, ?)",
            (coloc_id, canal, message, maintenant(), statut),
        )


def get_notifications(limite=30):
    with db() as conn:
        rows = conn.execute(
            """SELECT n.*, c.nom FROM notifications n
               LEFT JOIN colocataires c ON c.id = n.coloc_id
               ORDER BY n.id DESC LIMIT ?""",
            (limite,),
        ).fetchall()
    return [dict(r) for r in rows]
