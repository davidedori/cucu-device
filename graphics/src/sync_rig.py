#!/usr/bin/env python3
"""
Copia il rig del gufetto dal laboratorio (~/Documents/_CODING/cucu-animazioni/animazioni.html)
dentro graphics/src/tv-loops.html, tra i marcatori <rig>:

- CSS: dal blocco "RIG —" fino a fine <style> (perni e keyframe delle animazioni)
- JS:  forme di cucu-parti.svg (P, EYE, PIVOT) e owlSVG()

    python3 graphics/src/sync_rig.py

Da rilanciare quando cambia il laboratorio (per esempio dopo aggiorna_rig.py).
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
LAB = Path.home() / "Documents/_CODING/cucu-animazioni/animazioni.html"
DEST = SRC / "tv-loops.html"


def between(text, start, end, what):
    i = text.find(start)
    j = text.find(end, i)
    if i < 0 or j < 0:
        sys.exit(f"Nel laboratorio non trovo {what}")
    return text[i:j]


def main():
    lab = LAB.read_text(encoding="utf-8")
    css = between(lab, "/* =====================================================================\n     RIG", "</style>", "il CSS del rig")
    forms = between(lab, "// <forme cucu-parti.svg>", "// </forme cucu-parti.svg>", "le forme")
    m = re.search(r"^function owlSVG\(.*?^}\n", lab, re.S | re.M)
    if not m:
        sys.exit("Nel laboratorio non trovo owlSVG()")
    js = (forms + "// </forme cucu-parti.svg>\n\nlet uid = 0;\n"
          "const pv = (x, y) => `<g class=\"pv\"><circle cx=\"${x}\" cy=\"${y}\" r=\"20\"/><circle cx=\"${x}\" cy=\"${y}\" r=\"6\"/></g>`;\n\n"
          + m.group(0))

    page = DEST.read_text(encoding="utf-8")
    page, n1 = re.subn(r"(/\* <rig> \*/\n).*?(/\* </rig> \*/)", lambda m: m.group(1) + css.rstrip() + "\n" + m.group(2), page, flags=re.S)
    page, n2 = re.subn(r"(// <rig>\n).*?(// </rig>)", lambda m: m.group(1) + js + m.group(2), page, flags=re.S)
    if n1 != 1 or n2 != 1:
        sys.exit("In tv-loops.html mancano i marcatori <rig>")
    DEST.write_text(page, encoding="utf-8")
    print(f"Rig copiato in {DEST.name}")


if __name__ == "__main__":
    main()
