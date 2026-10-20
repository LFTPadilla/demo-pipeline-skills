#!/usr/bin/env python3
"""Build a page with the review layer.

Usage: inject.py <page.html> [<out.html>]
The page holds the marker <!-- cmt:layer -->. The script replaces it with the layer from assets/layer/layer.css and layer.js.
A page built earlier holds a <!-- cmt:begin ... <!-- cmt:end --> block. The script replaces that block too, so it upgrades old pages.
Without <out.html> the page is rewritten in place. Run it at build or deploy time only: the repo templates hold the marker, not the layer.
live-app-review does not use this script. Its proxy serves the same two files at /__review/layer.css and /__review/layer.js.
"""
import re
import sys
from pathlib import Path

# Path.resolve() follows symlinks, so a symlinked install still finds the layer.
LAYER = Path(__file__).resolve().parent.parent / "assets" / "layer"
SLOT = re.compile(r"<!-- cmt:layer -->|<!-- cmt:begin.*?<!-- cmt:end -->", re.S)


def build(html: str) -> str:
    """Return html with the layer in place of the marker or of an older layer block."""
    css, js = (LAYER / "layer.css").read_text(encoding="utf-8"), (LAYER / "layer.js").read_text(encoding="utf-8")
    if len(SLOT.findall(html)) != 1:
        sys.exit("expected exactly one <!-- cmt:layer --> marker (or one cmt:begin ... cmt:end block) in the page")
    block = f"<!-- cmt:begin (review layer. Built by mockup-review/scripts/inject.py. Do not edit by hand) -->\n<style>\n{css}</style>\n<script>\n{js}</script>\n<!-- cmt:end -->"
    return SLOT.sub(lambda _m: block, html, count=1)


def main(argv: list[str]) -> None:
    if len(argv) not in (1, 2) or argv[0].startswith("-"):
        sys.exit(__doc__)
    src = Path(argv[0])
    page = src.read_text(encoding="utf-8")
    if 'id="cmt-config"' not in page:
        print("warning: the page has no cmt-config block. Copy it from the template.", file=sys.stderr)
    out = Path(argv[1]) if len(argv) == 2 else src
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(page), encoding="utf-8")
    print(f"{out}: layer built")


if __name__ == "__main__":
    main(sys.argv[1:])
