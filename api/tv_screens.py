"""Schermate della TV con testo e QR, disegnate sul Pi perché cambiano da dispositivo
a dispositivo (nome della rete, password dell'hotspot, indirizzo).

- "Colleghiamoci!" (first): Cucù non ha ancora una rete; QR del Wi-Fi dell'hotspot
- "Fatto!" (done): Cucù si è appena collegato a una rete dall'app; QR dell'indirizzo
- "Eccomi!" (help): gesto d'emergenza (stessa statuina 5 volte); come raggiungere Cucù
  adesso: sull'hotspot il QR del suo Wi-Fi, su una rete il QR dell'indirizzo

Il loop animato (graphics/loops/<nome>.mp4: gufetto e cartoncino bianco) esce da
render_loops.mjs; testo e QR si disegnano qui in una PNG trasparente a tutto schermo
(<nome>_overlay.png) che read_nfc.py posa sul loop con il filtro logo di VLC. In più la
schermata intera, ferma (<nome>_screen.png, sopra graphics/<nome>.png), per quando il
loop manca. Testo e misure come nei concept (graphics/src/concept/concept-schermate.html,
#first, #done, #help-home, #help-hotspot), con i font della web UI.

Pillow e qrcode si importano solo qui dentro: senza, l'API parte lo stesso e queste
schermate semplicemente non ci sono.
"""
from pathlib import Path

W, H = 1920, 1080
# Cartoncino del QR: deve combaciare con le scene in graphics/src/tv-loops.html
# (336×336 centrato in 1348,548, cioè 28 px di margine attorno a un QR di 280)
CARD = (1348, 548, 336)
QR_SIZE = 280

INK = (61, 61, 58, 255)
MUTED = (110, 110, 105, 255)
INFO = ((216, 230, 243, 255), (61, 102, 144, 255))   # riquadro azzurro: un consiglio
PIN = ((249, 216, 202, 255), (176, 71, 42, 255))     # riquadro pesca: il nuovo PIN

TEXT_X, TEXT_W = 190, 600   # blocco di testo a sinistra, centrato in verticale
INDENT = 44                 # numeri dell'elenco a sinistra, testo rientrato

FONTS = Path(__file__).resolve().parent / "static" / "fonts"
SERIF = "fraunces-normal-latin.woff2"
SANS = "figtree-normal-latin.woff2"


def _font(name, size, weight):
    from PIL import ImageFont
    f = ImageFont.truetype(str(FONTS / name), size)
    axes = f.get_variation_axes()
    # Fraunces: dimensione ottica e peso; Figtree: solo il peso
    values = [min(max(size, a["minimum"]), a["maximum"]) if a.get("name") == b"Optical Size"
              else min(max(weight, a["minimum"]), a["maximum"]) for a in axes]
    f.set_variation_by_axes(values)
    return f


def _line_fonts(parts, size, width):
    """Font (normale, grassetto) per una riga di pezzi [(testo, grassetto)]: se non sta
    nella larghezza (nomi di rete lunghi) si rimpicciolisce tutta la riga."""
    while True:
        reg, bold = _font(SANS, size, 400), _font(SANS, size, 600)
        if size <= 16 or sum((bold if b else reg).getlength(t) for t, b in parts) <= width:
            return reg, bold
        size -= 2


def qr_matrix(data: str):
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def wifi_qr_data(ssid: str, password: str) -> str:
    """Contenuto del QR che collega il telefono a una rete (formato letto da iOS e Android)."""
    esc = lambda s: "".join("\\" + c if c in '\\;,:"' else c for c in s)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def render(title, items, qr_data, overlay_path: Path, base_png: Path, full_path: Path, pill=None, pill_colors=INFO):
    """Disegna la schermata. items: punti numerati, ciascuno {"lines": [...], "notes": [...]},
    dove ogni riga è una lista di pezzi (testo, grassetto): "lines" a 32 px, "notes" a 24 px
    in grigio. pill: un riquadro in fondo (facoltativo), pezzi (testo, grassetto) o testo,
    con i colori pill_colors (fondo, testo).
    Scrive overlay_path (RGBA trasparente, da posare sul loop) e full_path (sopra base_png)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    h1 = _font(SERIF, 62, 500)
    num_f = _font(SANS, 32, 400)
    pill_f, pill_b = _font(SANS, 28, 400), _font(SANS, 28, 600)
    if isinstance(pill, str):
        pill = [(pill, False)]
    width = TEXT_W - INDENT

    # righe da disegnare: (x, pezzi [(testo, font, colore)], altezza, linea di base, numero)
    # come nel concept: titolo 62 px, elenco 32 px con interlinea 1,3, note 24 px, 18 px fra i punti
    rows = [(TEXT_X, [(title, h1, INK)], 88, 56, None)]
    for i, item in enumerate(items):
        for j, parts in enumerate(item.get("lines", [])):
            reg, bold = _line_fonts(parts, 32, width)
            rows.append((TEXT_X + INDENT, [(t, bold if b else reg, INK) for t, b in parts], 42, 31,
                         f"{i + 1}." if j == 0 else None))
        notes = item.get("notes", [])
        for parts in notes:
            reg, bold = _line_fonts(parts, 24, width)
            rows.append((TEXT_X + INDENT, [(t, bold if b else reg, MUTED) for t, b in parts], 31, 23, None))
        if i < len(items) - 1:
            x, p, lh, base, num = rows[-1]
            rows[-1] = (x, p, lh + 18, base, num)

    pill_h = 48
    total = sum(r[2] for r in rows) + (22 + pill_h if pill else 0)
    y = (H - total) // 2
    for x, parts, lh, base, num in rows:
        if num:
            d.text((TEXT_X + 4, y + base), num, font=num_f, fill=INK, anchor="ls")
        for text, font, color in parts:
            d.text((x, y + base), text, font=font, fill=color, anchor="ls")
            x += font.getlength(text)
        y += lh
    if pill:
        y += 22
        bg, ink = pill_colors
        pw = sum((pill_b if b else pill_f).getlength(t) for t, b in pill) + 36
        d.rounded_rectangle((TEXT_X, y, TEXT_X + pw, y + pill_h), radius=14, fill=bg)
        x = TEXT_X + 18
        for t, b in pill:
            font = pill_b if b else pill_f
            d.text((x, y + pill_h / 2), t, font=font, fill=ink, anchor="lm")
            x += font.getlength(t)

    # QR al centro del cartoncino, a quadretti interi
    matrix = qr_matrix(qr_data)
    n = len(matrix)
    cell = QR_SIZE // n
    side = cell * n
    cx, cy, _ = CARD
    x0, y0 = cx - side // 2, cy - side // 2
    for r, row in enumerate(matrix):
        for c, on in enumerate(row):
            if on:
                d.rectangle((x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell - 1, y0 + (r + 1) * cell - 1), fill=INK)

    tmp = overlay_path.with_name(overlay_path.name + ".tmp.png")
    img.save(tmp)
    tmp.replace(overlay_path)
    with Image.open(base_png) as base:
        full = Image.alpha_composite(base.convert("RGBA"), img).convert("RGB")
    tmp = full_path.with_name(full_path.name + ".tmp.png")
    full.save(tmp)
    tmp.replace(full_path)


def render_done(ssid: str, host: str, run_dir: Path, graphics_dir: Path):
    """"Fatto!": Cucù è sulla rete di casa, il QR apre l'app."""
    render("Fatto!", [
        {"lines": [[("Collega il telefono al Wi-Fi", False)], [(ssid, True)]],
         "notes": [[("ora anche Cucù è lì", False)]]},
        {"lines": [[("Inquadra il codice", False)]],
         "notes": [[("oppure vai su ", False), (host, True)]]},
    ], f"http://{host}/", run_dir / "done_overlay.png", graphics_dir / "done.png", run_dir / "done_screen.png",
        pill="Poi aggiungi Cucù alla schermata Home", pill_colors=INFO)


def render_first(ap_ssid: str, password: str, ap_ip: str, run_dir: Path, graphics_dir: Path):
    """"Colleghiamoci!": Cucù non ha ancora una rete, il QR collega il telefono all'hotspot."""
    render("Colleghiamoci!", [
        {"lines": [[("Inquadra il codice con il telefono", False)]],
         "notes": [[("oppure collegati alla rete ", False), (ap_ssid, True)],
                   [("password ", False), (password, True)]]},
        {"lines": [[("Si apre la pagina per il Wi-Fi di casa", False)]],
         "notes": [[("se non si apre, vai su ", False), (ap_ip, True)]]},
    ], wifi_qr_data(ap_ssid, password), run_dir / "first_overlay.png", graphics_dir / "first.png",
        run_dir / "first_screen.png")


def render_help(run_dir: Path, graphics_dir: Path, host: str, ap_ssid: str = None, password: str = None,
                ap_ip: str = None, ssid: str = None, pin_open: bool = False):
    """"Eccomi!": come raggiungere Cucù adesso. Sull'hotspot (ap_ssid) il QR del suo Wi-Fi,
    su una rete (ssid) il QR dell'indirizzo. pin_open: c'è la finestra per un nuovo PIN."""
    if ap_ssid:
        items = [
            {"lines": [[("Inquadra il codice con il telefono", False)]],
             "notes": [[("oppure collegati alla rete ", False), (ap_ssid, True)],
                       [("password ", False), (password, True)]]},
            {"lines": [[("Apri l'app Cucù", True), (" o vai su ", False), (ap_ip, True)]]},
        ]
        qr = wifi_qr_data(ap_ssid, password)
    else:
        items = [
            {"lines": [[("Collega il telefono al Wi-Fi", False)], [(ssid or "", True)]],
             "notes": [[("la stessa rete di Cucù", False)]]},
            {"lines": [[("Apri l'app Cucù", True), (" o inquadra il codice", False)]],
             "notes": [[("oppure vai su ", False), (host, True)]]},
        ]
        qr = f"http://{host}/"
    pill = [("Puoi scegliere un ", False), ("nuovo PIN", True), (" finché lasci qui la statuina", False)] if pin_open else None
    render("Eccomi!", items, qr, run_dir / "help_overlay.png", graphics_dir / "first.png",
           run_dir / "help_screen.png", pill=pill, pill_colors=PIN)
