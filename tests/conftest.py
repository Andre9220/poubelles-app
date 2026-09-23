"""Configuration commune : base de test isolée, remise à zéro entre chaque test.

Le mode est choisi par variables d'environnement, avant l'import de `database` :
- par défaut, un fichier SQLite temporaire ;
- avec TURSO_DATABASE_URL, le mode réplique Turso (ex. `turso dev` en local).
"""

import os
import sys
import tempfile

import pytest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# Le serveur de test tourne en UTC, comme Streamlit Cloud : c'est précisément
# la situation où l'heure de Montréal doit rester juste.
os.environ["TZ"] = "UTC"
if hasattr(__import__("time"), "tzset"):
    __import__("time").tzset()

_DOSSIER = tempfile.mkdtemp(prefix="poubelles-tests-")
os.environ.setdefault("POUBELLES_DB", os.path.join(_DOSSIER, "test.db"))
os.environ.setdefault("POUBELLES_UPLOADS", os.path.join(_DOSSIER, "uploads"))

import database as bd  # noqa: E402
import auth  # noqa: E402
import notifications as notif  # noqa: E402

bd.init_db()

TABLES = [
    "sessions", "tentatives", "audit", "photos", "notifications",
    "demandes", "historique", "reglages", "colocataires",
]
MDP = "motdepasse123"


@pytest.fixture(autouse=True)
def base_propre(monkeypatch):
    """Chaque test repart de 4 colocs avec compte ; André est admin."""
    # Aucun WhatsApp réel ne doit partir pendant les tests.
    envois = []
    monkeypatch.setattr(notif, "_appeler_api",
                        lambda tel, cle, msg: envois.append((tel, msg)) or "ok")
    with bd.db() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
    bd.init_db()
    h = auth.hacher(MDP)
    for i, (nom, pseudo) in enumerate(
        [("André", "andre"), ("Mathis", "mathis"), ("Yassine", "yass"), ("Arnaud", "arno")]
    ):
        cid = bd.creer_coloc(nom, pseudo, h, is_admin=int(pseudo == "andre"))
        bd.maj_coloc(cid, telephone=f"+3360000000{i}", whatsapp_apikey=f"12345{i}")
    yield envois


def coloc(pseudo):
    return bd.get_coloc_par_pseudo(pseudo)
