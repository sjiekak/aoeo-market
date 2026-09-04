"""Test-only reference data and builders for the 4564 login.

The production client derives the install-signature CRC-32 from the live
Celeste manifest (``aoeo_market.auth.fetch_xlive_crc32``) and uses the
current ``DEVICE_HASH`` constant, so none of these captured values are used
by production code — per CONTRIBUTING.md, symbols that only tests use live
here instead of in ``aoeo_market.auth``.

Provenance (see docs/authentication.md):

* ``XLIVE_CRC32``               — ``8ca16109``, LE bytes of ``0x0961A18C``,
  the CRC-32 of xlive.dll 1.0.0.106 (shipped by the 2026-09-03 maintenance;
  the official client's 2026-09-04 login).
* ``XLIVE_CRC32_PRE_UPGRADE``  — ``f69b991a``, LE bytes of ``0x1A999BF6``,
  the CRC-32 of the pre-upgrade xlive.dll build (rejected since 2026-09-03).
* ``XLIVE_CRC32_ALT``          — ``458e0d1e``, LE bytes of ``0x1E0D8E45``,
  the CRC-32 of the xlive.dll build machine A had on 2026-08-10.
* ``DEVICE_HASH_PRE_UPGRADE``  — machine B's pre-upgrade device hash
  (rejected since 2026-09-03; the current value is ``auth.DEVICE_HASH``).
* ``DEVICE_HASH_ALT``          — machine A's device hash (2026-08-10).

``build_relogin_request`` rebuilds the packet-7 re-login (observed in the
2026-08-10 and 2026-08-17 captures): ``xuid + token + 0x01 + version 2018 +
email + password + install signature + device hash``.  The game sends it
right before the 1510/1500 logins; the market client never does, so the
builder is kept here for the capture-replay tests.
"""

from __future__ import annotations

import struct

from aoeo_market.auth import PROTOCOL_VERSION, build_install_signature

XLIVE_CRC32 = bytes.fromhex("8ca16109")
XLIVE_CRC32_PRE_UPGRADE = bytes.fromhex("f69b991a")
XLIVE_CRC32_ALT = bytes.fromhex("458e0d1e")

DEVICE_HASH_PRE_UPGRADE = "1257dc20e79151e29b7b2476a06de0df3e3952d240f94af2a235e468d971eb49"
DEVICE_HASH_ALT = "01b41e3557182b068efd169eb446b3eef517b209aad51b378ad88d2258035a18"


def build_relogin_request(
    mail: str,
    password: str,
    local_ip: str,
    xuid: int,
    token: str,
    *,
    device_hash: str,
    xlive_crc32: bytes,
) -> bytes:
    """Build the packet-7 re-login body + header for the 4564 service."""
    if len(device_hash) != 64:
        raise ValueError("device_hash must be 64 hexadecimal characters")

    def _len_prefixed(data: bytes) -> bytes:
        return struct.pack("<I", len(data)) + data

    body = (
        struct.pack("<q", xuid)
        + token.encode("ascii")
        + b"\x01"
        + struct.pack("<I", PROTOCOL_VERSION)
        + _len_prefixed(mail.encode("utf-8"))
        + _len_prefixed(password.encode("utf-8"))
        + build_install_signature(local_ip, xlive_crc32)
        + device_hash.encode("ascii")
    )
    return struct.pack("<II", 7, 8 + len(body)) + body
