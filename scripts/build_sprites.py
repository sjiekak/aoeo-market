"""Build the front-end item-icon sprite assets from the celeste-search sheets.

The Project Celeste "search" web app (``ProjectCeleste/celeste-search``) ships
its item icons as sprite sheets under ``src/assets/sprites/<kind>.webp``, one
sheet per entity kind (advisor / blueprint / consumable / design / item /
material).  Each sheet has a generated ``.scss`` sibling that positions every
icon as a CSS class::

    .icon--designs--M72n1hPX { background-position: 76.190476% 10%; }

The browser clips an icon with that ``background-position`` — the source app's
own technique — so **all sprite work happens in the front end**.  This script
only stages the assets the browser needs:

* ``aoeo_market/web/static/sprites.json`` — ``{kind: {icon: background-position}}``
  committed alongside the dashboard, parsed from the ``.scss`` files;
* ``aoeo_market/web/static/sprites/<kind>.webp`` — the six sprite sheets,
  copied verbatim.

Both are served as ordinary static files (``/static/sprites.json`` and
``/static/sprites/<kind>.webp``); the Python backend performs no sprite lookup
or image processing.  Re-run whenever the sprite sheets change::

    uv run python scripts/build_sprites.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# Singular kind -> sprite sheet name (the scss class prefix and webp filename).
_KIND_SHEETS = {
    "advisor": "advisors",
    "blueprint": "blueprints",
    "consumable": "consumables",
    "design": "designs",
    "item": "items",
    "material": "materials",
}

# `.icon--<sheet>--<icon> { background-position: <X> <Y>; }` — the icon key
# may contain alphanumerics, dashes and underscores; the position string is
# captured verbatim so the browser renders it identically to the source CSS.
_RULE = re.compile(r"\.icon--([a-z]+)--([\w\-.]+)\s*\{\s*background-position:\s*([^;]+);")


def build(sprites_dir: Path) -> dict[str, dict[str, str]]:
    """Return ``{kind: {icon: background-position}}`` from the sprite scss files."""
    index: dict[str, dict[str, str]] = {}
    for kind, sheet in _KIND_SHEETS.items():
        scss = sprites_dir / f"{sheet}.scss"
        if not scss.exists():
            print(f"warning: missing {scss}; skipping {kind}", file=sys.stderr)
            continue
        rules: dict[str, str] = {}
        for m in _RULE.finditer(scss.read_text(encoding="utf-8")):
            if m.group(1) != sheet:
                continue
            rules[m.group(2)] = m.group(3).strip()
        index[kind] = rules
    return index


def _grid(rules: dict[str, str]) -> dict[str, int]:
    """Grid dimensions (columns × rows) of one sprite sheet.

    Each icon is positioned at ``i/(cols-1)*100% j/(rows-1)*100%``, so the
    number of distinct x/y percentages equals the column/row count.  A bare
    ``0`` (no ``%``) and ``0%`` are the same cell.  The frontend uses these to
    scale the sheet with ``background-size: cols*100% rows*100%`` so a cell
    can be clipped at any pixel size.
    """
    xs: set[str] = set()
    ys: set[str] = set()
    for pos in rules.values():
        x, y = pos.split()
        xs.add(x.rstrip("%"))
        ys.add(y.rstrip("%"))
    return {"cols": len(xs), "rows": len(ys)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the front-end item-icon sprite assets from the celeste-search sprite sheets.")
    p.add_argument("--src", default="ProjectCeleste/celeste-search/src/assets/sprites", help="path to the celeste-search sprites directory")
    p.add_argument("--static", default="aoeo_market/web/static", help="dashboard static directory (sprites.json and sprites/ are written here)")
    args = p.parse_args(argv)

    src = Path(args.src)
    if not any(src.glob("*.scss")):
        print(f"error: no sprite .scss files at {src}", file=sys.stderr)
        return 2

    index = build(src)
    if not index:
        print(f"error: no sprite icons found at {src}", file=sys.stderr)
        return 2

    static = Path(args.static)
    static.mkdir(parents=True, exist_ok=True)
    document = {"@sheets": {kind: _grid(rules) for kind, rules in index.items()}, **index}
    static.joinpath("sprites.json").write_text(
        _json_dumps(document), encoding="utf-8"
    )

    sprites = static / "sprites"
    sprites.mkdir(parents=True, exist_ok=True)
    copied = 0
    for sheet in _KIND_SHEETS.values():
        source = src / f"{sheet}.webp"
        if not source.exists():
            print(f"warning: missing {source}; icon images for {sheet} will be absent", file=sys.stderr)
            continue
        shutil.copyfile(source, sprites / source.name)
        copied += 1

    total = sum(len(v) for v in index.values())
    print(f"wrote {static / 'sprites.json'} ({total} icons) and copied {copied} sprite sheets to {sprites}", file=sys.stderr)
    return 0


def _json_dumps(index: dict[str, dict[str, str]]) -> str:
    import json

    return json.dumps(index, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


if __name__ == "__main__":
    sys.exit(main())
