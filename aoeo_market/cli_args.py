"""Shared argparse declarations for the command-line entry points.

The Celeste Network login arguments (``--local-ip``, ``--email``,
``--password``, ``--host``, ``--port``, ``--timeout``, ``--device-hash``,
``--xlive-crc``) are declared here once and attached by both
:mod:`aoeo_market.cli` (``probe`` and ``fetch``) and
:mod:`aoeo_market.live_probe`.  The per-install ``--device-hash`` default is
the captured constant from :mod:`aoeo_market.auth`; ``--xlive-crc`` (the
4-byte CRC-32 of the installed xlive.dll) defaults to the value published in
the live Celeste manifest (:func:`aoeo_market.auth.fetch_xlive_crc32`), so
the commands never infer a stale fingerprint.

``--local-ip`` is optional: when it is omitted, the locally detected IPv4
address (:func:`detect_local_ip`) is used as the default, exactly the value
the kernel would source packets to the Celeste Network host with.
"""

from __future__ import annotations

import argparse
import socket

from . import auth
from .constants import (
    CELESTE_NETWORK_HOST,
    CELESTE_NETWORK_PORT,
    DEFAULT_CONNECT_TIMEOUT,
)


def detect_local_ip(
    host: str = CELESTE_NETWORK_HOST,
    port: int = CELESTE_NETWORK_PORT,
) -> str:
    """Return this machine's local IPv4 address as chosen by the kernel route.

    Connects a UDP socket to ``host:port`` — nothing is sent — and reads the
    local address the kernel picks as the packet source: the same IPv4 the
    game embeds in the login install signature.  Raises :class:`OSError` when no usable
    IPv4 route exists (e.g. the machine is offline), so callers can fall back
    to an explicit ``--local-ip``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]


def resolve_local_ip(parser: argparse.ArgumentParser, value: str | None) -> str:
    """Return the explicit ``--local-ip`` *value*, or the locally detected default.

    Reports a parser error (exit status 2) when no value was given and the
    address cannot be detected, e.g. because the machine has no active IPv4
    route.
    """
    if value:
        return value
    try:
        return detect_local_ip()
    except OSError as exc:
        parser.error(f"could not auto-detect the local IPv4 address ({exc}); pass --local-ip <your-ip> explicitly")


def add_login_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared Celeste Network login/connection arguments to ``parser``."""
    parser.add_argument(
        "--local-ip",
        help="your local IPv4 address (embedded in the install signature; auto-detected when omitted)",
    )
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
        "--xlive-crc",
        default=None,
        help="4-byte hex CRC-32 of the installed xlive.dll (fetched from the live Celeste manifest when omitted)",
    )


def parse_device_hash(value: str) -> str:
    """Validate a ``--device-hash`` value and return it."""
    if len(value) != 64:
        raise ValueError("--device-hash must be 64 hexadecimal characters")
    return value


def parse_xlive_crc(value: str) -> bytes:
    """Convert a ``--xlive-crc`` hex string to its 4 little-endian bytes."""
    try:
        crc = bytes.fromhex(value)
    except ValueError:
        raise ValueError("--xlive-crc must be hex") from None
    if len(crc) != 4:
        raise ValueError("--xlive-crc must be exactly 4 bytes of hex")
    return crc


def resolve_xlive_crc(value: str | None) -> bytes:
    """Return the 4-byte xlive.dll CRC-32 for the login install signature.

    An explicit ``--xlive-crc`` hex value wins; otherwise the live Celeste
    manifest (:data:`auth.XLIVE_MANIFEST_URL`) is fetched so the signature
    always matches the currently shipped xlive.dll.  A fetch failure raises
    :class:`aoeo_market.auth.XliveManifestError` — the command stops instead
    of guessing with a stale captured CRC (a stale CRC is indistinguishable
    from a rejected login).
    """
    if value:
        return parse_xlive_crc(value)
    try:
        return auth.fetch_xlive_crc32()
    except auth.XliveManifestError as exc:
        raise auth.XliveManifestError(f"{exc}; pass --xlive-crc <4-byte-hex> explicitly to override") from exc
