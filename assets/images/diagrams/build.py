"""Render the GIP architecture diagrams: python3 build.py [--svg-dir DIR] [--png-dir DIR] [name ...]

Output layout. In the repository (this file in assets/images/diagrams/) the SVG sources are written beside the
scripts and the 2x PNGs one directory up, in assets/images/. Anywhere else both go to ./out/. --svg-dir and
--png-dir override either. Exits 1 on any layout-check error. Stdlib only; needs rsvg-convert on PATH.
"""
import argparse
import os
import sys

import d1_platform_overview
import d2_credential_flow
import d3_websearch
import d4_memory
import d5_skills_registry

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_LAYOUT = os.path.basename(HERE) == "diagrams" and os.path.basename(os.path.dirname(HERE)) == "images"
SVG_DIR = HERE if REPO_LAYOUT else os.path.join(HERE, "out")
PNG_DIR = os.path.dirname(HERE) if REPO_LAYOUT else os.path.join(HERE, "out")
DIAGRAMS = {m.NAME: m for m in (d1_platform_overview, d2_credential_flow, d3_websearch, d4_memory, d5_skills_registry)}


def main(argv):
    ap = argparse.ArgumentParser(description="Render the GIP architecture diagrams.")
    ap.add_argument("names", nargs="*", help=f"diagrams to build (default: all): {', '.join(DIAGRAMS)}")
    ap.add_argument("--svg-dir", default=SVG_DIR, help=f"SVG output directory (default: {SVG_DIR})")
    ap.add_argument("--png-dir", default=PNG_DIR, help=f"PNG output directory (default: {PNG_DIR})")
    args = ap.parse_args(argv)
    unknown = [n for n in args.names if n not in DIAGRAMS]
    if unknown:
        ap.error(f"unknown diagram(s): {', '.join(unknown)}")
    failed = 0
    for name, mod in DIAGRAMS.items():
        if args.names and name not in args.names:
            continue
        d = mod.build(args.svg_dir, args.png_dir)
        status = "OK" if not d.errors else f"{len(d.errors)} check errors"
        print(f"{name}: {status} | {d.w}x{d.h} | min font {d.min_font:g} px = {d.display_px:.2f} px at 980 | "
              f"nodes {len(d.nodes)} | edges {len(d.edges)}")
        for e in d.errors[:60]:
            print("   ", e)
        failed += bool(d.errors)
    print(f"SVG -> {args.svg_dir}\nPNG -> {args.png_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
