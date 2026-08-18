"""Tests for the TCP 4564 "Celeste Network" login (aoeo_market.auth)."""

import pytest

from aoeo_market.auth import (
    DEVICE_HASH,
    DEVICE_HASH_ALT,
    LOGIN_TAIL_OPAQUE,
    LOGIN_TAIL_OPAQUE_ALT,
    PROTOCOL_VERSION,
    build_login_request,
    build_login_tail,
    build_relogin_request,
)


def test_build_login_request_rejects_bad_device_hash():
    with pytest.raises(ValueError):
        build_login_request(
            "dummy@example.com",
            "dummy-password",
            "127.0.0.1",
            device_hash="tooshort",
        )


@pytest.mark.parametrize(
    ("ip", "opaque", "expected_payload"),
    [
        # machine B (2026-08-13/17 captures, local IP 192.168.0.17)
        ("192.168.0.17", LOGIN_TAIL_OPAQUE, "f69b991ac0a8001140000000"),
        # machine A (2026-08-10 capture, local IP 192.168.1.37)
        ("192.168.1.37", LOGIN_TAIL_OPAQUE_ALT, "458e0d1ec0a8012540000000"),
    ],
)
def test_build_login_tail(ip: str, opaque: bytes, expected_payload: str):
    """A local IPv4 address encodes to the expected tail bytes.

    The first 4 bytes are per-install opaque data — NOT a constant 0x45 —
    while the trailing ``40 00 00 00`` group is constant across captures.
    """
    assert build_login_tail(ip, opaque) == bytes.fromhex(expected_payload)


def test_build_login_tail_rejects_bad_opaque():
    with pytest.raises(ValueError):
        build_login_tail("192.168.0.17", opaque=b"\x00\x00")


def test_login_request_layout_constants():
    pkt = build_login_request("a@b.co", "pw", "192.168.0.17")
    # 40 zero bytes, 0x01, version 2018 LE
    assert pkt[8:48] == b"\x00" * 40
    assert pkt[48] == 0x01
    assert pkt[49:53] == PROTOCOL_VERSION.to_bytes(4, "little")
    assert PROTOCOL_VERSION == 2018
    # email and password, length-prefixed
    assert pkt[53:57] == (6).to_bytes(4, "little")
    assert pkt[57:63] == b"a@b.co"
    assert pkt[63:67] == (2).to_bytes(4, "little")
    assert pkt[67:69] == b"pw"
    # tail: opaque(4) + ip(4) + 40 00 00 00, then the 64-char device hash
    assert pkt[69:73] == LOGIN_TAIL_OPAQUE
    assert pkt[73:77] == bytes([192, 168, 0, 17])
    assert pkt[77:81] == b"\x40\x00\x00\x00"
    assert pkt[81:145] == DEVICE_HASH.encode("ascii")


def test_relogin_request_layout():
    xuid = 0x0123456789ABCDEF
    token = "T" * 32
    pkt = build_relogin_request("a@b.co", "pw", "192.168.0.17", xuid, token)
    assert pkt[0:8] == (7).to_bytes(4, "little") + (8 + 137).to_bytes(4, "little")
    body = pkt[8:]
    # xuid + token + 0x01 + version + email + password + tail + hash
    assert body[0:8] == xuid.to_bytes(8, "little")
    assert body[8:40] == token.encode("ascii")
    assert body[40] == 0x01
    assert body[41:45] == PROTOCOL_VERSION.to_bytes(4, "little")
    assert body[45:49] == (6).to_bytes(4, "little")
    assert body[49:55] == b"a@b.co"
    assert body[55:59] == (2).to_bytes(4, "little")
    assert body[59:61] == b"pw"
    assert body[61:73] == LOGIN_TAIL_OPAQUE + bytes([192, 168, 0, 17]) + b"\x40\x00\x00\x00"
    assert body[73:137] == DEVICE_HASH.encode("ascii")
    assert len(body) == 137


def test_device_hash_constants():
    """The two known per-install hashes are 64 hex chars each."""
    assert len(DEVICE_HASH) == 64
    assert len(DEVICE_HASH_ALT) == 64
    assert DEVICE_HASH != DEVICE_HASH_ALT
    int(DEVICE_HASH, 16)
    int(DEVICE_HASH_ALT, 16)
