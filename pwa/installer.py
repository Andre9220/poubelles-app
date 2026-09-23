"""Rend l'application installable sur l'écran d'accueil (PWA).

Streamlit ne permet pas d'écrire dans le <head> de sa page depuis le script.
On copie donc les fichiers PWA dans son dossier `static/` (servi à la racine)
et on insère les balises nécessaires dans son `index.html`.

Exécuté une fois à la construction de l'image, jamais à l'exécution.
"""

import os
import shutil
import sys

import streamlit

MARQUEUR = "<!-- mission-poubelles-pwa -->"

BALISES = """
    <!-- mission-poubelles-pwa -->
    <link rel="manifest" href="./manifest.json" />
    <meta name="theme-color" content="#2e7d32" />
    <meta name="mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <meta name="apple-mobile-web-app-title" content="Poubelles" />
    <link rel="apple-touch-icon" href="./apple-touch-icon.png" />
    <link rel="icon" type="image/png" sizes="32x32" href="./favicon-32.png" />
    <script>
      if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
          navigator.serviceWorker.register('./sw.js').catch(function () {});
        });
      }
    </script>
"""


def main():
    racine = os.path.join(os.path.dirname(streamlit.__file__), "static")
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    if not os.path.isdir(racine):
        sys.exit(f"Dossier statique de Streamlit introuvable : {racine}")

    for nom in sorted(os.listdir(source)):
        shutil.copy2(os.path.join(source, nom), os.path.join(racine, nom))
        print(f"copié : {nom}")

    index = os.path.join(racine, "index.html")
    with open(index, encoding="utf-8") as f:
        html = f.read()

    if MARQUEUR in html:
        print("index.html déjà modifié, rien à faire")
        return

    if "</head>" not in html:
        sys.exit("Balise </head> absente : la structure de Streamlit a changé.")

    html = html.replace("</head>", BALISES + "  </head>", 1)
    with open(index, "w", encoding="utf-8") as f:
        f.write(html)
    print("index.html : balises PWA insérées")


if __name__ == "__main__":
    main()
