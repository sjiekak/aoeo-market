"""Celeste Network (TCP 4564) login — how ``Spartan.exe`` authenticates.

The launcher's own account API (``wss://prod.projectceleste.com:4513/``) is a
*separate* TLS WebSocket used only by the launcher GUI (friends, news, account
settings).  The game authenticates through a plaintext TCP service on port 4564
implemented inside Project Celeste's ``xlive.dll`` ("Celeste Network").

Frame format (all multi-byte integers little-endian)::

    [4B packet id] [4B total length — INCLUDING these 8 header bytes] [body]

Observed login exchange, identical across the three pre-upgrade captures
analysed (``capture_aoeo_login_market_query_towards_server.pcapng`` 2026-08-10,
``capture_aoeo_only.pcapng`` 2026-08-13,
``capture_aoeo_login_separate_user_email_password.pcapng`` 2026-08-17) and
re-confirmed after the 2026-09-03 server maintenance
(``capture_aoeo_login_after_server_upgrade_two_attempts_with_official_client
.pcapng``, 2026-09-04 — the request *format*, the response *format*, the
version constant 2018 and the manifest reply are all unchanged):

1. client -> server  packet 1: email + password (plaintext, length-prefixed)
2. server -> client  packet 1: xuid + profile name + 32-char session token
3. client -> server  packet 2: xuid + token + 0x2b  (session register)
4. server -> client  packet 2: a ~135 KB JSON game-file manifest

A rejected login gets a packet-1 response whose body is 67 bytes of
``[8B zeros][32B spaces][02 00][26B zeros]`` — the success layout with the
status byte ``0a`` replaced by ``00`` and every session field zeroed — and
the server closes the connection (FIN) immediately after.  :meth:`login`
raises :class:`LoginRejected` on this instead of registering a bogus session.

The game also sends, on the same connection:

* packet 3   — ``xuid + token + 0x10 + u32`` keepalive/status (~22 s after
  login, in both the 2026-08-13 and 2026-08-17 captures); the server answers
  ``xuid + 32 spaces + 0x11 0x01``.
* packet 4/5 — ``xuid + token + 0x0e`` friend-list queries; the server answers
  ``xuid + 32 spaces + 0x0f 0x15 + u32 len + {"friend-results": …}``.
* packet 7   — re-login: ``xuid + token + [0x01 + version 2018 + email +
  password + install signature + device hash]`` (same credentials block as
  packet 1, prefixed by the session); the server answers with the same layout
  as the packet-1 response.

The returned ``xuid`` / ``username`` / ``token`` are then fed into the 1510
game-service login (``client.MarketClient.login``, channel 0x0101 opcode 0xF1);
the game repeats the same 0xF1 login on the 1500 lobby/realm service.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import urllib.request
from dataclasses import dataclass
from typing import Self

from .constants import CELESTE_NETWORK_HOST, CELESTE_NETWORK_PORT

# Client/protocol version constant. Present in every captured login request
# (both the packet-1 and packet-7 forms) across all three pre-upgrade captures
# analysed: ``capture_aoeo_login_market_query_towards_server.pcapng``
# (2026-08-10), ``capture_aoeo_only.pcapng`` (2026-08-13) and
# ``capture_aoeo_login_separate_user_email_password.pcapng`` (2026-08-17) —
# and unchanged in the official client's post-upgrade login
# (``capture_aoeo_login_after_server_upgrade_two_attempts_with_official_client
# .pcapng``, 2026-09-04).
PROTOCOL_VERSION = 2018

# The 12-byte install-signature field of the login request has the layout
# ``[4B CRC-32 of the installed xlive.dll (LE)][4B local IPv4][4B 0x40 00 00
# 00]``.  Two of the three groups are constant in shape:
#
# * bytes 4..8  — the caller's local IPv4 address, network byte order
#   (192.168.0.17 in the 2026-08-13/17 and 2026-09-04 captures, 192.168.1.37
#   in the 2026-08-10 capture; both match the packet source address);
# * bytes 8..12 — ``40 00 00 00`` in every capture.
#
# The first group is the **CRC-32 of the installed xlive.dll**, little-endian.
# Proven 2026-09-04: the Celeste manifest
# (``https://downloads.projectceleste.com/game_files/xlive.json``) publishes
# ``"CRC32": 157393292`` = ``0x0961A18C`` for the current xlive.dll
# (1.0.0.106, built 2026-09-03); its little-endian bytes are ``8c a1 61 09``
# — byte-for-byte what the updated official client sent.  The pre-upgrade
# client sent ``f6 9b 99 1a`` (LE of ``0x1A999BF6``, the CRC-32 of the
# previous xlive.dll build) and machine A sent ``45 8e 0d 1e`` (LE of
# ``0x1E0D8E45``, the CRC-32 of the xlive.dll build it had on 2026-08-10):
# the field is per xlive.dll **build**, not per machine.  The 2026-09-03
# maintenance started rejecting stale CRCs, so the value must always match
# the currently installed xlive.dll — :func:`fetch_xlive_crc32` reads it from
# the published manifest at :data:`XLIVE_MANIFEST_URL`.
XLIVE_MANIFEST_URL = "https://downloads.projectceleste.com/game_files/xlive.json"
INSTALL_SIGNATURE_SUFFIX = b"\x40\x00\x00\x00"  # constant 4th group of the 12-byte signature


def xlive_crc32_from_manifest(data: bytes) -> bytes:
    """Parse an xlive.json manifest body and return its CRC32 as 4 LE bytes.

    The manifest describes the current xlive.dll build; its ``CRC32`` field
    is the standard CRC-32 of the DLL file, and the login request's install
    signature carries exactly those 4 bytes, little-endian.
    """
    doc = json.loads(data.decode("utf-8-sig"))
    crc = doc.get("CRC32")
    if not isinstance(crc, int) or not 0 <= crc <= 0xFFFFFFFF:
        raise ValueError(f"xlive manifest has no usable CRC32 field: {crc!r}")
    return crc.to_bytes(4, "little")


class XliveManifestError(RuntimeError):
    """The Celeste xlive.json manifest could not be fetched or parsed.

    Raised by :func:`fetch_xlive_crc32` (network or content failures) and
    re-raised with an ``--xlive-crc`` hint by
    :func:`aoeo_market.cli_args.resolve_xlive_crc`, which stops the live
    commands instead of guessing with a stale captured CRC.
    """


def fetch_xlive_crc32(url: str = XLIVE_MANIFEST_URL, timeout: float = 10.0) -> bytes:
    """Download the Celeste xlive.json manifest and return the CRC-32 of the
    current xlive.dll as 4 little-endian bytes (the install-signature prefix).

    Raises :class:`XliveManifestError` when the manifest cannot be fetched or
    does not contain a usable ``CRC32`` field.  Callers that prefer a
    captured value over a network round-trip can pass the 4 bytes directly.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aoeo-market"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return xlive_crc32_from_manifest(resp.read())
    except (OSError, ValueError) as exc:
        raise XliveManifestError(f"could not fetch or parse the xlive manifest {url}: {exc}") from exc


def build_install_signature(ip: str, xlive_crc32: bytes) -> bytes:
    """Build the 12-byte install-signature field for a given local IPv4 address.

    Layout: ``[4B CRC-32 of the installed xlive.dll (LE)][local IPv4 network
    order][0x40 00 00 00]``.  ``xlive_crc32`` is required: it must be the
    CRC-32 of the xlive.dll the server currently ships, read from the
    manifest by :func:`fetch_xlive_crc32`; only the address is computed.
    """
    if len(xlive_crc32) != 4:
        raise ValueError("xlive_crc32 must be exactly 4 bytes")
    return xlive_crc32 + ipaddress.IPv4Address(ip).packed + INSTALL_SIGNATURE_SUFFIX


# 64-hex-char (32-byte) machine/install fingerprint.  Stable per install and
# independent of the account: the 2026-08-13 and 2026-08-17 captures come from
# the same machine but different accounts, and both send the value below; the
# 2026-08-10 capture from another machine (same account as 2026-08-13) sends a
# different value (kept with the other captured references in
# ``tests/auth_ref.py``).
#
# The 2026-09-03 server maintenance changed the value: the updated official
# client on machine B now sends ``1cb498f3…`` instead of the pre-upgrade
# ``1257dc20…``, and the server rejects the pre-upgrade value (empty-session
# rejection, verified live 2026-09-04).  Unlike the install-signature CRC
# (which is derived from the live manifest), this is never defaulted: callers
# pass the value for their machine explicitly.
DEVICE_HASH = "1cb498f3c8c76b0a654698f36dec7a05d16a879f6d4f41c67e1b507c63c1106f"  # machine B (post-upgrade)

_HEADER = struct.Struct("<II")


@dataclass(frozen=True)
class GameSession:
    """Credentials the 1510 game service expects in its 0xF1 login frame."""

    xuid: int
    username: str
    token: str
    external_ip: str = ""
    extra: str = ""  # 4th response field; varies ('None' vs 'Summer')


class LoginRejected(RuntimeError):
    """The 4564 service answered the login packet but issued no session.

    Raised when the packet-1 login response carries no session token — the
    server's rejection frame for wrong credentials or a rejected device
    fingerprint — after which the server closes the connection.  The 1510
    login would fail with the same all-zero session, so the register step is
    never sent.
    """


class ProtocolError(RuntimeError):
    """Malformed bytes from the Celeste Network (4564) service.

    Raised when a server packet does not fit the documented layout — a wrong
    packet id, a bogus length field, or a login response body that cannot be
    parsed.  Argument-validation failures (bad caller input) keep raising
    :class:`ValueError`; this type is reserved for wire-format errors, so
    callers can tell the two apart.
    """


def _len_prefixed(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _login_body(mail: str, password: str, local_ip: str, device_hash: str, xlive_crc32: bytes) -> bytes:
    """The email+password block of the packet-1 login request."""
    return (
        b"\x00" * 40
        + b"\x01"
        + struct.pack("<I", PROTOCOL_VERSION)
        + _len_prefixed(mail.encode("utf-8"))
        + _len_prefixed(password.encode("utf-8"))
        + build_install_signature(local_ip, xlive_crc32)
        + device_hash.encode("ascii")
    )


def build_login_request(
    mail: str,
    password: str,
    local_ip: str,
    device_hash: str,
    xlive_crc32: bytes,
) -> bytes:
    """Build the packet-1 login request body + header for the 4564 service.

    ``device_hash`` is the per-install constant (:data:`DEVICE_HASH`) and
    ``xlive_crc32`` is the little-endian CRC-32 of the installed xlive.dll
    (:func:`fetch_xlive_crc32`); both are required, the caller chooses which
    values to replay.
    """
    if len(device_hash) != 64:
        raise ValueError("device_hash must be 64 hexadecimal characters")
    body = _login_body(mail, password, local_ip, device_hash, xlive_crc32)
    return _HEADER.pack(1, 8 + len(body)) + body


def build_session_register(xuid: int, token: str) -> bytes:
    """Build the packet-2 session-register body + header."""
    body = struct.pack("<q", xuid) + token.encode("ascii") + b"\x2b"
    return _HEADER.pack(2, 8 + len(body)) + body


def parse_login_response(body: bytes) -> GameSession:
    """Parse the packet-1 login response body.

    Layout (123/132 bytes in the captures)::

        [8B context][32B 0x20 padding][02 0a][8B xuid]
        [4B len][profileName][4B len][token32][4B len][externalIp]
        [4B len][extra][0x01]

    The 4th field is *not* a constant: it is ``"None"`` in the 2026-08-10
    capture and ``"Summer"`` in both 2026-08-13/17 captures.  The external IP
    is the server's view of the client's public address and varies between
    sessions.

    A *rejected* login (wrong credentials or rejected fingerprint) comes back
    as a 67-byte body: ``[8B zeros][32B spaces][02 00][26B zeros]`` — the
    success layout with the status byte zeroed and every session field empty
    — followed by the server closing the connection.  It parses to a session
    with ``xuid == 0`` and empty fields, which callers must treat as a
    rejection (see :class:`LoginRejected`).

    Raises :class:`ProtocolError` when the body does not fit the layout.
    """
    off = 8 + 32 + 2
    if off + 8 > len(body):
        raise ProtocolError("login response too short")
    (xuid,) = struct.unpack("<q", body[off : off + 8])
    off += 8

    fields: list[str] = []
    while off + 4 <= len(body):
        (n,) = struct.unpack("<I", body[off : off + 4])
        off += 4
        if n > 1024 or off + n > len(body):
            break
        fields.append(body[off : off + n].decode("utf-8", "replace"))
        off += n

    if len(fields) < 2:
        raise ProtocolError(f"unexpected login response fields: {fields!r}")

    return GameSession(
        xuid=xuid,
        username=fields[0],
        token=fields[1],
        external_ip=fields[2] if len(fields) > 2 else "",
        extra=fields[3] if len(fields) > 3 else "",
    )


class CelesteNetworkClient:
    """Minimal client for the TCP 4564 account/login service."""

    def __init__(
        self,
        host: str = CELESTE_NETWORK_HOST,
        port: int = CELESTE_NETWORK_PORT,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.manifest_received = False

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError(f"connection closed while reading response ({len(data)} of {n} bytes received)")
            data += chunk
        return data

    def _recv_packet(self) -> tuple[int, bytes]:
        header = self._recv_exact(8)
        pid, total = _HEADER.unpack(header)
        if total < 8:
            raise ProtocolError(f"invalid packet length {total}")
        return pid, self._recv_exact(total - 8)

    def login(
        self,
        mail: str,
        password: str,
        local_ip: str,
        device_hash: str,
        xlive_crc32: bytes,
    ) -> GameSession:
        """Authenticate with email + password and return the game session.

        ``device_hash`` is the per-install fingerprint (:data:`DEVICE_HASH`)
        and ``xlive_crc32`` is the little-endian CRC-32 of the installed
        xlive.dll (:func:`fetch_xlive_crc32`); both must be supplied by the
        caller — no defaults are inferred.

        Performs the 4564 exchange: login request, login response, session
        register, then drains the (large) manifest reply. The manifest is not
        used by callers, so its drain is best-effort: the server may close the
        connection right after the register, but the session token has already
        been issued and remains what the 1510 login needs. ``manifest_received``
        records whether the drain completed.

        A rejection (empty session in the packet-1 response — wrong credentials
        or a rejected device fingerprint) raises :class:`LoginRejected` before
        the register is sent; the server closes the connection on its own in
        that case.
        """
        self.connect()
        try:
            self._sock.sendall(build_login_request(mail, password, local_ip, device_hash, xlive_crc32))
            pid, body = self._recv_packet()
            if pid != 1:
                raise ProtocolError(f"unexpected login response packet id {pid}")

            session = parse_login_response(body)
            if session.xuid == 0 or not session.token:
                raise LoginRejected("server rejected the login (no session token issued) — wrong credentials or a rejected device fingerprint")

            self._sock.sendall(build_session_register(session.xuid, session.token))
            # packet 2: the ~135 KB game-file manifest; not used by callers.
            try:
                self._recv_packet()
                self.manifest_received = True
            except (OSError, ProtocolError):
                self.close()

            return session
        except Exception:
            self.close()
            raise
