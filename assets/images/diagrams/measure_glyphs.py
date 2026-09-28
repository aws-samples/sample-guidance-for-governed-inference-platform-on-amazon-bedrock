"""Dev tool: measure the opaque glyph of every icon and write icons/glyph-extents.json.

Run only when icons/ changes:  uv run --with pillow python3 measure_glyphs.py
Needs rsvg-convert. The build itself stays stdlib-only and just reads the JSON.

Each icon is rasterised at N x N px (its viewBox fills the square, as in the diagrams).
"rows"[r] = [min_x, max_x] of pixels with alpha >= 128 in row r, or null when the row is empty;
"cols"[c] = [min_y, max_y] likewise. Units are pixels of the N-px raster.
"""
import json
import os
import subprocess
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "icons")
N = 128
ALPHA = 128


def extents(path):
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "g.png")
        subprocess.run(["rsvg-convert", "-w", str(N), "-h", str(N), "-o", png, path], check=True)  # nosec B603
        alpha = Image.open(png).convert("RGBA").getchannel("A").load()
    rows, cols = [], []
    for r in range(N):
        xs = [x for x in range(N) if alpha[x, r] >= ALPHA]
        rows.append([xs[0], xs[-1]] if xs else None)
    for c in range(N):
        ys = [y for y in range(N) if alpha[c, y] >= ALPHA]
        cols.append([ys[0], ys[-1]] if ys else None)
    return {"rows": rows, "cols": cols}


def main():
    out = {"n": N, "alpha_threshold": ALPHA, "icons": {}}
    for name in sorted(os.listdir(ICONS)):
        if name.endswith(".svg"):
            out["icons"][name[:-4]] = extents(os.path.join(ICONS, name))
    lines = ['{"n": %d, "alpha_threshold": %d, "icons": {' % (N, ALPHA)]
    items = [f"{json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}" for k, v in out["icons"].items()]
    lines.append(",\n".join(items))
    lines.append("}}")
    with open(os.path.join(ICONS, "glyph-extents.json"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(items)} icons at N={N}")


if __name__ == "__main__":
    main()
