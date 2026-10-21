"""Build the browser-side type-ahead index from the committed item catalog.

The search box autocompletes **in the browser**: ``app.js`` loads one compact
JSON file, then matches and ranks it locally, so a keystroke never asks the
server (and therefore never touches DuckDB).  This script flattens the curated
catalog into that file::

    aoeo_market/data/catalog.json  ->  aoeo_market/web/static/suggestions.json

Each row carries only what a suggestion shows (id, name, type, kind, icon,
rarity, craftability) and the rows are sorted by id, so the generated file is
deterministic and diff-friendly.  It is committed alongside the dashboard,
like ``sprites.json``; refresh it whenever the catalog changes::

    uv run python -m scripts.build_suggestions

Run that from the repository root so ``aoeo_market`` is importable.  Only the
committed catalog is read — the gitignored ``ProjectCeleste`` tree is not
needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aoeo_market import catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the dashboard's browser-side type-ahead index from the item catalog.")
    parser.add_argument("--out", default="aoeo_market/web/static/suggestions.json", help="output JSON path")
    args = parser.parse_args(argv)

    rows = catalog.suggestion_index()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators keep the committed file small; it is fetched once and
    # kept in memory for the life of the page.
    out.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(rows)} items)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
