"""Schermata "Fatto!" sulla TV, dopo che Cucù si è collegato a una rete dall'app.

Lo sfondo (graphics/done.png: gufetto e cartoncino bianco) esce da render_loops.mjs;
quello che cambia da dispositivo a dispositivo (nome della rete, indirizzo, QR) si
disegna qui, sul Pi, sopra lo sfondo: read_nfc.py mostra la PNG intera. Ferma, senza
animazione: il filtro logo di VLC per posare testo e QR su un loop animato sul
dispositivo non funziona (provato il 14/10). Testo e misure come nel concept
(graphics/src/concept/concept-schermate.html, #done), con i font della web UI.

Pillow e qrcode si importano solo qui dentro: senza, l'API parte lo stesso e la
schermata semplicemente non c'è.
"""
from pathlib import Path

W, H = 1920, 1080
# Cartoncino del QR: deve combaciare con la scena "fatto" in graphics/src/tv-loops.html
# (336×336 centrato in 1348,548, cioè 28 px di margine attorno a un QR di 280)
DONE_CARD = (1348, 548, 336)
QR_SIZE = 280

INK = (61, 61, 58, 255)
MUTED = (110, 110, 105, 255)
INFO_BG = (216, 230, 243, 255)
INFO_INK = (61, 102, 144, 255)

TEXT_X, TEXT_W = 190, 600   # blocco di testo a sinistra, centrato in verticale

FONTS = Path(__file__).resolve().parent / "static" / "fonts"


def _font(name, size, weight):
    from PIL import ImageFont
    f = ImageFont.truetype(str(FONTS / name), size)
    axes = f.get_variation_axes()
    # Fraunces: dimensione ottica e peso; Figtree: solo il peso
    values = [min(max(size, a["minimum"]), a["maximum"]) if a.get("name") == b"Optical Size"
              else min(max(weight, a["minimum"]), a["maximum"]) for a in axes]
    f.set_variation_by_axes(values)
    return f


def _fit(text, name, size, weight, width):
    """Font che fa stare il testo nella larghezza (nomi di rete lunghi): si rimpicciolisce."""
    while size > 18:
        f = _font(name, size, weight)
        if f.getlength(text) <= width:
            return f
        size -= 2
    return _font(name, size, weight)


def _qr(url):
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.get_matrix()


def render(ssid: str, host: str, base_png: Path, out_path: Path):
    """Disegna testo e QR sopra base_png e scrive la schermata intera in out_path."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    h1 = _font("fraunces-normal-latin.woff2", 62, 500)
    body = _font("figtree-normal-latin.woff2", 32, 400)
    bold = _font("figtree-normal-latin.woff2", 32, 600)
    small = _font("figtree-normal-latin.woff2", 24, 400)
    small_b = _font("figtree-normal-latin.woff2", 24, 600)
    pill = _font("figtree-normal-latin.woff2", 28, 400)

    indent = 44  # numeri dell'elenco a sinistra, testo rientrato
    text_w = TEXT_W - indent
    ssid_f = _fit(ssid, "figtree-normal-latin.woff2", 32, 600, text_w)
    host_f = _fit(host, "figtree-normal-latin.woff2", 24, 600, text_w - small.getlength("oppure vai su "))

    # righe: (pezzi [(testo, font, colore)], altezza della riga, linea di base dalla cima, numero)
    # come nel concept: titolo 62 px, elenco 32 px con interlinea 1,3, note 24 px
    lines = [
        ([("Fatto!", h1, INK)], 88, 56, None),
        ([("Collega il telefono al Wi-Fi", body, INK)], 42, 31, "1."),
        ([(ssid, ssid_f, INK)], 42, 31, None),
        ([("ora anche Cucù è lì", small, MUTED)], 31 + 18, 23, None),
        ([("Inquadra il codice", body, INK)], 42, 31, "2."),
        ([("oppure vai su ", small, MUTED), (host, host_f, MUTED)], 31, 23, None),
    ]
    pill_text = "Poi aggiungi Cucù alla schermata Home"
    pill_h = 28 + 20
    total = sum(lh for _, lh, _, _ in lines) + 22 + pill_h
    y = (H - total) // 2

    for parts, lh, base, num in lines:
        x = TEXT_X if num is None and parts[0][1] is h1 else TEXT_X + indent
        if num:
            d.text((TEXT_X + 4, y + base), num, font=body, fill=INK, anchor="ls")
        for text, font, color in parts:
            d.text((x, y + base), text, font=font, fill=color, anchor="ls")
            x += font.getlength(text)
        y += lh

    y += 22
    pw = pill.getlength(pill_text) + 36
    d.rounded_rectangle((TEXT_X, y, TEXT_X + pw, y + pill_h), radius=14, fill=INFO_BG)
    d.text((TEXT_X + 18, y + pill_h / 2), pill_text, font=pill, fill=INFO_INK, anchor="lm")

    # QR al centro del cartoncino
    matrix = _qr(f"http://{host}/")
    n = len(matrix)
    cell = QR_SIZE // n
    side = cell * n
    cx, cy, _ = DONE_CARD
    x0, y0 = cx - side // 2, cy - side // 2
    for r, row in enumerate(matrix):
        for c, on in enumerate(row):
            if on:
                d.rectangle((x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell - 1, y0 + (r + 1) * cell - 1), fill=INK)

    with Image.open(base_png) as base:
        full = Image.alpha_composite(base.convert("RGBA"), img).convert("RGB")
    tmp = out_path.with_name(out_path.name + ".tmp.png")
    full.save(tmp)
    tmp.replace(out_path)
