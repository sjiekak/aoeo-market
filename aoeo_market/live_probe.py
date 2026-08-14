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
from .client import MarketClient, Session
from .constants import (
    CELESTE_NETWORK_HOST,
    CELESTE_NETWORK_PORT,
    DEFAULT_CONNECT_TIMEOUT,
)


def resolve_credentials(
    mail: str | None, password: str | None
) -> tuple[str, str]:
    """Return ``(email, password)`` from args, env, or a prompt."""
    if not mail:
        mail = os.environ.get("AOEO_EMAIL") or input("Email: ").strip()
    if not password:
        password = os.environ.get("AOEO_PASSWORD") or getpass.getpass(
            "Password: "
        )
    return mail, password


def probe(
    mail: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    try_game: bool = False,
) -> int:
    """Attempt a live login and return a process exit code (0 on success)."""
    host = host or CELESTE_NETWORK_HOST
    port = port or CELESTE_NETWORK_PORT
    mail, password = resolve_credentials(mail, password)

    print(f"Connecting to Celeste Network {host}:{port} ...", file=sys.stderr)
    cn = auth.CelesteNetworkClient(host=host, port=port, timeout=timeout)
    try:
        session = cn.login(mail, password)
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
    """Best-effort 1510 login handshake; the full ordering is still partial."""
    print(
        "\nAttempting TCP 1510 login handshake (best-effort)...",
        file=sys.stderr,
    )
    mc = MarketClient(connect_timeout=timeout)
    try:
        mc.connect()
        mc.login(
            Session(
                xuid=session.xuid,
                username=session.username,
                token=session.token,
            )
        )
        data = b""
        try:
            data = mc._recv_some()
        except Exception as exc:  # no immediate reply is expected
            print(f"  (no immediate reply: {exc!r})")
        print("  sent OP_LOGIN 0xF1")
        if data:
            print(f"  server reply: {data.hex()}")
        else:
            print(
                "  no immediate reply (login handshake is only partially "
                "reversed; see client.login)"
            )
    finally:
        mc.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aoeo_market.live_probe",
        description="Attempt a real connection to the Celeste game backend.",
    )
    p.add_argument("--email", help="account email (or $AOEO_EMAIL)")
    p.add_argument("--password", help="account password (or $AOEO_PASSWORD)")
    p.add_argument("--host", default=CELESTE_NETWORK_HOST)
    p.add_argument("--port", type=int, default=CELESTE_NETWORK_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    p.add_argument(
        "--game",
        action="store_true",
        help="also attempt the TCP 1510 login handshake (best-effort)",
    )
    args = p.parse_args(argv)
    return probe(
        mail=args.email,
        password=args.password,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        try_game=args.game,
    )


if __name__ == "__main__":
    sys.exit(main())
