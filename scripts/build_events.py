"""Build the browser-side seasonal-event index from the committed item catalog.

Event filtering happens entirely in the front end.  ``/api/listings`` already
carries the catalog's ``event`` field, but the best-sellers, best-value and
not-on-sale views only return the item id, so the browser needs one compact
lookup it can consult for any row.  This script flattens the catalog's event
metadata (plus the event acronyms the game uses) into that file::

    aoeo_market/data/catalog.json  ->  aoeo_market/web/static/events.json

Keys are the lowercased wire ids; values are ``{name, year?, acronym}``.  Items
whose catalog entry has no event but whose id encodes one (``SE2025_…``,
``HE_Blackwood``, ``WE_IceKingsHeart``, ``Axe2H_Halloween2020``,
``CreateArmorBldg_Winter2021``, ``…_IceKing_LEG``, and the event vanity items)
are included too, so the browser covers the gaps in the curated metadata.  The
front end applies the same id heuristic to ids the index has never seen.

Refresh it whenever the catalog changes::

    uv run python -m scripts.build_events

Run that from the repository root so ``aoeo_market`` is importable.  Only the
committed catalog is read — the gitignored ``ProjectCeleste`` tree is not
needed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Seasonal events as the curated database names them, and the short forms the
# game and the marketplace item ids use.
EVENT_ACRONYMS = {
    "summer": "SE",
    "halloween": "HE",
    "winter": "WE",
}

_EVENT_ID_TOKENS = {"se": "summer", "he": "halloween", "we": "winter"}
_EVENT_WORDS = ("halloween", "summer", "winter")


def event_acronym(name: str | None) -> str | None:
    """The short form of an event name (Summer Event -> SE)."""
    if not name:
        return None
    key = str(name).strip().lower()
    known = EVENT_ACRONYMS.get(key)
    if known:
        return known
    words = [word for word in re.split(r"[^a-z0-9]+", key) if word]
    if not words:
        return None
    if len(words) > 1:
        return "".join(word[0] for word in words).upper()
    return words[0][:2].upper()


def infer_event(item_id: str) -> dict | None:
    """Recover ``{name[, year]}`` from a wire id that encodes its event."""
    text = item_id.lower()
    name: str | None = None
    year: int | None = None
    for token in re.split(r"[^a-z0-9]+", text):
        if not token:
            continue
        if token in _EVENT_ID_TOKENS:
            name = _EVENT_ID_TOKENS[token]
            break
        acronym = re.fullmatch(r"(se|he|we)(\d{4})", token)
        if acronym:
            name = _EVENT_ID_TOKENS[acronym.group(1)]
            year = int(acronym.group(2))
            break
        for word in _EVENT_WORDS:
            if token.startswith(word):
                name = word
                tail = token[len(word) :]
                if len(tail) == 4 and tail.isdigit():
                    year = int(tail)
                break
        if name:
            break
    if name is None and "iceking" in text:
        name = "winter"
    if name is None:
        return None
    event = {"name": name}
    if year is not None:
        event["year"] = year
    return event


def _vanity_event(entry: dict) -> dict | None:
    """A vanity item's name can name its event when its id does not."""
    if entry.get("kind") != "item" or not str(entry.get("type") or "").startswith("Vanity"):
        return None
    name = str(entry.get("name") or "").lower()
    for word in _EVENT_WORDS:
        if word in name:
            return {"name": word}
    return None


def event_of(item_id: str, entry: dict) -> dict | None:
    """The event ``{name[, year], acronym}`` for one catalog entry."""
    raw = entry.get("event") or {}
    event: dict | None = None
    if raw.get("name"):
        event = {"name": str(raw["name"])}
        if isinstance(raw.get("year"), int):
            event["year"] = raw["year"]
    if event is None:
        event = infer_event(item_id) or _vanity_event(entry)
    if event is None:
        return None
    acronym = event_acronym(event["name"])
    if acronym:
        event["acronym"] = acronym
    return event


def build_index(catalog_path: str | Path) -> dict[str, dict]:
    """Return the sorted ``{lowercase item id: event}`` index."""
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    index: dict[str, dict] = {}
    for item_id, entry in catalog.items():
        event = event_of(item_id, entry)
        if event is not None:
            index[item_id] = event
    return dict(sorted(index.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the dashboard's browser-side seasonal-event index from the item catalog.")
    parser.add_argument("--catalog", default="aoeo_market/data/catalog.json", help="input catalog JSON path")
    parser.add_argument("--out", default="aoeo_market/web/static/events.json", help="output JSON path")
    args = parser.parse_args(argv)

    index = build_index(args.catalog)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators keep the committed file small; it is fetched once and
    # kept in memory for the life of the page.
    out.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(index)} events)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
