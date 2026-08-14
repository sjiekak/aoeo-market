"""Live headless market client for the Project Celeste game backend.

This speaks the TCP 1510 game-service protocol directly: it logs in with an
existing account session, then polls the marketplace browse and feeds snapshots
to a :class:`~aoeo_market.observer.MarketObserver`.

STATUS / WHAT YOU MUST SUPPLY
-----------------------------
Authentication is account-bound. To connect you need three values that identify
your session to the game server:

    * ``xuid``      - your account's 64-bit id.
    * ``username``  - your profile name.
    * ``token``     - the 32-char game session token.

``MarketClient.acquire_session(mail, password)`` obtains all three by logging
in over the plaintext "Celeste Network" service on TCP 4564, exactly the way
the game itself (``Spartan.exe`` via ``xlive.dll``) authenticates. See
:mod:`aoeo_market.auth`.

Alternatively you can extract them once from a real login you perform with the
official launcher (sniff your own 4564 login response, or the 1510 login frame
opcode 0xF1). Fine for a personal tool.

IMPORTANT OPERATIONAL NOTE
--------------------------
The backend may allow only one live session per account. If so, run EITHER the
game OR this client, not both at once, or you may get disconnected. A read-only
poller that logs in as its own (possibly dedicated) account is the clean setup.

The login *handshake* (the small control frames the server expects around the
0xF1 login frame) is only partially reversed from a single capture; the pieces
that are certain (framing, login field layout, market query, response parsing)
are implemented, and the handshake steps are marked TODO with the captured
reference bytes so they can be completed against a fresh capture.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import protocol as proto
from .constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    GAME_SERVER_HOST,
    GAME_SERVER_PORT,
)
from .market import parse_listings
from .observer import Event, MarketObserver


@dataclass
class Session:
    xuid: int
    username: str
    token: str


@dataclass
class MarketClient:
    server: str = GAME_SERVER_HOST
    port: int = GAME_SERVER_PORT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    observer: MarketObserver = field(default_factory=MarketObserver)

    _sock: socket.socket | None = field(default=None, init=False)
    _rx: bytes = field(default=b"", init=False)
    _seq: int = field(default=0x34, init=False)  # continues the counter series
    _ctx: bytes = field(default=b"\x00" * 8, init=False)

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        self._sock = socket.create_connection((self.server, self.port), self.connect_timeout)
        self._sock.settimeout(self.connect_timeout)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _send(self, channel: int, opcode: int, payload: bytes) -> None:
        assert self._sock is not None
        frame = proto.Frame(self._ctx, channel, opcode, payload).encode()
        self._sock.sendall(frame)

    def _recv_some(self) -> bytes:
        assert self._sock is not None
        return self._sock.recv(65536)

    # -- auth -------------------------------------------------------------
    def acquire_session(self, mail: str, password: str, local_ip: str) -> Session:
        """Log in over the Celeste Network (TCP 4564) and return a Session.

        Mirrors what the game does: send email + password in a plaintext login
        packet, read back ``xuid`` / profile name / 32-char token, then
        register the session. See :mod:`aoeo_market.auth`.
        """
        from . import auth

        cn = auth.CelesteNetworkClient()
        try:
            gs = cn.login(mail, password, local_ip)
        finally:
            cn.close()
        return Session(xuid=gs.xuid, username=gs.username, token=gs.token)

    def login(self, session: Session) -> None:
        """Perform the 1510 login handshake and authenticate the session."""
        payload = proto.build_login_payload(session.xuid, session.username, session.token)
        # TODO: the captured login used channel 0x0101, opcode 0xF1, and was
        # preceded/followed by a short control exchange (opcodes 0xf2/0xff/0xfe/
        # 0x91/0x92 seen in cap1). Complete that ordering from a fresh capture.
        self._send(proto.CH_LOGIN, proto.OP_LOGIN, payload)

    def ping(self) -> None:
        self._send(proto.CH_GAME, proto.OP_PING, proto.build_ping_payload(self._next_seq()))

    # -- market -----------------------------------------------------------
    def request_market(self, selectors: list[int] | None = None) -> None:
        """Send a market browse query. Default is a broad wildcard sweep."""
        if selectors is None:
            selectors = [proto.WILDCARD] * 6
        body = proto.build_market_query_payload(self._next_seq(), selectors)
        self._send(proto.CH_GAME, proto.OP_MARKET_QUERY, body)

    def _drain_listings(self, budget: float) -> list:
        """Read for up to *budget* seconds, decode frames, inflate zlib app
        messages, and return all listings parsed from them."""
        assert self._sock is not None
        deadline = time.monotonic() + budget
        app = b""
        while time.monotonic() < deadline:
            try:
                chunk = self._recv_some()
            except TimeoutError:
                break
            if not chunk:
                break
            self._rx += chunk
            frames, self._rx = proto.decode_frames(self._rx)
            for fr in frames:
                app += fr.payload
        merged = b"".join(out for _, out in proto.iter_zlib_members(app))
        return parse_listings(merged)

    # -- loop -------------------------------------------------------------
    def poll_once(self, selectors: list[int] | None = None) -> list[Event]:
        self.request_market(selectors)
        listings = self._drain_listings(budget=min(self.poll_interval, 10.0))
        return self.observer.observe(listings)

    def run(self, on_event: Callable[[Event], None]) -> None:
        """Poll forever, invoking *on_event* for each change. Sends keepalives."""
        while True:
            for ev in self.poll_once():
                on_event(ev)
            self.ping()
            time.sleep(self.poll_interval)
