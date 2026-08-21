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

``MarketClient.acquire_session(mail, password, local_ip, device_hash=...,
opaque=...)`` obtains all three by logging in over the plaintext "Celeste
Network" service on TCP 4564, exactly the way the game itself
(``Spartan.exe`` via ``xlive.dll``) authenticates. See :mod:`aoeo_market.auth`.
The ``device_hash`` / ``opaque`` per-install values are required — the caller
supplies the constants for its machine.

Alternatively you can extract them once from a real login you perform with the
official launcher (sniff your own 4564 login response, or the 1510 login frame
opcode 0xF1). Fine for a personal tool.

IMPORTANT OPERATIONAL NOTE
--------------------------
The backend may allow only one live session per account. If so, run EITHER the
game OR this client, not both at once, or you may get disconnected. A read-only
poller that logs in as its own (possibly dedicated) account is the clean setup.

The 1510 login handshake was fully reversed from two independent captures
(2026-08-10 and 2026-08-17): :meth:`login` replays the exact frame and
message sequence the game sends (see :func:`aoeo_market.protocol
.build_login_bundle`), and the market polling reuses the offline zlib-scanning
pipeline verified against the captures.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import protocol as proto
from .constants import (
    CELESTE_NETWORK_HOST,
    CELESTE_NETWORK_PORT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    GAME_SERVER_HOST,
    GAME_SERVER_PORT,
)
from .market import Listing, parse_listings
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
    _ctr: int = field(default=1, init=False)  # per-connection message counter
    _ctx: bytes = field(default=b"\x00" * 8, init=False)

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        self._sock = socket.create_connection((self.server, self.port), self.connect_timeout)
        self._sock.settimeout(self.connect_timeout)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def _next_ctr(self) -> int:
        s = self._ctr
        self._ctr += 1
        return s

    def _send(self, channel: int, opcode: int, payload: bytes) -> None:
        assert self._sock is not None
        frame = proto.Frame(self._ctx, channel, opcode, payload, self._next_ctr()).encode()
        self._sock.sendall(frame)

    def _recv_some(self) -> bytes:
        assert self._sock is not None
        return self._sock.recv(65536)

    # -- auth -------------------------------------------------------------
    def acquire_session(
        self,
        mail: str,
        password: str,
        local_ip: str,
        host: str = CELESTE_NETWORK_HOST,
        port: int = CELESTE_NETWORK_PORT,
        *,
        device_hash: str,
        opaque: bytes,
    ) -> Session:
        """Log in over the Celeste Network (TCP 4564) and return a Session.

        Mirrors what the game does: send email + password in a plaintext login
        packet, read back ``xuid`` / profile name / 32-char token, then
        register the session. See :mod:`aoeo_market.auth`.

        ``device_hash`` and ``opaque`` are the per-install constants for the
        machine this client runs on (:data:`aoeo_market.auth.DEVICE_HASH` and
        :data:`aoeo_market.auth.LOGIN_TAIL_OPAQUE`).  They are required — no
        defaults are inferred here; the CLI layer chooses the values.
        """
        from . import auth

        cn = auth.CelesteNetworkClient(host=host, port=port, timeout=self.connect_timeout)
        try:
            gs = cn.login(
                mail,
                password,
                local_ip,
                device_hash=device_hash,
                opaque=opaque,
            )
        finally:
            cn.close()
        return Session(xuid=gs.xuid, username=gs.username, token=gs.token)

    def login(self, session: Session) -> bytes:
        """Connect to the game service and perform the 1510 login handshake.

        Sends the full captured login: the 0xF1 frame (channel 0x0101) whose
        payload is the ``version + xuid + name + token`` prefix followed by
        the eight-message bundle (0xFF/0x91/0x1C/0x61/0xBE/0x55/0xAD/0x57 with
        counters 1..8, the last carrying the client settings XML).  The server
        answers with an 0xF2 frame whose bundle includes the initial market
        offer data (opcode 0x62, zlib ``<Empire><Offers>``); the bytes read
        during the handshake are buffered internally so the first
        :meth:`poll_once` can surface them, and are returned to the caller.
        """
        self.connect()
        bundle = proto.build_login_bundle(session.xuid, session.username, session.token)
        assert self._sock is not None
        self._sock.sendall(bundle)
        self._ctr = 9  # counters 1..8 were consumed by the login bundle

        received = b""
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            if b"\x00\x00\x00\xf2" in self._rx:
                # The 0xF2 header arrived; keep draining for a short window so
                # the rest of the reply bundle (the offers document) lands in
                # the buffer for the first poll.
                deadline = min(deadline, time.monotonic() + 3.0)
            try:
                chunk = self._recv_some()
            except TimeoutError:
                break
            if not chunk:
                break
            self._rx += chunk
            received += chunk
        return received

    def ping(self) -> None:
        self._send(proto.CH_GAME, proto.OP_PING, proto.build_ping_payload())

    # -- market -----------------------------------------------------------
    def request_market(self, sweep: list[list[int]] | None = None) -> None:
        """Send the market browse sweep.

        By default the captured ten-query sweep
        (:data:`aoeo_market.protocol.DEFAULT_MARKET_SWEEP`) is replayed.  An
        all-wildcard query is *not* answered by the server — the queries must
        keep the game's selector shapes.
        """
        if sweep is None:
            sweep = [list(s) for s in proto.DEFAULT_MARKET_SWEEP]
        for i, selectors in enumerate(sweep):
            body = proto.build_market_query_payload(i, selectors)
            self._send(proto.CH_GAME, proto.OP_MARKET_QUERY, body)

    def _drain_listings(self, budget: float) -> list:
        """Read for up to *budget* seconds, inflate zlib app messages straight
        from the raw byte stream, and return all listings parsed from them.

        Data messages carry their own ``[u32 inflated][u32 deflated][zlib]``
        sizes and may span the declared frame lengths, so the frame layer is
        bypassed here: the raw stream is scanned for complete zlib members,
        exactly like :func:`aoeo_market.pcap_source.listings_from_pcap` does
        offline.
        """
        assert self._sock is not None
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                chunk = self._recv_some()
            except TimeoutError:
                break
            if not chunk:
                break
            self._rx += chunk
        merged = b"".join(out for _, out in proto.iter_zlib_members(self._rx))
        return parse_listings(merged)

    # -- loop -------------------------------------------------------------
    def fetch_listings(self, sweep: list[list[int]] | None = None, budget: float | None = None) -> list[Listing]:
        """Send the market browse sweep and return the raw active listings.

        One-shot counterpart to :meth:`poll_once` that does not feed the
        observer: useful for a caller that just wants the current snapshot.
        """
        self.request_market(sweep)
        if budget is None:
            budget = min(self.poll_interval, 20.0)
        return self._drain_listings(budget=budget)

    def poll_once(self, sweep: list[list[int]] | None = None) -> list[Event]:
        return self.observer.observe(self.fetch_listings(sweep))

    def run(self, on_event: Callable[[Event], None]) -> None:
        """Poll forever, invoking *on_event* for each change. Sends keepalives."""
        while True:
            for ev in self.poll_once():
                on_event(ev)
            self.ping()
            time.sleep(self.poll_interval)
