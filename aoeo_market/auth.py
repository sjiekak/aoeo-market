"""Celeste Network (TCP 4564) login — how ``Spartan.exe`` authenticates.

The launcher's own account API (``wss://prod.projectceleste.com:4513/``) is a
*separate* TLS WebSocket used only by the launcher GUI (friends, news, account
settings).  The game authenticates through a plaintext TCP service on port 4564
implemented inside Project Celeste's ``xlive.dll`` ("Celeste Network").

Frame format (all multi-byte integers little-endian)::

    [4B packet id] [4B total length — INCLUDING these 8 header bytes] [body]

Observed login exchange
(capture ``capture_aoeo_login_market_query_towards_server.pcapng``):

1. client -> server  packet 1: email + password (plaintext, length-prefixed)
2. server -> client  packet 1: xuid + profile name + 32-char session token
3. client -> server  packet 2: xuid + token + 0x2b  (session register)
4. server -> client  packet 2: a ~135 KB JSON game-file manifest

The returned ``xuid`` / ``username`` / ``token`` are then fed into the 1510
game-service login (``client.MarketClient.login``, channel 0x0101 opcode 0xF1).
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from .constants import CELESTE_NETWORK_HOST, CELESTE_NETWORK_PORT

# The 12 opaque bytes between the password and the device hash in the observed
# login request. Reconstructed as: 0x45 | two 32-bit LE words (public/local
# IP-ish values) | 3 zero bytes. They are stable for a given install, so we
# replay the captured bytes rather than attempting to regenerate them.
_LOGIN_TAIL = bytes.fromhex("458e0d1ec0a8012540000000")

# 64-hex-char (32-byte) machine/install fingerprint from the capture.
DEVICE_HASH = (
    "01b41e3557182b068efd169eb446b3eef517b209aad51b378ad88d2258035a18"
)

_HEADER = struct.Struct("<II")


@dataclass(frozen=True)
class GameSession:
    """Credentials the 1510 game service expects in its 0xF1 login frame."""

    xuid: int
    username: str
    token: str
    external_ip: str = ""


def _len_prefixed(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def build_login_request(
    mail: str, password: str, device_hash: str = DEVICE_HASH
) -> bytes:
    """Build the packet-1 login request body + header for the 4564 service."""
    if len(device_hash) != 64:
        raise ValueError("device_hash must be 64 hexadecimal characters")
    body = (
        b"\x00" * 40
        + b"\x01"
        + struct.pack("<I", 2018)  # client/protocol version constant
        + _len_prefixed(mail.encode("utf-8"))
        + _len_prefixed(password.encode("utf-8"))
        + _LOGIN_TAIL
        + device_hash.encode("ascii")
    )
    return _HEADER.pack(1, 8 + len(body)) + body


def build_session_register(xuid: int, token: str) -> bytes:
    """Build the packet-2 session-register body + header."""
    body = struct.pack("<q", xuid) + token.encode("ascii") + b"\x2b"
    return _HEADER.pack(2, 8 + len(body)) + body


def parse_login_response(body: bytes) -> GameSession:
    """Parse the packet-1 login response body.

    Layout (122 bytes in the capture)::

        [8B context][32B 0x20 padding][02 0a][8B xuid]
        [4B len][profileName][4B len][token32][4B len][externalIp]
        [4B len]["None"][0x01]
    """
    off = 8 + 32 + 2
    if off + 8 > len(body):
        raise ValueError("login response too short")
    (xuid,) = struct.unpack("<q", body[off:off + 8])
    off += 8

    fields: list[str] = []
    while off + 4 <= len(body):
        (n,) = struct.unpack("<I", body[off:off + 4])
        off += 4
        if n > 1024 or off + n > len(body):
            break
        fields.append(body[off:off + n].decode("utf-8", "replace"))
        off += n

    if len(fields) < 2:
        raise ValueError(f"unexpected login response fields: {fields!r}")

    return GameSession(
        xuid=xuid,
        username=fields[0],
        token=fields[1],
        external_ip=fields[2] if len(fields) > 2 else "",
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

    def __enter__(self) -> CelesteNetworkClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection(
            (self.host, self.port), self.timeout
        )
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
                raise ConnectionError(
                    "connection closed while reading response "
                    f"({len(data)} of {n} bytes received)"
                )
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
        device_hash: str = DEVICE_HASH,
    ) -> GameSession:
        """Authenticate with email + password and return the game session.

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
            self._sock.sendall(
                build_login_request(mail, password, device_hash)
            )
            pid, body = self._recv_packet()
            if pid != 1:
                raise RuntimeError(
                    f"unexpected login response packet id {pid}"
                )

            session = parse_login_response(body)

            self._sock.sendall(
                build_session_register(session.xuid, session.token)
            )
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
