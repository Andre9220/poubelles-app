"""Génère les icônes de l'application (poubelle blanche sur fond vert).

Dessiné en primitives Pillow plutôt qu'à partir d'un emoji : aucune police
n'est garantie présente dans l'image Docker.
"""

import os

from PIL import Image, ImageDraw

VERT = (46, 125, 50)
BLANC = (255, 255, 255)
DESTINATION = os.path.join(os.path.dirname(__file__), "static")


def dessiner(taille):
    # On dessine 4x plus grand puis on réduit : bords lissés sans antialiasing.
    facteur = 4
    c = taille * facteur
    img = Image.new("RGBA", (c, c), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, c - 1, c - 1], radius=int(c * 0.22), fill=VERT)

    # Proportions de la poubelle, en fraction du côté.
    largeur = c * 0.46
    gauche = (c - largeur) / 2
    droite = gauche + largeur
    haut = c * 0.34
    bas = c * 0.78
    epaisseur = max(1, int(c * 0.012))

    # Couvercle et poignée
    d.rounded_rectangle(
        [gauche - c * 0.05, haut - c * 0.07, droite + c * 0.05, haut - c * 0.02],
        radius=int(c * 0.025),
        fill=BLANC,
    )
    d.rounded_rectangle(
        [c / 2 - c * 0.07, haut - c * 0.13, c / 2 + c * 0.07, haut - c * 0.08],
        radius=int(c * 0.02),
        fill=BLANC,
    )

    # Corps légèrement trapézoïdal
    retrait = c * 0.035
    d.polygon(
        [
            (gauche, haut),
            (droite, haut),
            (droite - retrait, bas),
            (gauche + retrait, bas),
        ],
        fill=BLANC,
    )

    # Rainures verticales, en vert pour creuser le corps
    for fraction in (0.3, 0.5, 0.7):
        x = gauche + largeur * fraction
        d.line(
            [(x, haut + c * 0.06), (x, bas - c * 0.05)],
            fill=VERT,
            width=epaisseur * 2,
        )

    return img.resize((taille, taille), Image.LANCZOS)


def main():
    os.makedirs(DESTINATION, exist_ok=True)
    for taille, nom in [
        (192, "icon-192.png"),
        (512, "icon-512.png"),
        (180, "apple-touch-icon.png"),
        (32, "favicon-32.png"),
    ]:
        chemin = os.path.join(DESTINATION, nom)
        dessiner(taille).save(chemin, "PNG", optimize=True)
        print(f"{nom}: {os.path.getsize(chemin)} octets")


if __name__ == "__main__":
    main()
