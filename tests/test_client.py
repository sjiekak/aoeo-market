"""Unit tests for the live client's message counter and drain budget."""

import struct

import pytest

from aoeo_market import client as client_mod
from aoeo_market.auth import LoginRejected
from aoeo_market.client import MarketClient, Session
from aoeo_market.constants import MIN_DRAIN_BUDGET


def test_default_drain_budget_floored_at_40s():
    # The default poll interval (30s) is below the floor: the budget is raised
    # so a slow/streamed market reply is not truncated.
    assert MarketClient()._default_drain_budget() == MIN_DRAIN_BUDGET == 40.0
    # A longer poll interval still wins over the floor.
    assert MarketClient(poll_interval=120.0)._default_drain_budget() == 120.0


def test_next_ctr_wraps_within_one_byte():
    mc = MarketClient()
    mc._ctr = 0xFF
    assert mc._next_ctr() == 0xFF
    assert mc._next_ctr() == 0x00
    assert mc._next_ctr() == 0x01


def test_next_ctr_never_escapes_frame_header():
    mc = MarketClient()
    mc._ctr = 0xFF
    for _ in range(600):  # far more messages than any watch session sends
        assert 0 <= mc._next_ctr() <= 0xFF


class _FakeSock:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def settimeout(self, t: float) -> None:
        pass

    def close(self) -> None:
        pass


def _f2_reply(status: int) -> bytes:
    """The 18-byte 0xF2 login reply: context, ch 0x0101, opcode 0xF2, counter
    2, one payload byte (0x01 = accepted, 0x00 = rejected)."""
    return b"\x00" * 8 + struct.pack(">HHI", 0x0101, 0x0101, 0xF2) + b"\x02" + bytes([status])


def test_login_detects_f2_rejection(monkeypatch):
    """An 0xF2 reply whose payload starts with 0x00 is a rejected login."""
    sock = _FakeSock([_f2_reply(0x00)])
    monkeypatch.setattr(client_mod.socket, "create_connection", lambda *a, **k: sock)
    mc = MarketClient(connect_timeout=1.0)
    with pytest.raises(LoginRejected, match="0xF2"):
        mc.login(Session(xuid=1, username="x", token="T" * 32))


def test_login_accepts_f2_status_one(monkeypatch):
    """An 0xF2 reply starting with 0x01 is the accepted login."""
    sock = _FakeSock([_f2_reply(0x01)])
    monkeypatch.setattr(client_mod.socket, "create_connection", lambda *a, **k: sock)
    mc = MarketClient(connect_timeout=1.0)
    reply = mc.login(Session(xuid=1, username="x", token="T" * 32))
    assert b"\x00\x00\x00\xf2" in reply
    assert len(sock.sent) == 1  # the login bundle


def test_login_without_reply_is_not_a_rejection(monkeypatch):
    """A silent server (no 0xF2 at all) keeps the historical behavior."""
    sock = _FakeSock([])
    monkeypatch.setattr(client_mod.socket, "create_connection", lambda *a, **k: sock)
    mc = MarketClient(connect_timeout=1.0)
    assert mc.login(Session(xuid=1, username="x", token="T" * 32)) == b""
