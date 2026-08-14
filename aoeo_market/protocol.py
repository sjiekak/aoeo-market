"""Wire protocol for the Age of Empires Online / Project Celeste game backend.

Decoded from packet captures of the live client talking to the game server
(51.91.169.108) on TCP 1510. The channel carries a custom length-prefixed
binary framing that wraps zlib-compressed XML application messages.

Frame layout (all multi-byte header fields big-endian)::

    [8B context/session id] [2B channel] [2B payload length] [4B opcode]
    [payload: <length> bytes] [1B trailer]

Notes
-----
* The 8-byte context id is zero during the pre-login handshake.
* ``channel`` 0x0032 (50) is the main game service; 0x0101 (257) carries login.
* Payloads on the game service are frequently zlib streams (``0x78 0x9c`` /
  ``0x78 0xda``) whose inflated content is UTF-8/UTF-16 XML.
* ``length`` is only 16 bits, so large server messages are split across several
  frames; rather than depend on frame boundaries for reassembly we scan the
  decoded application byte stream for complete zlib members (see
  :func:`iter_zlib_members`), which is robust to that splitting.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

HEADER_LEN = 16
TRAILER_LEN = 1

# Channels
CH_LOGIN = 0x0101
CH_GAME = 0x0032

# Opcodes observed on the game service (channel 0x0032)
OP_PING = 0x7E          # keepalive; payload[0:8] LE = global sequence counter
OP_MARKET_QUERY = 0xAB  # client -> server market browse request
OP_LOGIN = 0xF1         # client -> server login (on channel 0x0101)

ZLIB_MAGIC = (b"\x78\x01", b"\x78\x9c", b"\x78\xda")


@dataclass(frozen=True)
class Frame:
    context: bytes  # 8 bytes
    channel: int
    opcode: int
    payload: bytes

    def encode(self, trailer: int = 0x00) -> bytes:
        if len(self.context) != 8:
            raise ValueError("context must be exactly 8 bytes")
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload too large for 16-bit length field")
        return (
            self.context
            + struct.pack(">HHI", self.channel, len(self.payload), self.opcode)
            + self.payload
            + bytes([trailer])
        )


def decode_frames(data: bytes) -> tuple[list[Frame], bytes]:
    """Decode as many whole frames as possible.

    Returns ``(frames, remainder)`` where ``remainder`` is trailing bytes that
    do not yet form a complete frame (a partial read from a socket).
    """
    frames: list[Frame] = []
    off = 0
    n = len(data)
    while off + HEADER_LEN <= n:
        context = data[off : off + 8]
        channel, length, opcode = struct.unpack(">HHI", data[off + 8 : off + 16])
        end = off + HEADER_LEN + length + TRAILER_LEN
        if end > n:
            break
        payload = data[off + HEADER_LEN : off + HEADER_LEN + length]
        frames.append(Frame(context, channel, opcode, payload))
        off = end
    return frames, data[off:]


def iter_zlib_members(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(offset, inflated_bytes)`` for every zlib member found in *data*.

    The game service concatenates independently-compressed application messages;
    this walks the buffer, inflating each ``0x78``-prefixed member and skipping
    ahead by exactly the number of compressed bytes consumed.
    """
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b"\x78", i)
        if j < 0 or j + 1 >= n:
            return
        if data[j : j + 2] in ZLIB_MAGIC:
            d = zlib.decompressobj()
            try:
                out = d.decompress(data[j:])
            except zlib.error:
                i = j + 1
                continue
            if len(out) >= 8:
                consumed = len(data[j:]) - len(d.unused_data)
                yield j, out
                i = j + consumed
                continue
        i = j + 1


# --- Message builders -----------------------------------------------------

def build_login_payload(xuid: int, username: str, token: str, version: int = 0x02) -> bytes:
    """Build the body of an OP_LOGIN (0xF1) frame.

    Structure (from capture)::

        [1B version] [8B xuid LE] [4B name-len LE][name]
        [4B token-len LE][token] ...trailing fields...

    The trailing fields beyond the token were not needed to identify the login
    but are preserved verbatim from a real login when replaying; see
    :mod:`aoeo_market.client`.
    """
    name = username.encode("ascii")
    tok = token.encode("ascii")
    return (
        bytes([version])
        + struct.pack("<q", xuid)
        + struct.pack("<I", len(name)) + name
        + struct.pack("<I", len(tok)) + tok
    )


def build_ping_payload(seq: int) -> bytes:
    """OP_PING (0x7e) body: 8-byte little-endian sequence counter, zero-padded."""
    return struct.pack("<Q", seq) + b"\x00" * 16


# Market query category selectors, as seen in captured 0xAB payloads. 0xFFFFFFFF
# is a wildcard ("any"). The client's browse iterated several category tuples;
# a broadly-wildcarded query is expected to return the whole market.
WILDCARD = 0xFFFFFFFF


def build_market_query_payload(seq: int, selectors: list[int]) -> bytes:
    """Build an OP_MARKET_QUERY (0xAB) body.

    ``selectors`` is the sequence of 32-bit little-endian filter words that
    follow the 8-byte counter in the captured requests. Use :data:`WILDCARD`
    for "any". Exact selector semantics (category / rarity / level) are only
    partially reversed; wildcard-heavy queries are the safe default.
    """
    body = struct.pack("<Q", seq)
    for s in selectors:
        body += struct.pack("<I", s)
    return body
