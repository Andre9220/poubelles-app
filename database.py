"""Couche d'accès aux données de Mission Poubelles.

Deux modes, choisis automatiquement :

- **local** : un fichier SQLite (Umbrel, poste de dev). C'est le mode par défaut.
- **Turso** : dès que `TURSO_DATABASE_URL` est défini (Streamlit Cloud). La base
  vit chez Turso ; l'app en garde une *réplique embarquée* dans un fichier
  local. Les lectures se font sur la réplique, donc instantanément, et seules
  les écritures partent sur le réseau. Sans ça, un affichage de la page admin
  (~200 requêtes) coûterait plusieurs secondes d'allers-retours.

Une seule connexion est partagée par le processus, protégée par un verrou :
Streamlit exécute chaque session dans son propre thread.
"""

import base64
import json
import os
import re
import sqlite3
import secrets
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _config(nom, defaut=None):
    """Variable d'environnement, sinon secret Streamlit, sinon valeur par défaut."""
    valeur = os.environ.get(nom)
    if valeur:
        return valeur
    try:
        import streamlit as st

        return st.secrets.get(nom, defaut)
    except Exception:
        return defaut


_ICI = os.path.dirname(os.path.abspath(__file__))
DB = _config("POUBELLES_DB", os.path.join(_ICI, "data", "poubelles.db"))
# Ancien stockage des photos sur disque, conservé pour la migration vers la base.
UPLOADS = _config("POUBELLES_UPLOADS", os.path.join(_ICI, "data", "uploads"))
TURSO_URL = _config("TURSO_DATABASE_URL")
TURSO_TOKEN = _config("TURSO_AUTH_TOKEN", "")
DISTANT = bool(TURSO_URL)
# Streamlit Cloud clone le dépôt dans /mount/src : son disque est effacé à chaque
# redémarrage, donc une base SQLite locale y perdrait silencieusement tout.
SUR_STREAMLIT_CLOUD = _ICI.startswith("/mount/src/")

# Les serveurs de Streamlit Cloud sont en UTC et la variable TZ n'y est pas
# fiable : on calcule l'heure de Montréal explicitement, partout.
FUSEAU = ZoneInfo(_config("POUBELLES_FUSEAU", "America/Toronto"))

POINTS_PAR_SORTIE = 10
DUREE_SESSION_JOURS = 180
# Anti-force-brute : au-delà, le pseudo est bloqué pendant la fenêtre.
TENTATIVES_MAX = 5
FENETRE_TENTATIVES_MIN = 15

# Paramètres modifiables depuis la page admin, avec leurs valeurs par défaut.
REGLAGES_DEFAUT = {
    "jours_collecte": "0,2,4,6",  # lundi, mercredi, vendredi, dimanche
    # Au-delà de ce délai après un signalement, la sortie est comptée en retard.
    "delai_max_heures": "24",
}

# Tables sauvegardées et restaurées, dans l'ordre d'insertion. Les sessions et
# les tentatives de connexion n'en font volontairement pas partie.
TABLES_SAUVEGARDE = [
    "colocataires", "historique", "demandes", "reglages",
    "notifications", "audit", "photos",
]


class ErreurBase(Exception):
    """Erreur métier lisible, à afficher telle quelle à l'utilisateur."""


def heure():
    """Heure de Montréal, sans fuseau attaché (format stocké en base)."""
    return datetime.now(FUSEAU).replace(tzinfo=None, microsecond=0)


def aujourdhui():
    return heure().date()


def maintenant():
    return heure().isoformat(sep=" ")


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------

_ECRITURE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)", re.I)


class _Ligne:
    """Ligne accessible par nom ou par position, comme sqlite3.Row."""

    __slots__ = ("_valeurs", "_index")

    def __init__(self, valeurs, index):
        self._valeurs = valeurs
        self._index = index

    def __getitem__(self, cle):
        if isinstance(cle, str):
            return self._valeurs[self._index[cle]]
        return self._valeurs[cle]

    def keys(self):
        return self._index.keys()

    def __iter__(self):
        return iter(self._valeurs)

    def __len__(self):
        return len(self._valeurs)


class _Curseur:
    def __init__(self, curseur):
        self._c = curseur

    def _index(self):
        return {d[0]: i for i, d in enumerate(self._c.description or ())}

    def fetchone(self):
        ligne = self._c.fetchone()
        return None if ligne is None else _Ligne(ligne, self._index())

    def fetchall(self):
        index = self._index()
        return [_Ligne(l, index) for l in self._c.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._c.lastrowid


_SIGNES_LIAISON = ("hrana", "baton", "connect", "stream", "sync error", "timed out")


def _erreur_liaison(e):
    """Vrai si l'exception vient de la liaison avec Turso et non d'une requête fautive.

    libsql signale les coupures réseau par un ValueError (« Hrana: … »), pas par
    son propre type d'erreur : il faut donc regarder le message.
    """
    return isinstance(e, (ValueError, OSError, RuntimeError)) and any(
        signe in str(e).lower() for signe in _SIGNES_LIAISON
    )


class _ConnexionTurso:
    """Adapte libsql (qui renvoie des tuples) à l'interface de sqlite3.Row.

    Les flux Turso expirent après un moment d'inactivité : sans précaution, la
    première requête de la journée échouerait. Tant qu'aucune écriture n'est en
    cours, une liaison périmée est donc rouverte et la requête rejouée.
    """

    def __init__(self, fabrique, replique):
        self._fabrique = fabrique
        self._c = fabrique()
        self._replique = replique
        self.a_ecrit = False

    def execute(self, sql, params=()):
        try:
            curseur = self._c.execute(sql, tuple(params))
        except Exception as e:
            if self.a_ecrit or not _erreur_liaison(e):
                raise  # une transaction était entamée : la rejouer serait faux
            self._c = self._fabrique()
            curseur = self._c.execute(sql, tuple(params))
        if _ECRITURE.match(sql):
            self.a_ecrit = True
        return _Curseur(curseur)

    def cursor(self):
        return self

    def commit(self):
        self._c.commit()
        if self.a_ecrit and self._replique:
            # Garantit que la réplique locale voit l'écriture avant la lecture suivante.
            self._c.sync()
        self.a_ecrit = False

    def rollback(self):
        self.a_ecrit = False
        self._c.rollback()


# Mode effectivement utilisé, affiché dans la page admin pour le diagnostic.
MODE = "sqlite"


def _supprimer_replique(chemin):
    for suffixe in ("", "-wal", "-shm", "-info", "-client_wal_index"):
        try:
            os.remove(chemin + suffixe)
        except OSError:
            pass


def _ouvrir():
    global MODE
    if DISTANT:
        import libsql

        # Streamlit sert chaque session dans son propre thread : la connexion
        # est partagée, et le verrou de db() garantit un seul usage à la fois.
        replique = os.path.join(tempfile.gettempdir(), "poubelles-replique.db")
        def en_replique():
            conn = libsql.connect(
                replique, sync_url=TURSO_URL, auth_token=TURSO_TOKEN,
                sync_interval=30, _check_same_thread=False,
            )
            conn.sync()
            return conn

        def en_direct():
            return libsql.connect(
                database=TURSO_URL, auth_token=TURSO_TOKEN, _check_same_thread=False
            )

        try:
            connexion = _ConnexionTurso(en_replique, replique=True)
            MODE = "turso-replique"
        except Exception:
            # Réplique indisponible (protocole de synchro non supporté, disque
            # plein…) : on se rabat sur le mode distant direct, plus lent mais sûr.
            _supprimer_replique(replique)
            connexion = _ConnexionTurso(en_direct, replique=False)
            MODE = "turso-direct"
        return connexion

    if SUR_STREAMLIT_CLOUD:
        raise ErreurBase(
            "Configuration incomplète : les secrets TURSO_DATABASE_URL et "
            "TURSO_AUTH_TOKEN sont introuvables. Ajoute-les dans Settings → "
            "Secrets de l'app Streamlit, puis redémarre-la."
        )
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    MODE = "sqlite"
    return conn


_verrou = threading.RLock()
_connexion = None


def _erreurs_base():
    erreurs = [sqlite3.Error]
    if DISTANT:
        import libsql

        erreurs.append(libsql.Error)
    return tuple(erreurs)


@contextmanager
def db():
    global _connexion
    with _verrou:
        if _connexion is None:
            try:
                _connexion = _ouvrir()
            except ErreurBase:
                raise
            except Exception as e:
                raise ErreurBase(f"Base de données injoignable : {e}") from e
        conn = _connexion
        try:
            yield conn
            conn.commit()
        except Exception as e:
            _annuler(conn)
            liaison = DISTANT and _erreur_liaison(e)
            if liaison:
                # On repartira d'une connexion neuve au prochain appel.
                _connexion = None
            if liaison or isinstance(e, _erreurs_base()):
                raise ErreurBase(
                    "Base de données momentanément injoignable, réessaie."
                    if liaison else f"Erreur base de données : {e}"
                ) from e
            raise
        except BaseException:
            _annuler(conn)
            raise


def _annuler(conn):
    try:
        conn.rollback()
    except Exception:
        pass  # connexion déjà cassée : il n'y a plus rien à annuler


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

        # Photos stockées en base : Streamlit Cloud n'a pas de disque persistant.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS photos(
                id TEXT PRIMARY KEY,
                contenu BLOB NOT NULL,
                cree_le TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tentatives(
                cle TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tentatives ON tentatives(cle, ts)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                admin_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT
            )
        """)

        for cle, valeur in REGLAGES_DEFAUT.items():
            cur.execute(
                "INSERT OR IGNORE INTO reglages(cle, valeur) VALUES (?, ?)",
                (cle, valeur),
            )

        _backfill(cur)
        _migrer_photos_fichiers(cur)


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


def _migrer_photos_fichiers(cur):
    """Importe en base les photos encore stockées comme fichiers (ancienne version)."""
    for table in ("colocataires", "historique"):
        lignes = cur.execute(
            f"SELECT id, photo FROM {table} "
            "WHERE photo IS NOT NULL AND photo NOT LIKE 'db:%'"
        ).fetchall()
        for ligne in lignes:
            chemin = os.path.join(UPLOADS, ligne["photo"])
            if not os.path.isfile(chemin):
                continue  # fichier perdu : on garde la référence, elle s'affichera vide
            with open(chemin, "rb") as f:
                contenu = f.read()
            ref = uuid.uuid4().hex
            cur.execute(
                "INSERT INTO photos(id, contenu, cree_le) VALUES (?, ?, ?)",
                (ref, contenu, maintenant()),
            )
            cur.execute(
                f"UPDATE {table} SET photo = ? WHERE id = ?", (f"db:{ref}", ligne["id"])
            )


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

def stocker_photo(contenu):
    """Enregistre une image (octets JPEG) et renvoie sa référence « db:… »."""
    ref = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO photos(id, contenu, cree_le) VALUES (?, ?, ?)",
            (ref, contenu, maintenant()),
        )
    return f"db:{ref}"


def lire_photo(ref):
    """Octets de la photo, ou None si absente."""
    if not ref:
        return None
    if ref.startswith("db:"):
        with db() as conn:
            ligne = conn.execute(
                "SELECT contenu FROM photos WHERE id = ?", (ref[3:],)
            ).fetchone()
        return bytes(ligne["contenu"]) if ligne else None
    # Référence héritée : fichier sur disque (Umbrel, avant migration).
    chemin = os.path.join(UPLOADS, ref)
    if os.path.isfile(chemin):
        with open(chemin, "rb") as f:
            return f.read()
    return None


def supprimer_photo(ref):
    if ref and ref.startswith("db:"):
        with db() as conn:
            conn.execute("DELETE FROM photos WHERE id = ?", (ref[3:],))


# --------------------------------------------------------------------------
# Anti-force-brute
# --------------------------------------------------------------------------

def minutes_blocage(cle):
    """Minutes restantes de blocage pour ce pseudo, ou 0."""
    debut = (heure() - timedelta(minutes=FENETRE_TENTATIVES_MIN)).isoformat(sep=" ")
    with db() as conn:
        conn.execute("DELETE FROM tentatives WHERE ts < ?", (debut,))
        lignes = conn.execute(
            "SELECT ts FROM tentatives WHERE cle = ? ORDER BY ts", (cle,)
        ).fetchall()
    if len(lignes) < TENTATIVES_MAX:
        return 0
    liberation = datetime.fromisoformat(lignes[0]["ts"]) + timedelta(
        minutes=FENETRE_TENTATIVES_MIN
    )
    return max(1, int((liberation - heure()).total_seconds() // 60) + 1)


def noter_echec(cle):
    with db() as conn:
        conn.execute(
            "INSERT INTO tentatives(cle, ts) VALUES (?, ?)", (cle, maintenant())
        )


def effacer_echecs(cle):
    with db() as conn:
        conn.execute("DELETE FROM tentatives WHERE cle = ?", (cle,))


# --------------------------------------------------------------------------
# Journal d'audit des actions admin
# --------------------------------------------------------------------------

def journaliser_action(admin_id, action, detail=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO audit(ts, admin_id, action, detail) VALUES (?, ?, ?, ?)",
            (maintenant(), admin_id, action, detail),
        )


def get_audit(limite=50):
    with db() as conn:
        lignes = conn.execute(
            """SELECT a.*, c.nom AS nom_admin FROM audit a
               LEFT JOIN colocataires c ON c.id = a.admin_id
               ORDER BY a.id DESC LIMIT ?""",
            (limite,),
        ).fetchall()
    return [dict(l) for l in lignes]


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
    seuil = (heure() - timedelta(minutes=minutes)).isoformat(sep=" ")
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
                heure().strftime("%d/%m/%Y %H:%M"),
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
    debut_mois = aujourdhui().replace(day=1).isoformat()
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


def ajuster_points(coloc_id, delta, motif, admin_id):
    """Bonus ou malus manuel, tracé dans le journal d'audit. Renvoie le nouveau total."""
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise ErreurBase("Le nombre de points doit être un entier.")
    if delta == 0:
        raise ErreurBase("Indique un nombre de points différent de zéro.")
    motif = (motif or "").strip()[:120]
    if not motif:
        raise ErreurBase("Indique un motif : il apparaîtra dans le journal.")
    with db() as conn:
        coloc = conn.execute(
            "SELECT nom, points FROM colocataires WHERE id = ?", (coloc_id,)
        ).fetchone()
        if coloc is None:
            raise ErreurBase("Colocataire introuvable.")
        total = max(0, coloc["points"] + delta)
        conn.execute("UPDATE colocataires SET points = ? WHERE id = ?", (total, coloc_id))
        conn.execute(
            "INSERT INTO audit(ts, admin_id, action, detail) VALUES (?, ?, ?, ?)",
            (maintenant(), admin_id, "Points ajustés",
             f"{coloc['nom']} {delta:+d} → {total} pts ({motif})"),
        )
    return total


# --------------------------------------------------------------------------
# Sauvegarde et restauration
# --------------------------------------------------------------------------

FORMAT_SAUVEGARDE = "mission-poubelles"


def exporter():
    """Toute la base en JSON (photos en base64). Sans sessions ni tentatives."""
    donnees = {
        "format": FORMAT_SAUVEGARDE,
        "version": 1,
        "exporte_le": maintenant(),
        "tables": {},
    }
    with db() as conn:
        for table in TABLES_SAUVEGARDE:
            lignes = []
            for ligne in conn.execute(f"SELECT * FROM {table}").fetchall():
                d = dict(ligne)
                if table == "photos":
                    d["contenu"] = base64.b64encode(bytes(d["contenu"])).decode()
                lignes.append(d)
            donnees["tables"][table] = lignes
    return json.dumps(donnees, ensure_ascii=False)


def restaurer(texte):
    """Remplace toute la base par une sauvegarde. Renvoie le nombre de lignes par table.

    Tout se fait dans une seule transaction : en cas d'erreur, rien n'est modifié.
    Les sessions sont vidées, donc tout le monde devra se reconnecter.
    """
    try:
        donnees = json.loads(texte)
    except (ValueError, TypeError):
        raise ErreurBase("Fichier de sauvegarde illisible.")
    if not isinstance(donnees, dict) or donnees.get("format") != FORMAT_SAUVEGARDE:
        raise ErreurBase("Ce fichier n'est pas une sauvegarde Mission Poubelles.")
    tables = donnees.get("tables") or {}
    if not tables.get("colocataires"):
        raise ErreurBase("Sauvegarde sans colocataires : restauration refusée.")

    with db() as conn:
        for table in reversed(TABLES_SAUVEGARDE):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM tentatives")
        for table in TABLES_SAUVEGARDE:
            # Seules les colonnes réellement présentes sont reprises : les noms
            # viennent du fichier et ne doivent jamais atteindre le SQL tels quels.
            colonnes = _colonnes(conn, table)
            for ligne in tables.get(table) or []:
                d = {k: v for k, v in ligne.items() if k in colonnes}
                if not d:
                    continue
                if table == "photos":
                    try:
                        d["contenu"] = base64.b64decode(d["contenu"])
                    except (ValueError, TypeError, KeyError):
                        continue
                conn.execute(
                    f"INSERT INTO {table}({', '.join(d)}) "
                    f"VALUES ({', '.join('?' * len(d))})",
                    tuple(d.values()),
                )
    return {t: len(tables.get(t) or []) for t in TABLES_SAUVEGARDE}


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
    expire = heure() + timedelta(days=DUREE_SESSION_JOURS)
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions(token, coloc_id, cree_le, expire_le) VALUES (?, ?, ?, ?)",
            (token, coloc_id, maintenant(), expire.isoformat(sep=" ")),
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
            expire = heure() + timedelta(days=DUREE_SESSION_JOURS)
            conn.execute(
                "UPDATE sessions SET expire_le = ? WHERE token = ?",
                (expire.isoformat(sep=" "), token),
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
