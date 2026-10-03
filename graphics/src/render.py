#!/usr/bin/env python3
"""
Rigenera le grafiche mostrate sulla TV da graphics/src/tv-screens.html
(stesso metodo dell'immagine di anteprima del sito: Chrome headless).

    python3 graphics/src/render.py

L'avvio (Plymouth) e graphics/splash.png si fanno con render_loops.mjs, dal gufetto animato.

Serve Google Chrome (macOS) e Pillow. Si lancia sul computer, non sul Cucù:
le PNG generate vanno nel repository e arrivano ai dispositivi con l'OTA.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent.parent
GRAPHICS = ROOT / "graphics"
HTML = SRC / "tv-screens.html"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# schermata (id nell'HTML) → file usato da read_nfc.py
SCREENS = {
    "idle": GRAPHICS / "idle.png",            # in attesa di una statuina
    "end": GRAPHICS / "end.png",              # episodio finito: togli la statuina
    "next": GRAPHICS / "wait_next.png",       # statuina tolta: se ne sceglie un'altra
    "rest": GRAPHICS / "rest.png",            # bloccato dai limiti di tempo
    "listen": GRAPHICS / "listen.png",        # audio in corso (personaggi audio)
}

def shot(fragment: str, size, out: Path, transparent=False):
    args = [
        CHROME, "--headless", "--hide-scrollbars", "--force-device-scale-factor=1",
        f"--window-size={size[0]},{size[1]}", "--virtual-time-budget=4000",
        f"--screenshot={out}",
    ]
    if transparent:
        args.append("--default-background-color=00000000")
    args.append(f"{HTML.as_uri()}#{fragment}")
    subprocess.run(args, check=True, capture_output=True, timeout=120)
    if not out.exists():
        sys.exit(f"Chrome non ha prodotto {out}")


def save_png(img: Image.Image, dest: Path, mode="RGB"):
    img = img.convert(mode)
    img.save(dest, optimize=True)
    print(f"  {dest.relative_to(ROOT)}  ({dest.stat().st_size // 1024} KB)")



def main():
    if not Path(CHROME).exists():
        sys.exit("Google Chrome non trovato: serve per generare le immagini.")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        print("Schermate TV:")
        for fragment, dest in SCREENS.items():
            raw = tmp / f"{fragment}.png"
            shot(fragment, (1920, 1080), raw)
            save_png(Image.open(raw), dest)

        print("Clessidra:")
        (GRAPHICS / "hourglass").mkdir(exist_ok=True)
        for level in range(10):
            raw = tmp / f"hg-{level}.png"
            shot(f"hg-{level}", (140, 140), raw, transparent=True)
            save_png(Image.open(raw), GRAPHICS / "hourglass" / f"hourglass_{level}.png", "RGBA")


if __name__ == "__main__":
    main()
