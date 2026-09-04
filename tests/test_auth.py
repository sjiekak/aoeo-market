"""Tests for the TCP 4564 "Celeste Network" login (aoeo_market.auth)."""

import pytest

from aoeo_market import auth
from aoeo_market.auth import (
    DEVICE_HASH,
    DEVICE_HASH_ALT,
    DEVICE_HASH_PRE_UPGRADE,
    INSTALL_SIGNATURE_SUFFIX,
    PROTOCOL_VERSION,
    CelesteNetworkClient,
    LoginRejected,
    XliveManifestError,
    build_install_signature,
    build_login_request,
    build_relogin_request,
    parse_login_response,
    xlive_crc32_from_manifest,
)
from tests.auth_ref import XLIVE_CRC32, XLIVE_CRC32_ALT, XLIVE_CRC32_PRE_UPGRADE


def test_build_login_request_rejects_bad_device_hash():
    with pytest.raises(ValueError):
        build_login_request(
            "dummy@example.com",
            "dummy-password",
            "127.0.0.1",
            device_hash="tooshort",
            xlive_crc32=XLIVE_CRC32,
        )


@pytest.mark.parametrize(
    ("ip", "xlive_crc32", "expected_payload"),
    [
        # xlive.dll 1.0.0.106 (manifest of 2026-09-04), local IP 192.168.0.17
        ("192.168.0.17", XLIVE_CRC32, "8ca16109c0a8001140000000"),
        # the pre-upgrade xlive.dll build (rejected by the server now)
        ("192.168.0.17", XLIVE_CRC32_PRE_UPGRADE, "f69b991ac0a8001140000000"),
        # machine A's xlive.dll build (2026-08-10 capture, local IP 192.168.1.37)
        ("192.168.1.37", XLIVE_CRC32_ALT, "458e0d1ec0a8012540000000"),
    ],
)
def test_build_install_signature(ip: str, xlive_crc32: bytes, expected_payload: str):
    """The 12-byte install signature is the xlive.dll CRC-32 (LE), the local
    IPv4, and the constant ``40 00 00 00`` suffix.

    The first 4 bytes are the CRC-32 of the installed xlive.dll — NOT a
    constant 0x45 prefix or a per-machine opaque value — while the trailing
    group is constant across captures.
    """
    assert build_install_signature(ip, xlive_crc32) == bytes.fromhex(expected_payload)
    assert build_install_signature(ip, xlive_crc32)[8:] == INSTALL_SIGNATURE_SUFFIX


def test_build_install_signature_rejects_bad_crc():
    with pytest.raises(ValueError):
        build_install_signature("192.168.0.17", xlive_crc32=b"\x00\x00")


def test_xlive_crc32_from_manifest():
    """The manifest's CRC32 field becomes 4 little-endian bytes.

    157393292 == 0x0961A18C == the CRC-32 of xlive.dll 1.0.0.106; its
    little-endian bytes are ``8c a1 61 09``, what the login request sends.
    """
    body = b'\xef\xbb\xbf{\r\n  "FileName": "xlive.dll",\r\n  "CRC32": 157393292\r\n}'
    assert xlive_crc32_from_manifest(body) == bytes.fromhex("8ca16109")


def test_xlive_crc32_from_manifest_rejects_bad_field():
    with pytest.raises(ValueError):
        xlive_crc32_from_manifest(b'{"CRC32": "nope"}')
    with pytest.raises(ValueError):
        xlive_crc32_from_manifest(b'{"CRC32": -1}')
    with pytest.raises(ValueError):
        xlive_crc32_from_manifest(b'{"CRC32": 4294967296}')
    with pytest.raises(ValueError):
        xlive_crc32_from_manifest(b'{"other": 1}')


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_xlive_crc32(monkeypatch):
    """The live manifest is fetched and its CRC32 returned as LE bytes."""
    import urllib.request

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return _FakeResponse(b'{"FileName": "xlive.dll", "CRC32": 157393292}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(auth.urllib.request, "urlopen", fake_urlopen)
    assert auth.fetch_xlive_crc32(url="http://example.test/xlive.json", timeout=1.0) == bytes.fromhex("8ca16109")
    assert captured["url"] == "http://example.test/xlive.json"
    assert captured["timeout"] == 1.0


def test_fetch_xlive_crc32_wraps_network_failure(monkeypatch):
    """A network failure surfaces as XliveManifestError."""
    import urllib.request

    def boom(req, timeout):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(auth.urllib.request, "urlopen", boom)
    with pytest.raises(XliveManifestError, match="offline"):
        auth.fetch_xlive_crc32(url="http://example.test/xlive.json")


def test_fetch_xlive_crc32_wraps_bad_content(monkeypatch):
    """An unparsable manifest body surfaces as XliveManifestError."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"not json"))
    monkeypatch.setattr(auth.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"not json"))
    with pytest.raises(XliveManifestError, match="xlive manifest"):
        auth.fetch_xlive_crc32(url="http://example.test/xlive.json")


def test_login_request_layout_constants():
    pkt = build_login_request("a@b.co", "pw", "192.168.0.17", device_hash=DEVICE_HASH, xlive_crc32=XLIVE_CRC32)
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
    # install signature: xlive.dll CRC-32 (4) + ip (4) + 40 00 00 00, then the
    # 64-char device hash
    assert pkt[69:73] == XLIVE_CRC32
    assert pkt[73:77] == bytes([192, 168, 0, 17])
    assert pkt[77:81] == b"\x40\x00\x00\x00"
    assert pkt[81:145] == DEVICE_HASH.encode("ascii")


def test_relogin_request_layout():
    xuid = 0x0123456789ABCDEF
    token = "T" * 32
    pkt = build_relogin_request("a@b.co", "pw", "192.168.0.17", xuid, token, device_hash=DEVICE_HASH, xlive_crc32=XLIVE_CRC32)
    assert pkt[0:8] == (7).to_bytes(4, "little") + (8 + 137).to_bytes(4, "little")
    body = pkt[8:]
    # xuid + token + 0x01 + version + email + password + signature + hash
    assert body[0:8] == xuid.to_bytes(8, "little")
    assert body[8:40] == token.encode("ascii")
    assert body[40] == 0x01
    assert body[41:45] == PROTOCOL_VERSION.to_bytes(4, "little")
    assert body[45:49] == (6).to_bytes(4, "little")
    assert body[49:55] == b"a@b.co"
    assert body[55:59] == (2).to_bytes(4, "little")
    assert body[59:61] == b"pw"
    assert body[61:73] == XLIVE_CRC32 + bytes([192, 168, 0, 17]) + b"\x40\x00\x00\x00"
    assert body[73:137] == DEVICE_HASH.encode("ascii")
    assert len(body) == 137


def test_login_builders_require_identity_values():
    """The per-install device hash and the xlive CRC-32 are never defaulted —
    callers must pass them explicitly."""
    with pytest.raises(TypeError):
        build_login_request("a@b.co", "pw", "192.168.0.17")
    with pytest.raises(TypeError):
        build_relogin_request("a@b.co", "pw", "192.168.0.17", 1, "T" * 32)
    with pytest.raises(TypeError):
        build_install_signature("192.168.0.17")
    cn = CelesteNetworkClient()
    with pytest.raises(TypeError):
        cn.login("a@b.co", "pw", "192.168.0.17")


def test_device_hash_constants():
    """The two known per-install hashes are 64 hex chars each."""
    assert len(DEVICE_HASH) == 64
    assert len(DEVICE_HASH_ALT) == 64
    assert DEVICE_HASH != DEVICE_HASH_ALT
    int(DEVICE_HASH, 16)
    int(DEVICE_HASH_ALT, 16)


def test_post_upgrade_constants():
    """The post-upgrade machine-B values are the ones the server accepts."""
    assert DEVICE_HASH == "1cb498f3c8c76b0a654698f36dec7a05d16a879f6d4f41c67e1b507c63c1106f"
    assert XLIVE_CRC32 == bytes.fromhex("8ca16109")
    assert len(DEVICE_HASH_PRE_UPGRADE) == 64
    assert XLIVE_CRC32_PRE_UPGRADE == bytes.fromhex("f69b991a")


#: The packet-1 rejection body captured after the 2026-09-03 upgrade: the
#: success layout (8 zeros, 32 spaces, status byte) with the status zeroed
#: and every session field empty; the server then FINs.
REJECT_BODY = b"\x00" * 8 + b"\x20" * 32 + b"\x02\x00" + b"\x00" * 25


def test_parse_login_response_decodes_rejection_as_empty_session():
    """The rejection frame parses to xuid=0 and empty fields."""
    session = parse_login_response(REJECT_BODY)
    assert session.xuid == 0
    assert session.username == ""
    assert session.token == ""
    assert session.external_ip == ""


class _FakeSock:
    """Socket stand-in that serves queued responses in recv-sized chunks."""

    def __init__(self, responses: list[bytes]):
        self.responses = list(responses)
        self.buf = b""
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        while len(self.buf) < n and self.responses:
            self.buf += self.responses.pop(0)
        if not self.buf:
            return b""
        chunk, self.buf = self.buf[:n], self.buf[n:]
        return chunk

    def settimeout(self, t: float) -> None:
        pass

    def close(self) -> None:
        pass


def test_login_raises_on_rejected_session(monkeypatch):
    """A rejected packet-1 response raises LoginRejected and skips the
    session-register step (the server has already closed the connection)."""
    reject = auth._HEADER.pack(1, 8 + len(REJECT_BODY)) + REJECT_BODY
    sock = _FakeSock([reject])
    monkeypatch.setattr(auth.socket, "create_connection", lambda *a, **k: sock)
    cn = CelesteNetworkClient()
    with pytest.raises(LoginRejected, match="no session token"):
        cn.login(
            "a@b.co",
            "pw",
            "192.168.0.17",
            device_hash=DEVICE_HASH,
            xlive_crc32=XLIVE_CRC32,
        )
    assert len(sock.sent) == 1  # only the login request; no register


def test_login_accepts_real_session(monkeypatch):
    """A normal session (nonzero xuid + token) proceeds to the register."""
    body = (
        b"\x00" * 8
        + b"\x20" * 32
        + b"\x02\x0a"
        + (12345).to_bytes(8, "little")
        + (3).to_bytes(4, "little")
        + b"abc"
        + (32).to_bytes(4, "little")
        + b"T" * 32
        + (4).to_bytes(4, "little")
        + b"None"
        + b"\x01"
    )
    ok = auth._HEADER.pack(1, 8 + len(body)) + body
    manifest = auth._HEADER.pack(2, 8 + 4) + b"\x00" * 4
    sock = _FakeSock([ok, manifest])
    monkeypatch.setattr(auth.socket, "create_connection", lambda *a, **k: sock)
    cn = CelesteNetworkClient()
    session = cn.login(
        "a@b.co",
        "pw",
        "192.168.0.17",
        device_hash=DEVICE_HASH,
        xlive_crc32=XLIVE_CRC32,
    )
    assert session.xuid == 12345
    assert session.token == "T" * 32
    assert len(sock.sent) == 2  # login request + session register
    assert cn.manifest_received is True
