"""Shared argparse declarations for the command-line entry points.

The Celeste Network login arguments (``--local-ip``, ``--email``,
``--password``, ``--host``, ``--port``, ``--timeout``, ``--device-hash``,
``--tail``) are declared here once and attached by both
:mod:`aoeo_market.cli` (``probe`` and ``fetch``) and
:mod:`aoeo_market.live_probe`.  The per-install ``--device-hash`` and
``--tail`` defaults are the captured constants from :mod:`aoeo_market.auth`,
set here so the commands themselves never infer them.
"""

from __future__ import annotations

import argparse

from . import auth
from .constants import (
    CELESTE_NETWORK_HOST,
    CELESTE_NETWORK_PORT,
    DEFAULT_CONNECT_TIMEOUT,
)


def add_login_args(
    parser: argparse.ArgumentParser,
    *,
    local_ip_help: str = "your local IPv4 address",
) -> None:
    """Add the shared Celeste Network login/connection arguments to ``parser``."""
    parser.add_argument("--local-ip", required=True, help=local_ip_help)
    parser.add_argument("--email", help="account email (or $AOEO_EMAIL)")
    parser.add_argument("--password", help="account password (or $AOEO_PASSWORD)")
    parser.add_argument(
        "--host",
        default=CELESTE_NETWORK_HOST,
        help=f"Celeste Network host (default {CELESTE_NETWORK_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=CELESTE_NETWORK_PORT,
        help=f"Celeste Network port (default {CELESTE_NETWORK_PORT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="socket timeout in seconds",
    )
    parser.add_argument(
        "--device-hash",
        default=auth.DEVICE_HASH,
        help="64-hex per-install fingerprint overriding the captured default",
    )
    parser.add_argument(
        "--tail",
        default=auth.LOGIN_TAIL_OPAQUE.hex(),
        help="4-byte hex per-install login tail overriding the captured default",
    )


def parse_device_hash(value: str) -> str:
    """Validate a ``--device-hash`` value and return it."""
    if len(value) != 64:
        raise ValueError("--device-hash must be 64 hexadecimal characters")
    return value


def parse_tail(value: str) -> bytes:
    """Convert a ``--tail`` hex string to its 4 opaque bytes."""
    try:
        tail = bytes.fromhex(value)
    except ValueError:
        raise ValueError("--tail must be hex") from None
    if len(tail) != 4:
        raise ValueError("--tail must be exactly 4 bytes of hex")
    return tail
