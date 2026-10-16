"""Cartellini delle statuine: la striscia di carta che si infila nella statuina
personalizzabile (cucu-cad, build_tag_figure()), piegata a metà a cavallo dell'anima.
Un lembo per faccia, ciascuno dietro una finestra della cornice.

Le misure vengono da card_spec.json, scritto dalla macro: carta (paper_w × 2·flap_h,
piega al centro) e finestra (w × h, raggio r, a side/bottom/fold dai bordi del lembo).

Sul foglio i due lembi hanno l'alto verso la piega: la metà di sopra della striscia
è capovolta, così una volta piegata l'immagine è dritta su entrambe le facce.

Il PDF è scritto a mano (solo stdlib): pagine in punti, quindi misure esatte in mm,
JPEG incorporati così come sono (DCTDecode), Helvetica dei PDF standard (nessun font da
incorporare). Niente raster della pagina intera in RAM. Pillow serve solo per ritagliare
le immagini, e si importa solo lì: senza, l'API parte lo stesso.
"""
import io
import json
import zlib
from pathlib import Path

MM = 72 / 25.4
PAGE_W, PAGE_H = 210.0, 297.0   # A4
DPI = 300
MARGIN_X = 8.0                  # minimo ai lati del foglio
GAP_X = 4.0                     # tra due cartellini vicini
TOP = 20.0                      # sopra i cartellini: istruzioni e righello
BOTTOM = 12.0
GAP_Y = 7.0                     # tra due righe: c'è il nome del personaggio
RULER_MM = 50.0
ZOOM_MIN = 0.1                  # sotto 1 l'immagine intera sta nel lembo, con bianco intorno
ZOOM_MAX = 4.0

DEFAULT_SPEC = {
    "model": "statuina-1", "paper_w": 24.0, "flap_h": 40.0,
    "window": {"w": 21.0, "h": 37.0, "r": 3.0, "side": 1.5, "bottom": 1.5, "fold": 1.5},
}

# Larghezze dell'Helvetica standard (1/1000 di em), caratteri da 32 a 126
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def load_spec(path: Path) -> dict:
    try:
        with open(path) as f:
            spec = json.load(f)
        float(spec["paper_w"]), float(spec["flap_h"])
        for k in ("w", "h", "r", "side", "bottom", "fold"):
            float(spec["window"][k])
        return spec
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_SPEC


def layout(spec: dict) -> dict:
    """Posizione (in mm dall'angolo in alto a sinistra) di ogni cartellino sul foglio."""
    w, h = spec["paper_w"], 2 * spec["flap_h"]
    cols = int((PAGE_W - 2 * MARGIN_X + GAP_X) // (w + GAP_X))
    rows = int((PAGE_H - TOP - BOTTOM + GAP_Y) // (h + GAP_Y))
    x0 = (PAGE_W - (cols * w + (cols - 1) * GAP_X)) / 2
    slots = [[round(x0 + c * (w + GAP_X), 3), round(TOP + r * (h + GAP_Y), 3)]
             for r in range(rows) for c in range(cols)]
    return {"page": [PAGE_W, PAGE_H], "strip": [w, h], "slots": slots}


def normalize(settings) -> dict:
    """Inquadratura: centro del lembo nell'immagine (cx, cy da 0 a 1) e ingrandimento
    rispetto all'immagine che copre il lembo (zoom 1; sotto 1 resta del bianco)."""
    s = settings if isinstance(settings, dict) else {}
    def num(key, default, lo, hi):
        try:
            v = float(s.get(key, default))
        except (TypeError, ValueError):
            v = default
        return min(max(v, lo), hi) if v == v else default
    return {"cx": num("cx", .5, 0, 1), "cy": num("cy", .5, 0, 1), "zoom": num("zoom", 1, ZOOM_MIN, ZOOM_MAX)}


def crop_box(iw: int, ih: int, spec: dict, settings: dict):
    """Rettangolo dell'immagine (in pixel) che finisce sul lembo. Stesso calcolo
    dell'anteprima nella web UI (cardCrop() in index.html). Più grande dell'immagine
    se è rimpicciolita: allora è l'immagine a restare dentro il lembo."""
    w, fh = spec["paper_w"], spec["flap_h"]
    s = normalize(settings)
    scale = max(w / iw, fh / ih) * s["zoom"]     # mm per pixel
    cw, ch = w / scale, fh / scale
    left = min(max(s["cx"] * iw - cw / 2, min(0, iw - cw)), max(0, iw - cw))
    top = min(max(s["cy"] * ih - ch / 2, min(0, ih - ch)), max(0, ih - ch))
    return (left, top, left + cw, top + ch)


def flap_jpeg(src: Path, spec: dict, settings: dict) -> bytes:
    """Il lembo a 300 dpi, in JPEG: l'immagine ritagliata, su fondo bianco se trasparente."""
    from PIL import Image, ImageOps
    size = (round(spec["paper_w"] / 25.4 * DPI), round(spec["flap_h"] / 25.4 * DPI))
    with Image.open(src) as im:
        im.draft("RGB", (size[0] * 4, size[1] * 4))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        left, top, right, bottom = crop_box(im.width, im.height, spec, settings)
        k = size[0] / (right - left)              # pixel del lembo per pixel dell'immagine
        # la parte dell'immagine dentro il lembo, su bianco (rimpicciolita non lo copre)
        x0, y0 = max(left, 0), max(top, 0)
        x1, y1 = min(right, im.width), min(bottom, im.height)
        out = Image.new("RGB", size, "white")
        part = im.resize((max(1, round((x1 - x0) * k)), max(1, round((y1 - y0) * k))),
                         Image.LANCZOS, box=(x0, y0, x1, y1))
        out.paste(part, (round((x0 - left) * k), round((y0 - top) * k)))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _jpeg_size(data: bytes):
    """Larghezza e altezza da un JPEG (marcatore SOF), senza Pillow."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    raise ValueError("JPEG senza dimensioni")


def _text_width(text: str, size: float) -> float:
    """Larghezza in mm di un testo in Helvetica."""
    units = sum(_HELV[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return units / 1000 * size / MM


def _pdf_text(text: str) -> bytes:
    raw = text.encode("cp1252", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _fit(text: str, size: float, width: float) -> str:
    if _text_width(text, size) <= width:
        return text
    while text and _text_width(text + "…", size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


class _Page:
    """Comandi di disegno di una pagina, in mm con l'origine in alto a sinistra."""
    def __init__(self):
        self.ops = []

    def op(self, s: str):
        self.ops.append(s)

    @staticmethod
    def xy(x, y):
        return f"{x * MM:.2f} {(PAGE_H - y) * MM:.2f}"

    def stroke(self, gray, width_mm, dash=None):
        self.op(f"{gray} G {width_mm * MM:.2f} w")
        self.op(f"[{dash[0] * MM:.2f} {dash[1] * MM:.2f}] 0 d" if dash else "[] 0 d")

    def line(self, x1, y1, x2, y2):
        self.op(f"{self.xy(x1, y1)} m {self.xy(x2, y2)} l S")

    def rect(self, x, y, w, h):
        self.op(f"{self.xy(x, y + h)} {w * MM:.2f} {h * MM:.2f} re S")

    def round_rect(self, x, y, w, h, r):
        k = r * 0.5523
        p = [f"{self.xy(x + r, y)} m", f"{self.xy(x + w - r, y)} l",
             f"{self.xy(x + w - r + k, y)} {self.xy(x + w, y + r - k)} {self.xy(x + w, y + r)} c",
             f"{self.xy(x + w, y + h - r)} l",
             f"{self.xy(x + w, y + h - r + k)} {self.xy(x + w - r + k, y + h)} {self.xy(x + w - r, y + h)} c",
             f"{self.xy(x + r, y + h)} l",
             f"{self.xy(x + r - k, y + h)} {self.xy(x, y + h - r + k)} {self.xy(x, y + h - r)} c",
             f"{self.xy(x, y + r)} l",
             f"{self.xy(x, y + r - k)} {self.xy(x + r - k, y)} {self.xy(x + r, y)} c S"]
        self.op(" ".join(p))

    def triangle(self, gray, pts):
        (x1, y1), (x2, y2), (x3, y3) = pts
        self.op(f"{gray} g {self.xy(x1, y1)} m {self.xy(x2, y2)} l {self.xy(x3, y3)} l f")

    def image(self, name, x, y, w, h, upside_down=False):
        a, d = w * MM, h * MM
        e, f = x * MM, (PAGE_H - y - h) * MM
        if upside_down:
            self.op(f"q {-a:.2f} 0 0 {-d:.2f} {e + a:.2f} {f + d:.2f} cm /{name} Do Q")
        else:
            self.op(f"q {a:.2f} 0 0 {d:.2f} {e:.2f} {f:.2f} cm /{name} Do Q")

    def text(self, x, y, size, s, gray=0.25):
        self.ops.append(f"{gray} g BT /F1 {size} Tf {self.xy(x, y)} Td ".encode() + _pdf_text(s) + b" Tj ET")

    def content(self) -> bytes:
        return b"\n".join(o if isinstance(o, bytes) else o.encode() for o in self.ops)


def _draw_header(page: _Page, x0: float):
    page.text(x0, 9.5, 8, "Ritaglia sul contorno, piega sul tratteggio e infila il cartellino "
                          "nella statuina, con la piega in alto.")
    # righello di controllo: se la stampa è stata adattata alla pagina, non è 5 cm
    y = 15.0
    page.stroke(0.2, 0.3)
    page.line(x0, y, x0 + RULER_MM, y)
    for i in range(6):
        tick = 2.2 if i in (0, 5) else 1.2
        page.line(x0 + i * 10, y - tick, x0 + i * 10, y)
    page.text(x0 + RULER_MM + 3, y + 0.2, 7,
              "Questa riga deve misurare 5 cm. Se è più corta, stampa al 100% (dimensione reale).",
              gray=0.4)


def _draw_card(page: _Page, spec: dict, x: float, y: float, image, label):
    w, fh = spec["paper_w"], spec["flap_h"]
    win = spec["window"]
    fold = y + fh
    if image:
        page.image(image, x, y, w, fh, upside_down=True)
        page.image(image, x, fold, w, fh)
    else:
        # da disegnare: il contorno delle finestre e, nel bordo nascosto dalla
        # cornice, due triangolini verso la piega, che è l'alto di entrambi i lembi
        page.stroke(0.6, 0.15)
        page.round_rect(x + win["side"], y + win["bottom"], win["w"], win["h"], win["r"])
        page.round_rect(x + win["side"], fold + win["fold"], win["w"], win["h"], win["r"])
        cx, t = x + w / 2, min(win["fold"] * 0.6, 1.0)
        for sgn in (-1, 1):
            tip = fold + sgn * win["fold"] * 0.2
            base = tip + sgn * t
            page.triangle(0.7, [(cx, tip), (cx - t, base), (cx + t, base)])
    page.stroke(0.45, 0.2, dash=(1.2, 0.8))
    page.line(x, fold, x + w, fold)
    page.stroke(0.3, 0.2)
    page.rect(x, y, w, 2 * fh)
    if label:
        size = 6.5
        text = _fit(label, size, w + GAP_X - 1)
        page.text(x + (w - _text_width(text, size)) / 2, y + 2 * fh + 3.4, size, text, gray=0.4)


def build_pdf(spec: dict, cards: list, title: str = "Cartellini di Cucù") -> bytes:
    """cards: (jpeg o None, nome sotto il cartellino o None) per ogni casella, in ordine;
    None al posto del JPEG = cartellino da disegnare. Lo stesso JPEG (stesso oggetto
    bytes) si incorpora una volta sola."""
    lay = layout(spec)
    per_page = len(lay["slots"])
    pages = [cards[i:i + per_page] for i in range(0, len(cards), per_page)] or [[]]

    objs = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    def stream(head: bytes, data: bytes) -> bytes:
        return b"<< " + head + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"

    catalog = add(b"")
    pages_id = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    images = {}
    for jpeg, _ in cards:
        if jpeg is not None and id(jpeg) not in images:
            iw, ih = _jpeg_size(jpeg)
            oid = add(stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                             b"/BitsPerComponent 8 /Filter /DCTDecode" % (iw, ih), jpeg))
            images[id(jpeg)] = (f"Im{len(images) + 1}", oid)
    xobjects = b" ".join(b"/%s %d 0 R" % (n.encode(), oid) for n, oid in images.values())
    resources = b"<< /Font << /F1 %d 0 R >> /XObject << %s >> >>" % (font, xobjects)

    kids = []
    for chunk in pages:
        page = _Page()
        _draw_header(page, lay["slots"][0][0])
        for (x, y), (jpeg, label) in zip(lay["slots"], chunk):
            _draw_card(page, spec, x, y, images[id(jpeg)][0] if jpeg is not None else None, label)
        content = add(stream(b"/Filter /FlateDecode", zlib.compress(page.content())))
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %s /Contents %d 0 R >>"
                        % (pages_id, PAGE_W * MM, PAGE_H * MM, resources, content)))
    objs[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    # PrintScaling /None: chi lo rispetta propone la stampa al 100%
    objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R /ViewerPreferences << /PrintScaling /None >> >>" % pages_id
    info = add(b"<< /Title " + _pdf_text(title) + b" /Producer (Cucu) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, catalog, info, xref)
    return bytes(out)
