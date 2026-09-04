"""Live connection probe for the Project Celeste game backend.

Attempts a *real* network round-trip against the Celeste Network login service
(TCP 4564) and, optionally, the game service (TCP 1510). This is the manual
counterpart to the offline capture-based tests: it validates that the login
bytes reconstructed in :mod:`aoeo_market.auth` are still accepted by the live
server.

Credentials are taken from, in order: explicit arguments, the ``AOEO_EMAIL`` /
``AOEO_PASSWORD`` environment variables, or an interactive prompt. The password
is never echoed or logged.

    uv run python -m aoeo_market.live_probe
    uv run python -m aoeo_market.cli probe --email you@example.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import auth
from .cli_args import add_login_args, parse_device_hash, resolve_local_ip, resolve_xlive_crc
from .client import MarketClient, Session
from .constants import (
    CELESTE_NETWORK_HOST,
    CELESTE_NETWORK_PORT,
    DEFAULT_CONNECT_TIMEOUT,
)


def resolve_credentials(mail: str | None, password: str | None) -> tuple[str, str]:
    """Return ``(email, password)`` from args, env, or a prompt."""
    if not mail:
        mail = os.environ.get("AOEO_EMAIL") or input("Email: ").strip()
    if not password:
        password = os.environ.get("AOEO_PASSWORD") or getpass.getpass("Password: ")
    return mail, password


def probe(
    local_ip: str,
    mail: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    try_game: bool = False,
    *,
    device_hash: str,
    xlive_crc32: bytes,
) -> int:
    """Attempt a live login and return a process exit code (0 on success).

    ``device_hash`` is the per-install fingerprint and ``xlive_crc32`` is the
    little-endian CRC-32 of the installed xlive.dll; both are required — this
    function does not infer them.  The CLI entry points (:mod:`aoeo_market.cli`
    and :func:`main`) default them to the captured values and the live
    manifest via :func:`aoeo_market.cli_args.add_login_args`.
    """
    host = host or CELESTE_NETWORK_HOST
    port = port or CELESTE_NETWORK_PORT
    mail, password = resolve_credentials(mail, password)

    print(f"Connecting to Celeste Network {host}:{port} ...", file=sys.stderr)
    cn = auth.CelesteNetworkClient(host=host, port=port, timeout=timeout)
    try:
        try:
            session = cn.login(
                mail,
                password,
                local_ip,
                device_hash=device_hash,
                xlive_crc32=xlive_crc32,
            )
        except auth.LoginRejected as exc:
            print(f"FAILED - 4564 login rejected: {exc}", file=sys.stderr)
            return 1
        except auth.ProtocolError as exc:
            print(f"FAILED - malformed 4564 reply: {exc}", file=sys.stderr)
            return 1
        manifest_received = cn.manifest_received
    finally:
        cn.close()

    print("OK - 4564 login accepted")
    print(f"  xuid        = {session.xuid} (0x{session.xuid:016x})")
    print(f"  username    = {session.username}")
    print(f"  token       = {session.token}")
    print(f"  external_ip = {session.external_ip}")
    if not manifest_received:
        print(
            "  note: server closed after session register; the ~135 KB "
            "manifest was not sent. The session token may still be usable "
            "for the 1510 login (try --game)."
        )
    if try_game:
        return _probe_game(session, timeout)
    return 0


def _probe_game(session: auth.GameSession, timeout: float) -> int:
    """Attempt the TCP 1510 login (full captured handshake) and read a reply."""
    print("\nAttempting TCP 1510 login handshake ...", file=sys.stderr)
    mc = MarketClient(connect_timeout=timeout)
    try:
        try:
            reply = mc.login(
                Session(
                    xuid=session.xuid,
                    username=session.username,
                    token=session.token,
                )
            )
        except auth.LoginRejected as exc:
            print(f"  FAILED - 1510 login rejected: {exc}", file=sys.stderr)
            return 1
        print("  sent 0xF1 login bundle (8 messages, counters 1..8)")
        if reply:
            f2_seen = b"\x00\x00\x00\xf2" in reply
            print(f"  server reply: {len(reply)} bytes (0xF2 received: {f2_seen})")
        else:
            print("  no immediate reply (server may be silent until polled)")
    finally:
        mc.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aoeo_market.live_probe",
        description="Attempt a real connection to the Celeste game backend.",
    )
    add_login_args(p)
    p.add_argument(
        "--game",
        action="store_true",
        help="also attempt the TCP 1510 login handshake (best-effort)",
    )
    args = p.parse_args(argv)
    args.local_ip = resolve_local_ip(p, args.local_ip)
    try:
        device_hash = parse_device_hash(args.device_hash)
        xlive_crc32 = resolve_xlive_crc(args.xlive_crc)
    except (ValueError, auth.XliveManifestError) as exc:
        p.error(str(exc))
    return probe(
        local_ip=args.local_ip,
        mail=args.email,
        password=args.password,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        try_game=args.game,
        device_hash=device_hash,
        xlive_crc32=xlive_crc32,
    )


if __name__ == "__main__":
    sys.exit(main())
