"""Command-line entry points.

uv run python -m aoeo_market.cli dump   <capture>   # list all listings
uv run python -m aoeo_market.cli replay <capA> <capB># diff two snapshots -> events
uv run python -m aoeo_market.cli fetch               # read the live market
uv run python -m aoeo_market.cli fetch  --watch      # stream events

The live commands detect your local IPv4 address as the default; pass
``--local-ip <ip>`` to override it.
"""

from __future__ import annotations

import argparse
import sys
import time

from .cli_args import add_login_args, parse_device_hash, parse_tail, resolve_local_ip
from .client import MarketClient
from .constants import (
    DEFAULT_POLL_INTERVAL,
    GAME_SERVER_HOST,
    GAME_SERVER_PORT,
)
from .market import Listing
from .observer import Event, MarketObserver
from .pcap_source import listings_from_pcap

_HEADER = f"{'ITEM_ID':28} {'TYPE':9} {'LVL':>3} {'CNT':>3} {'PRICE':>8} {'EXPIRES(d)':>10}  SELLER"


def _print_listings(listings: list[Listing]) -> None:
    listings = sorted(listings, key=lambda l: (l.item_type, l.item_id, l.item_price))
    print(f"{len(listings)} active listings\n")
    print(_HEADER)
    for l in listings:
        print(
            f"{l.item_id:28.28} {l.item_type:9.9} {l.item_level:3d} {l.item_count:3d} "
            f"{l.item_price:8d} {l.seconds_till_expiry / 86400:10.1f}  {l.seller_empire_id}"
        )


def _print_event(ev: Event) -> None:
    stamp = time.strftime("%H:%M:%S")
    if ev.kind == "LISTED":
        print(f"[{stamp}] LISTED   tx={ev.listing.transaction_id} {ev.listing.item_id} @ {ev.listing.item_price}")
    else:
        print(f"[{stamp}] REMOVED  tx={ev.listing.transaction_id} {ev.listing.item_id} -> {ev.reason.value}")


def _dump(args: argparse.Namespace) -> int:
    _print_listings(listings_from_pcap(args.capture))
    return 0


def _replay(args: argparse.Namespace) -> int:
    # Treat two captures as two points in time. A listing that vanishes with
    # less than a day left on its countdown reads as EXPIRED; any earlier
    # disappearance is just REMOVED (sold or withdrawn — indistinguishable).
    obs = MarketObserver(clock=lambda: 0.0)
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


def _login_identity(args: argparse.Namespace) -> tuple[str, bytes] | int:
    """Validate the ``--device-hash`` / ``--tail`` pair for a live command.

    Returns ``(device_hash, opaque_tail)``, or the process exit code 2 after
    reporting a malformed value.
    """
    try:
        device_hash = parse_device_hash(args.device_hash)
        opaque = parse_tail(args.tail)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return device_hash, opaque


def _probe(args: argparse.Namespace) -> int:
    from .live_probe import probe

    identity = _login_identity(args)
    if isinstance(identity, int):
        return identity
    device_hash, opaque = identity
    return probe(
        local_ip=args.local_ip,
        mail=args.email,
        password=args.password,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        try_game=args.game,
        device_hash=device_hash,
        opaque=opaque,
    )


def _watch(mc: MarketClient, interval: float) -> int:
    """Prime the observer with the current snapshot, then stream events."""
    listings = mc.fetch_listings()
    _print_listings(listings)
    mc.observer.observe(listings)  # first snapshot: suppress the LISTED flood
    while True:
        mc.ping()
        time.sleep(interval)
        for ev in mc.poll_once():
            _print_event(ev)


def _fetch(args: argparse.Namespace) -> int:
    from .live_probe import resolve_credentials

    identity = _login_identity(args)
    if isinstance(identity, int):
        return identity
    device_hash, opaque = identity

    mail, password = resolve_credentials(args.email, args.password)
    mc = MarketClient(
        server=args.game_host,
        port=args.game_port,
        connect_timeout=args.timeout,
        poll_interval=args.interval,
    )
    try:
        print(f"Logging in over Celeste Network {args.host}:{args.port} ...", file=sys.stderr)
        session = mc.acquire_session(
            mail,
            password,
            args.local_ip,
            host=args.host,
            port=args.port,
            device_hash=device_hash,
            opaque=opaque,
        )
        print(
            f"Logged in as {session.username} (xuid {session.xuid}); connecting to game service {args.game_host}:{args.game_port} ...",
            file=sys.stderr,
        )
        mc.login(session)
        if args.watch:
            return _watch(mc, args.interval)
        _print_listings(mc.fetch_listings())
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 0
    finally:
        mc.close()


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

    pr = sub.add_parser("probe", help="attempt a live connection to the game backend")
    add_login_args(pr)
    pr.add_argument(
        "--game",
        action="store_true",
        help="also attempt the TCP 1510 login handshake",
    )
    pr.set_defaults(func=_probe)

    f = sub.add_parser("fetch", help="log in and read the live market listing")
    add_login_args(f)
    f.add_argument(
        "--game-host",
        default=GAME_SERVER_HOST,
        help=f"game service host (default {GAME_SERVER_HOST})",
    )
    f.add_argument(
        "--game-port",
        type=int,
        default=GAME_SERVER_PORT,
        help=f"game service port (default {GAME_SERVER_PORT})",
    )
    f.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"seconds between polls with --watch (default {DEFAULT_POLL_INTERVAL})",
    )
    f.add_argument(
        "--watch",
        action="store_true",
        help="keep polling and stream LISTED/REMOVED events instead of exiting",
    )
    f.set_defaults(func=_fetch)

    args = p.parse_args(argv)
    if hasattr(args, "local_ip"):
        args.local_ip = resolve_local_ip(p, args.local_ip)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
