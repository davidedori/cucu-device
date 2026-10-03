#!/usr/bin/env python3
"""
Rigenera le grafiche mostrate sulla TV da graphics/src/tv-screens.html
(stesso metodo dell'immagine di anteprima del sito: Chrome headless).

    python3 graphics/src/render.py

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
PLYMOUTH = ROOT / "plymouth"
HTML = SRC / "tv-screens.html"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# schermata (id nell'HTML) → file usato da read_nfc.py
SCREENS = {
    "idle": GRAPHICS / "idle.png",            # in attesa di una statuina
    "end": GRAPHICS / "end.png",              # episodio finito: togli la statuina
    "next": GRAPHICS / "wait_next.png",       # statuina tolta: se ne sceglie un'altra
    "rest": GRAPHICS / "rest.png",            # bloccato dai limiti di tempo
    "listen": GRAPHICS / "listen.png",        # audio in corso (personaggi audio)
    "splash": GRAPHICS / "splash.png",        # avvio (fallback di Plymouth)
}

ORANGE = (0xE9, 0x4E, 0x1B)


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


def save_png(img: Image.Image, dest: Path, mode="RGB", palette=False):
    img = img.convert(mode)
    if palette:
        # 256 colori con dithering: metà del peso. Solo per i fotogrammi di
        # avvio (fondo, forme e gufetto), che finiscono nell'initramfs; sulle
        # illustrazioni lascerebbe puntini visibili
        img = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
    img.save(dest, optimize=True)
    print(f"  {dest.relative_to(ROOT)}  ({dest.stat().st_size // 1024} KB)")


def extract_owl(frame: Path) -> Image.Image:
    """Dal vecchio fotogramma di avvio (gufetto arancione su fondo pesca uniforme)
    tiene solo il gufetto, ricolorato con l'arancione del logo e con l'alpha
    ricavato dal canale verde (antialiasing incluso). Gli occhi restano vuoti:
    nel logo sono trasparenti."""
    im = Image.open(frame).convert("RGB")
    bg = im.getpixel((im.width // 2, 40))
    px = im.load()
    # l'arancione del vecchio file: il pixel meno verde
    low = min(im.get_flattened_data() if hasattr(im, 'get_flattened_data') else im.getdata(), key=lambda p: p[1])
    span = max(1, bg[1] - low[1])
    out = Image.new("RGBA", im.size, (*ORANGE, 0))
    opx = out.load()
    for y in range(im.height):
        for x in range(im.width):
            g = px[x, y][1]
            if g < bg[1] - 6:
                a = max(0, min(255, round((bg[1] - g) / span * 255)))
                opx[x, y] = (*ORANGE, a)
    return out


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

        print("Avvio (Plymouth):")
        bg_raw = tmp / "bootbg.png"
        shot("bootbg", (1920, 1080), bg_raw)
        background = Image.open(bg_raw).convert("RGBA")
        for i in range(1, 6):
            owl_src = SRC / f"boot-owl-{i:02d}.png"
            if not owl_src.exists():
                # prima esecuzione: il gufetto si prende dai fotogrammi originali
                extract_owl(PLYMOUTH / f"boot_{i:02d}.png").save(owl_src, optimize=True)
                print(f"  estratto {owl_src.relative_to(ROOT)}")
            frame = background.copy()
            frame.alpha_composite(Image.open(owl_src).convert("RGBA"))
            save_png(frame, PLYMOUTH / f"boot_{i:02d}.png", palette=True)


if __name__ == "__main__":
    main()
