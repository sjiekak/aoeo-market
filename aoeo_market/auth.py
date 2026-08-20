"""Celeste Network (TCP 4564) login — how ``Spartan.exe`` authenticates.

The launcher's own account API (``wss://prod.projectceleste.com:4513/``) is a
*separate* TLS WebSocket used only by the launcher GUI (friends, news, account
settings).  The game authenticates through a plaintext TCP service on port 4564
implemented inside Project Celeste's ``xlive.dll`` ("Celeste Network").

Frame format (all multi-byte integers little-endian)::

    [4B packet id] [4B total length — INCLUDING these 8 header bytes] [body]

Observed login exchange, identical across the three captures analysed
(``capture_aoeo_login_market_query_towards_server.pcapng`` 2026-08-10,
``capture_aoeo_only.pcapng`` 2026-08-13,
``capture_aoeo_login_separate_user_email_password.pcapng`` 2026-08-17):

1. client -> server  packet 1: email + password (plaintext, length-prefixed)
2. server -> client  packet 1: xuid + profile name + 32-char session token
3. client -> server  packet 2: xuid + token + 0x2b  (session register)
4. server -> client  packet 2: a ~135 KB JSON game-file manifest

The game also sends, on the same connection:

* packet 3   — ``xuid + token + 0x10 + u32`` keepalive/status (~22 s after
  login, in both the 2026-08-13 and 2026-08-17 captures); the server answers
  ``xuid + 32 spaces + 0x11 0x01``.
* packet 4/5 — ``xuid + token + 0x0e`` friend-list queries; the server answers
  ``xuid + 32 spaces + 0x0f 0x15 + u32 len + {"friend-results": …}``.
* packet 7   — re-login: ``xuid + token + [0x01 + version 2018 + email +
  password + tail + device hash]`` (same credentials block as packet 1,
  prefixed by the session); the server answers with the same layout as the
  packet-1 response.

The returned ``xuid`` / ``username`` / ``token`` are then fed into the 1510
game-service login (``client.MarketClient.login``, channel 0x0101 opcode 0xF1);
the game repeats the same 0xF1 login on the 1500 lobby/realm service.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from dataclasses import dataclass
from typing import Self

from .constants import CELESTE_NETWORK_HOST, CELESTE_NETWORK_PORT

# Client/protocol version constant. Present in every captured login request
# (both the packet-1 and packet-7 forms) across all three captures analysed:
# ``capture_aoeo_login_market_query_towards_server.pcapng`` (2026-08-10),
# ``capture_aoeo_only.pcapng`` (2026-08-13) and
# ``capture_aoeo_login_separate_user_email_password.pcapng`` (2026-08-17).
PROTOCOL_VERSION = 2018

# The 12-byte login tail has the layout ``[4B opaque][4B local IPv4][4B 0x40
# 00 00 00]``.  Two of the three groups are genuinely constant:
#
# * bytes 4..8  — the caller's local IPv4 address, network byte order
#   (192.168.0.17 in the 2026-08-13/17 captures, 192.168.1.37 in the
#   2026-08-10 capture; both match the packet source address);
# * bytes 8..12 — ``40 00 00 00`` in every capture.
#
# The first group is NOT constant: it is 4 opaque bytes that are stable per
# install (per machine) but differ between machines.  The 2026-08-10 capture
# (machine A) used ``45 8e 0d 1e`` — the earlier code mistakenly read this as
# a constant ``0x45`` prefix — while both 2026-08-13/17 captures (machine B,
# different days, different accounts) used ``f6 9b 99 1a``.  Pass the right
# value for the machine the client runs on.
LOGIN_TAIL_OPAQUE = bytes.fromhex("f69b991a")  # machine B (2026-08-13/17)
LOGIN_TAIL_OPAQUE_ALT = bytes.fromhex("458e0d1e")  # machine A (2026-08-10)
LOGIN_TAIL_SUFFIX = b"\x40\x00\x00\x00"  # constant across all captures


def build_login_tail(ip: str, opaque: bytes) -> bytes:
    """Build the 12-byte login tail for a given local IPv4 address.

    Layout: ``[4B opaque per-install][local IPv4 network order][0x40 00 00
    00]``.  ``opaque`` is required: the 4 opaque bytes are stable for a given
    install but differ between machines, so the caller must supply them
    (:data:`LOGIN_TAIL_OPAQUE` / :data:`LOGIN_TAIL_OPAQUE_ALT`); only the
    address is computed.
    """
    if len(opaque) != 4:
        raise ValueError("opaque must be exactly 4 bytes")
    return opaque + ipaddress.IPv4Address(ip).packed + LOGIN_TAIL_SUFFIX


# 64-hex-char (32-byte) machine/install fingerprint.  Stable per install and
# independent of the account: the 2026-08-13 and 2026-08-17 captures come from
# the same machine but different accounts, and both send the value below; the
# 2026-08-10 capture from another machine (same account as 2026-08-13) sends
# the ALT value.  Like the tail opaque bytes, this is never defaulted: callers
# pass the value for their machine explicitly.
DEVICE_HASH = "1257dc20e79151e29b7b2476a06de0df3e3952d240f94af2a235e468d971eb49"
DEVICE_HASH_ALT = "01b41e3557182b068efd169eb446b3eef517b209aad51b378ad88d2258035a18"

_HEADER = struct.Struct("<II")


@dataclass(frozen=True)
class GameSession:
    """Credentials the 1510 game service expects in its 0xF1 login frame."""

    xuid: int
    username: str
    token: str
    external_ip: str = ""
    extra: str = ""  # 4th response field; varies ('None' vs 'Summer')


def _len_prefixed(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _login_body(mail: str, password: str, local_ip: str, device_hash: str, opaque: bytes) -> bytes:
    """The email+password block shared by the packet-1 and packet-7 logins."""
    return (
        b"\x00" * 40
        + b"\x01"
        + struct.pack("<I", PROTOCOL_VERSION)
        + _len_prefixed(mail.encode("utf-8"))
        + _len_prefixed(password.encode("utf-8"))
        + build_login_tail(local_ip, opaque)
        + device_hash.encode("ascii")
    )


def build_login_request(
    mail: str,
    password: str,
    local_ip: str,
    device_hash: str,
    opaque: bytes,
) -> bytes:
    """Build the packet-1 login request body + header for the 4564 service.

    ``device_hash`` and ``opaque`` are the per-install constants
    (:data:`DEVICE_HASH` / :data:`LOGIN_TAIL_OPAQUE`) and are required: the
    caller chooses which machine's values to replay.
    """
    if len(device_hash) != 64:
        raise ValueError("device_hash must be 64 hexadecimal characters")
    body = _login_body(mail, password, local_ip, device_hash, opaque)
    return _HEADER.pack(1, 8 + len(body)) + body


def build_session_register(xuid: int, token: str) -> bytes:
    """Build the packet-2 session-register body + header."""
    body = struct.pack("<q", xuid) + token.encode("ascii") + b"\x2b"
    return _HEADER.pack(2, 8 + len(body)) + body


def build_relogin_request(
    mail: str,
    password: str,
    local_ip: str,
    xuid: int,
    token: str,
    device_hash: str,
    opaque: bytes,
) -> bytes:
    """Build the packet-7 re-login body + header for the 4564 service.

    Observed in the 2026-08-10 and 2026-08-17 captures: after the initial
    login the game re-authenticates on the same connection with a packet that
    carries ``xuid + token`` first and then the same email/password block as
    packet 1 (``0x01``, version 2018, lengths, tail, device hash).  The
    server answers with a packet-7 response identical in layout to the
    packet-1 response (with the 8 leading zero bytes replaced by the xuid).

    ``device_hash`` and ``opaque`` are the per-install constants and are
    required, exactly as in :func:`build_login_request`.
    """
    if len(device_hash) != 64:
        raise ValueError("device_hash must be 64 hexadecimal characters")
    body = (
        struct.pack("<q", xuid)
        + token.encode("ascii")
        + b"\x01"
        + struct.pack("<I", PROTOCOL_VERSION)
        + _len_prefixed(mail.encode("utf-8"))
        + _len_prefixed(password.encode("utf-8"))
        + build_login_tail(local_ip, opaque)
        + device_hash.encode("ascii")
    )
    return _HEADER.pack(7, 8 + len(body)) + body


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
    """
    off = 8 + 32 + 2
    if off + 8 > len(body):
        raise ValueError("login response too short")
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
        raise ValueError(f"unexpected login response fields: {fields!r}")

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
            raise ValueError(f"invalid packet length {total}")
        return pid, self._recv_exact(total - 8)

    def login(
        self,
        mail: str,
        password: str,
        local_ip: str,
        device_hash: str,
        opaque: bytes,
    ) -> GameSession:
        """Authenticate with email + password and return the game session.

        ``device_hash`` and ``opaque`` are the per-install constants
        (:data:`DEVICE_HASH` / :data:`LOGIN_TAIL_OPAQUE`) and must be supplied
        by the caller — no captured defaults are inferred.

        Performs the 4564 exchange: login request, login response, session
        register, then drains the (large) manifest reply. The manifest is not
        used by callers, so its drain is best-effort: the server may close the
        connection right after the register (e.g. when it rejects the replayed
        device fingerprint), but the session token has already been issued and
        remains what the 1510 login needs. ``manifest_received`` records
        whether the drain completed.
        """
        self.connect()
        try:
            self._sock.sendall(build_login_request(mail, password, local_ip, device_hash, opaque))
            pid, body = self._recv_packet()
            if pid != 1:
                raise RuntimeError(f"unexpected login response packet id {pid}")

            session = parse_login_response(body)

            self._sock.sendall(build_session_register(session.xuid, session.token))
            # packet 2: the ~135 KB game-file manifest; not used by callers.
            try:
                self._recv_packet()
                self.manifest_received = True
            except (OSError, ValueError):
                self.close()

            return session
        except Exception:
            self.close()
            raise
