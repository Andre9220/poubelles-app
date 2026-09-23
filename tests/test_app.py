"""Tests de bout en bout de Mission Poubelles.

Lancement : `pytest -q tests/`   (mode SQLite local)
Avec Turso : `TURSO_DATABASE_URL=http://127.0.0.1:8080 pytest -q tests/`
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

import auth
import database as bd
import notifications as notif
import scheduler as sched
from conftest import MDP, RACINE, coloc

APP = os.path.join(RACINE, "app.py")


def rendre(pseudo=None, **etat):
    at = AppTest.from_file(APP, default_timeout=120)
    if pseudo:
        at.session_state["token"] = bd.creer_session(coloc(pseudo)["id"])
    for k, v in etat.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# --------------------------------------------------------------- heure

def test_heure_de_montreal_sur_un_serveur_utc():
    attendu = datetime.now(ZoneInfo("America/Toronto")).replace(tzinfo=None)
    assert abs((bd.heure() - attendu).total_seconds()) < 5
    # Le serveur, lui, est bien en UTC : l'écart prouve qu'on ne dépend pas de TZ.
    assert abs((datetime.now() - attendu).total_seconds()) > 3600


# --------------------------------------------------------------- connexion

def test_connexion_et_anti_force_brute():
    for _ in range(bd.TENTATIVES_MAX):
        with pytest.raises(bd.ErreurBase, match="incorrect"):
            _connecter("andre", "faux")
    # 6e tentative : bloquée, même avec le BON mot de passe
    with pytest.raises(bd.ErreurBase, match="Trop de tentatives"):
        _connecter("andre", MDP)


def test_blocage_ne_revele_pas_les_pseudos_inexistants():
    for _ in range(bd.TENTATIVES_MAX):
        with pytest.raises(bd.ErreurBase, match="incorrect"):
            _connecter("fantome", "x")
    with pytest.raises(bd.ErreurBase, match="Trop de tentatives"):
        _connecter("fantome", "x")


def test_succes_efface_les_echecs_et_blocage_expire():
    for _ in range(bd.TENTATIVES_MAX - 1):
        with pytest.raises(bd.ErreurBase):
            _connecter("andre", "faux")
    _connecter("andre", MDP)                      # réussite : compteur remis à zéro
    for _ in range(bd.TENTATIVES_MAX - 1):
        with pytest.raises(bd.ErreurBase, match="incorrect"):
            _connecter("andre", "faux")
    # Des échecs plus vieux que la fenêtre ne comptent plus
    vieux = (bd.heure() - timedelta(minutes=bd.FENETRE_TENTATIVES_MIN + 1)).isoformat(sep=" ")
    with bd.db() as conn:
        conn.execute("UPDATE tentatives SET ts = ?", (vieux,))
    assert bd.minutes_blocage("andre") == 0


def _connecter(pseudo, mdp):
    return auth.connecter(pseudo, mdp)


def test_code_invitation_modifiable():
    with pytest.raises(bd.ErreurBase, match="Code d'invitation"):
        auth.inscrire("Luc", "luc", MDP, MDP, "mauvais")
    bd.set_reglage("code_invitation", "nouveau-code")
    with pytest.raises(bd.ErreurBase, match="Code d'invitation"):
        auth.inscrire("Luc", "luc", MDP, MDP, auth.CODE_INVITATION)
    assert auth.inscrire("Luc", "luc", MDP, MDP, "nouveau-code")


# --------------------------------------------------------------- roulement

def test_roulement_equitable_sans_doublon():
    vus, precedent = [], None
    for _ in range(12):
        p = sched.prochain_coloc()
        assert p["id"] != precedent
        bd.ajouter_sortie(p["id"], None, bd.ouvrir_demande(p["id"]))
        vus.append(p["nom"]); precedent = p["id"]
    assert all(vus.count(n) == 3 for n in set(vus))


def test_absent_jamais_designe_ni_notifie(base_propre):
    absent = sched.prochain_coloc()
    bd.maj_coloc(absent["id"], absent=1)
    for _ in range(6):
        p = sched.prochain_coloc()
        assert p["id"] != absent["id"]
        bd.ajouter_sortie(p["id"], None, bd.ouvrir_demande(p["id"]))
    base_propre.clear()
    notif.diffuser("test")
    assert absent["telephone"] not in [t for t, _ in base_propre]


def test_cycle_signalement_validation_ponctualite():
    designe = sched.prochain_coloc()
    d = bd.ouvrir_demande(coloc("arno")["id"])
    with pytest.raises(bd.ErreurBase, match="déjà signalées"):
        bd.ouvrir_demande(coloc("mathis")["id"])
    assert sched.peut_valider(designe)
    assert not sched.peut_valider(coloc("arno")) or designe["pseudo"] == "arno"
    bd.ajouter_sortie(designe["id"], None, bd.demande_ouverte())
    assert bd.demande_ouverte() is None
    h = bd.get_historique(1)[0]
    assert h["demande_id"] == d["id"] and h["delai_heures"] is not None
    assert bd.stats_globales()["ponctualite"] == 100


# --------------------------------------------------------------- admin

def test_annulation_retire_points_et_rouvre():
    p = sched.prochain_coloc()
    bd.ajouter_sortie(p["id"], None, bd.ouvrir_demande(p["id"]))
    bd.annuler_sortie(bd.get_historique(1)[0]["id"])
    assert bd.get_coloc(p["id"])["points"] == 0
    assert bd.demande_ouverte() is not None
    assert sched.prochain_coloc()["id"] == p["id"]


def test_saisie_admin_tracee():
    admin, autre = coloc("andre"), coloc("mathis")
    bd.ajouter_sortie(autre["id"], None, None, enregistre_par=admin["id"])
    h = bd.get_historique(1)[0]
    assert h["saisi_par"] == "André" and h["coloc_id"] == autre["id"]


def test_ajustement_points_et_journal():
    admin, cible = coloc("andre"), coloc("yass")
    assert bd.ajuster_points(cible["id"], 25, "a sorti le recyclage", admin["id"]) == 25
    assert bd.ajuster_points(cible["id"], -100, "retard", admin["id"]) == 0   # jamais < 0
    with pytest.raises(bd.ErreurBase, match="motif"):
        bd.ajuster_points(cible["id"], 5, "  ", admin["id"])
    with pytest.raises(bd.ErreurBase, match="différent de zéro"):
        bd.ajuster_points(cible["id"], 0, "rien", admin["id"])
    journal = bd.get_audit()
    assert journal[0]["action"] == "Points ajustés" and "-100" in journal[0]["detail"]
    assert journal[0]["nom_admin"] == "André"


# --------------------------------------------------------------- photos

def test_photos_en_base():
    ref = bd.stocker_photo(b"\xff\xd8image")
    assert ref.startswith("db:") and bd.lire_photo(ref) == b"\xff\xd8image"
    bd.supprimer_photo(ref)
    assert bd.lire_photo(ref) is None
    assert bd.lire_photo(None) is None


def test_migration_photos_fichiers_vers_base():
    dossier = os.path.join(bd.UPLOADS, "profils")
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, "ancienne.jpg"), "wb") as f:
        f.write(b"ANCIENNE")
    bd.maj_coloc(coloc("andre")["id"], photo="profils/ancienne.jpg")
    bd.init_db()
    ref = coloc("andre")["photo"]
    assert ref.startswith("db:") and bd.lire_photo(ref) == b"ANCIENNE"


# --------------------------------------------------------------- sauvegarde

def test_sauvegarde_restauration_aller_retour():
    admin = coloc("andre")
    p = sched.prochain_coloc()
    ref = bd.stocker_photo(b"PREUVE")
    bd.ajouter_sortie(p["id"], ref, bd.ouvrir_demande(p["id"]))
    bd.ajuster_points(admin["id"], 7, "test", admin["id"])
    bd.set_reglage("code_invitation", "secret-coloc")
    bd.creer_session(admin["id"])

    texte = bd.exporter()
    avant = json.loads(texte)["tables"]

    with bd.db() as conn:                         # catastrophe : tout est perdu
        for t in bd.TABLES_SAUVEGARDE:
            conn.execute(f"DELETE FROM {t}")
    bilan = bd.restaurer(texte)

    assert bilan["colocataires"] == 4
    apres = json.loads(bd.exporter())["tables"]
    for t in bd.TABLES_SAUVEGARDE:
        assert apres[t] == avant[t], t
    assert bd.lire_photo(ref) == b"PREUVE"
    assert auth.verifier(MDP, coloc("andre")["password_hash"])
    assert bd.sessions_actives() == {}             # tout le monde doit se reconnecter


@pytest.mark.parametrize("contenu, message", [
    ("pas du json", "illisible"),
    (json.dumps({"format": "autre"}), "pas une sauvegarde"),
    (json.dumps({"format": "mission-poubelles", "tables": {}}), "sans colocataires"),
])
def test_restauration_refuse_les_fichiers_invalides(contenu, message):
    with pytest.raises(bd.ErreurBase, match=message):
        bd.restaurer(contenu)
    assert len(bd.get_colocs()) == 4               # rien n'a été touché


def test_restauration_ignore_les_colonnes_inconnues():
    donnees = json.loads(bd.exporter())
    donnees["tables"]["colocataires"][0]["colonne_piege); DROP TABLE audit; --"] = "x"
    bd.restaurer(json.dumps(donnees))
    assert len(bd.get_colocs()) == 4
    bd.journaliser_action(None, "la table audit existe toujours")


# --------------------------------------------------------------- interface

def test_ecran_connexion():
    at = rendre()
    assert [t.label for t in at.tabs] == ["Connexion", "Créer mon compte"]


def test_page_admin_complete():
    at = rendre("andre")
    assert len(at.tabs) == 5
    boutons = [b.label for b in at.button]
    for attendu in ["Envoyer à toute la coloc", "Appliquer", "Changer le code",
                    "Préparer la sauvegarde", "Restaurer", "Enregistrer la sortie"]:
        assert attendu in boutons, attendu


def test_non_admin_ne_voit_aucune_action_admin():
    at = rendre("mathis")
    assert len(at.tabs) == 4
    boutons = [b.label for b in at.button]
    for interdit in ["Appliquer", "Restaurer", "Préparer la sauvegarde", "Changer le code"]:
        assert interdit not in boutons


def test_ajustement_points_via_interface():
    at = rendre("andre")
    motif = [t for t in at.text_input if "Motif" in t.label][0]
    motif.set_value("coup de main")
    [n for n in at.number_input if "Points à ajouter" in n.label][0].set_value(15)
    [b for b in at.button if b.label == "Appliquer"][0].click().run()
    assert not at.exception
    assert bd.get_audit()[0]["action"] == "Points ajustés"


def test_rendus_repetes_sans_plantage():
    for _ in range(5):
        rendre("andre")


# --------------------------------------------------------------- liaison Turso

class _FauxCurseur:
    description = (("n", None, None, None, None, None, None),)

    def fetchall(self):
        return [(1,)]


class _FausseLiaison:
    """Imite libsql : la première requête échoue si la liaison est périmée."""

    def __init__(self, perimee):
        self.perimee = perimee
        self.requetes = []

    def execute(self, sql, params):
        self.requetes.append(sql)
        if self.perimee:
            self.perimee = False
            raise ValueError("Hrana: `api error: status=400 Bad Request, body=Received an invalid baton`")
        return _FauxCurseur()


def test_erreur_de_liaison_reconnue_par_son_message():
    assert bd._erreur_liaison(ValueError("Hrana: `http error: error trying to connect`"))
    assert bd._erreur_liaison(ValueError("Received an invalid baton"))
    assert not bd._erreur_liaison(ValueError("invalid literal for int() with base 10"))
    assert not bd._erreur_liaison(KeyError("hrana"))


def test_liaison_perimee_rouverte_et_requete_rejouee():
    liaisons = [_FausseLiaison(perimee=True), _FausseLiaison(perimee=False)]
    conn = bd._ConnexionTurso(lambda: liaisons.pop(0), replique=False)
    assert conn.execute("SELECT 1").fetchall()[0]["n"] == 1   # rejouée, invisible


def test_pas_de_rejeu_au_milieu_d_une_transaction():
    liaison = _FausseLiaison(perimee=False)
    conn = bd._ConnexionTurso(lambda: liaison, replique=False)
    conn.execute("UPDATE reglages SET valeur = 'x'")           # écriture en cours
    liaison.perimee = True
    # Rejouer seule la 2e requête sur une liaison neuve perdrait la 1re : on refuse.
    with pytest.raises(ValueError, match="baton"):
        conn.execute("UPDATE reglages SET valeur = 'y'")


def test_streamlit_cloud_sans_turso_refuse_de_demarrer(monkeypatch):
    # Sans ce garde-fou, l'app tournerait sur une base locale vide, effacée à
    # chaque redémarrage : les comptes créés disparaîtraient en silence.
    monkeypatch.setattr(bd, "SUR_STREAMLIT_CLOUD", True)
    monkeypatch.setattr(bd, "DISTANT", False)
    monkeypatch.setattr(bd, "_connexion", None)
    with pytest.raises(bd.ErreurBase, match="TURSO_DATABASE_URL"):
        bd.get_colocs()
