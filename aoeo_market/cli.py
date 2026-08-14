"""Command-line entry points.

    uv run python -m aoeo_market.cli dump   <capture>   # list all listings
    uv run python -m aoeo_market.cli replay <capA> <capB># diff two snapshots -> events
"""

from __future__ import annotations

import argparse
import sys

from .constants import CELESTE_NETWORK_HOST, CELESTE_NETWORK_PORT
from .observer import MarketObserver
from .pcap_source import listings_from_pcap


def _dump(args: argparse.Namespace) -> int:
    listings = sorted(listings_from_pcap(args.capture), key=lambda l: (l.item_type, l.item_id, l.item_price))
    print(f"{len(listings)} active listings\n")
    print(f"{'ITEM_ID':28} {'TYPE':9} {'LVL':>3} {'CNT':>3} {'PRICE':>8} {'EXPIRES(d)':>10}  SELLER")
    for l in listings:
        print(f"{l.item_id:28.28} {l.item_type:9.9} {l.item_level:3d} {l.item_count:3d} "
              f"{l.item_price:8d} {l.seconds_till_expiry/86400:10.1f}  {l.seller_empire_id}")
    return 0


def _replay(args: argparse.Namespace) -> int:
    # Treat two captures as two points in time; grace huge so pre-expiry
    # disappearances read as SOLD_OR_CANCELLED for the demo.
    obs = MarketObserver(expiry_grace_seconds=60, clock=lambda: 0.0)
    for ev in obs.observe(listings_from_pcap(args.first), at=0.0):
        pass  # first snapshot is all LISTED; suppress for a clean diff view
    events = obs.observe(listings_from_pcap(args.second), at=args.gap)
    for ev in events:
        if ev.kind == "LISTED":
            print(f"LISTED    tx={ev.listing.transaction_id} {ev.listing.item_id} @ {ev.listing.item_price}")
        else:
            print(f"REMOVED   tx={ev.listing.transaction_id} {ev.listing.item_id} -> {ev.reason.value}")
    print(f"\n{len(events)} events")
    return 0


def _probe(args: argparse.Namespace) -> int:
    from .live_probe import probe

    return probe(
        mail=args.email,
        password=args.password,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        try_game=args.game,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aoeo_market")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="list all active listings in a capture")
    d.add_argument("capture")
    d.set_defaults(func=_dump)

    r = sub.add_parser("replay", help="diff two captures into market events")
    r.add_argument("first")
    r.add_argument("second")
    r.add_argument("--gap", type=float, default=3600.0, help="seconds between snapshots")
    r.set_defaults(func=_replay)

    pr = sub.add_parser(
        "probe", help="attempt a live connection to the game backend"
    )
    pr.add_argument("--email", help="account email (or $AOEO_EMAIL)")
    pr.add_argument("--password", help="account password (or $AOEO_PASSWORD)")
    pr.add_argument(
        "--host", default=CELESTE_NETWORK_HOST,
        help=f"Celeste Network host (default {CELESTE_NETWORK_HOST})",
    )
    pr.add_argument(
        "--port", type=int, default=CELESTE_NETWORK_PORT,
        help=f"Celeste Network port (default {CELESTE_NETWORK_PORT})",
    )
    pr.add_argument(
        "--timeout", type=float, default=15.0,
        help="socket timeout in seconds",
    )
    pr.add_argument(
        "--game", action="store_true",
        help="also attempt the TCP 1510 login handshake",
    )
    pr.set_defaults(func=_probe)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
