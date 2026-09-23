# 🗑️ Mission Poubelles

Application Streamlit pour organiser la sortie des poubelles d'une colocation :
signalement quand c'est plein, roulement équitable, anti-triche, points,
notifications WhatsApp et espace admin.

## Hébergement : Streamlit Community Cloud + Turso

Streamlit Cloud n'a **pas de disque persistant** : un fichier SQLite y serait
effacé à chaque redémarrage. La base vit donc chez **Turso** (SQLite hébergé,
gratuit, sans carte bancaire). Les photos sont stockées dans la base.

### 1. Créer la base Turso

1. Crée un compte sur <https://turso.tech> (connexion GitHub possible).
2. **Create Database** → nom `poubelles`, région la plus proche de
   Streamlit Cloud : **AWS US East (Virginia)**.
3. Dans la base : **Connect** → note l'**URL** (`libsql://…turso.io`)
   puis **Generate Token** et note le **jeton**.

### 2. Publier le code sur GitHub

Crée un dépôt **privé** (ex. `poubelles-app`) et pousse-y ce dossier.
Le `.gitignore` exclut déjà `data/` et les secrets.

### 3. Déployer sur Streamlit Cloud

1. <https://share.streamlit.io> → **Create app** → choisis le dépôt.
2. Main file : `app.py`.
3. **Advanced settings** → Python **3.12** → dans **Secrets**, colle le
   contenu de `.streamlit/secrets.toml.example` avec tes vraies valeurs.
4. **Deploy**.

### 4. Récupérer les données existantes

1. Sur l'app neuve, onglet **Créer mon compte** : pseudo `andre`, un mot de
   passe **provisoire**. Premier compte → admin automatiquement.
2. **Admin → Zone rouge → Restaurer une sauvegarde** → choisis le fichier
   `sauvegarde-poubelles-….json` → tape `RESTAURER`.
3. Reconnecte-toi avec ton **ancien** mot de passe : le provisoire a disparu.

## Lancer en local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Sans secrets Turso, l'app utilise `data/poubelles.db`.

## Tests

```bash
pip install pytest
pytest -q tests/
```

## Limites connues sur Streamlit Cloud

- L'app **s'endort après 12 h sans visite** ; le premier visiteur clique
  « Yes, get this app back up! » et patiente ~30 s.
- Pas d'installation en app (PWA) : Streamlit Cloud contrôle la page. On peut
  toujours ajouter un raccourci sur l'écran d'accueil.

## Hébergement alternatif (Docker)

`Dockerfile` et `docker-compose.yml` servent toujours pour un serveur
personnel (ex. Umbrel), avec PWA installable.
