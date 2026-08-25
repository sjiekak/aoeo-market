"""Wire protocol for the Age of Empires Online / Project Celeste game backend.

Decoded from packet captures of the live client talking to the game server
(51.91.169.108) on TCP 1510. The channel carries a custom length-prefixed
binary framing that wraps zlib-compressed XML application messages.

Frame layout (all multi-byte header fields big-endian)::

    [8B context/session id] [2B channel] [2B payload length] [4B opcode]
    [1B counter] [payload: <length> bytes]

Notes
-----
* The 8-byte context id is zero before and after login (captured sessions keep
  it zero throughout).
* ``channel`` 0x0032 (50) is the main game service; 0x0101 (257) carries the
  login frame; the lobby/realm service on TCP 1500 uses 0x0028 (40).
* The 1-byte **counter** is a per-connection correlation id: it starts at 1 in
  the login bundle, increments on every client message (wrapping within the
  byte after 255), and is echoed by the
  server's reply (login reply opcodes 0xFE/0x92/0x1D/0x62 mirror the request
  counters 1..4; ping 0x7E counter N is answered by 0x7F counter N).
* The opcode field is ``[1B flags][3B opcode]``.  Flags 0 = plain, and
  messages whose payload embeds a zlib member use flag 2 (client -> server,
  e.g. 0x02000057) or flag 1 (server -> client, e.g. 0x01000062).
* The 2-byte ``length`` is **not reliable for framing**: it describes only the
  part of the payload the game had buffered when it wrote the header.  Data
  messages carry their own ``[u32 inflated size][u32 deflated size][zlib]``
  fields inside the payload and may continue past the declared length; the
  login frame's declared length covers its prefix plus the first 255 bytes of
  the embedded message stream, which continues in subsequent TCP segments.
  Robust parsing therefore scans the payload stream for complete zlib members
  (see :func:`iter_zlib_members`) instead of trusting ``length``.
* Payloads on the game service are frequently zlib streams (``0x78 0x9c`` /
  ``0x78 0xda``) whose inflated content is UTF-8/UTF-16 XML.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

HEADER_LEN = 16
COUNTER_LEN = 1

# Channels
CH_LOGIN = 0x0101
CH_GAME = 0x0032

# Opcodes observed on the game service (channel 0x0032)
OP_PING = 0x7E  # keepalive; payload = 24 zero bytes (counter byte seqs it)
OP_MARKET_QUERY = 0xAB  # client -> server market browse request
OP_LOGIN = 0xF1  # client -> server login (on channel 0x0101)
OP_LOGIN_OK = 0xF2  # server -> client login reply (on channel 0x0101)

# Login-bundle message opcodes, in the order the game sends them after the
# 0xF1 prefix (verified byte-for-byte in two independent captures, 2026-08-10
# and 2026-08-17).  The server's replies use the same counters.
OP_BUNDLE_PREP = 0xFF  # ch 0x0000, payload: 16-byte constant blob
OP_BUNDLE_XBOX = 0x91  # payload: b"\x00"
OP_BUNDLE_PROFILE = 0x1C  # u32 utf16-len + UTF-16 profile name + xuid
OP_BUNDLE_XUID = 0x61  # payload: xuid
OP_BUNDLE_MARK = 0xBE  # payload: empty
OP_BUNDLE_XUID2 = 0x55  # payload: xuid
OP_BUNDLE_XUID3 = 0xAD  # payload: xuid
OP_BUNDLE_SETTINGS = 0x57  # xuid + u32 inflated + u32 deflated + zlib settings

# Reply opcodes (server -> client) seen right after the 0xF2 login reply.
OP_BUNDLE_PREP_REPLY = 0xFE
OP_BUNDLE_XBOX_REPLY = 0x92
OP_BUNDLE_PROFILE_REPLY = 0x1D
OP_BUNDLE_OFFERS = 0x62  # xuid + u32 + u32 + zlib "<Empire><Offers>…" XML

ZLIB_MAGIC = (b"\x78\x01", b"\x78\x9c", b"\x78\xda")

# Constant 16-byte payload of the 0xFF login-bundle message; identical in the
# 2026-08-10 and 2026-08-17 captures.
BUNDLE_PREP_PAYLOAD = bytes.fromhex("04000000000000000000000004180000")


@dataclass(frozen=True)
class Frame:
    context: bytes  # 8 bytes
    channel: int
    opcode: int
    payload: bytes
    counter: int = 0  # per-connection correlation id; echoed in the reply

    def encode(self) -> bytes:
        if len(self.context) != 8:
            raise ValueError("context must be exactly 8 bytes")
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload too large for 16-bit length field")
        if not 0 <= self.counter <= 0xFF:
            raise ValueError("counter must fit in one byte")
        return self.context + struct.pack(">HHI", self.channel, len(self.payload), self.opcode) + bytes([self.counter]) + self.payload


def decode_frames(data: bytes) -> tuple[list[Frame], bytes]:
    """Decode as many whole frames as possible.

    Returns ``(frames, remainder)`` where ``remainder`` is trailing bytes that
    do not yet form a complete frame (a partial read from a socket).

    Note: the length field is not reliable for data messages whose zlib
    payload continues past the declared length (see module docstring); callers
    that need the full application stream should use
    :func:`iter_zlib_members` over the concatenated payloads.
    """
    frames: list[Frame] = []
    off = 0
    n = len(data)
    while off + HEADER_LEN + COUNTER_LEN <= n:
        context = data[off : off + 8]
        channel, length, opcode = struct.unpack(">HHI", data[off + 8 : off + 16])
        end = off + HEADER_LEN + COUNTER_LEN + length
        if end > n:
            break
        counter = data[off + HEADER_LEN]
        payload = data[off + HEADER_LEN + COUNTER_LEN : end]
        frames.append(Frame(context, channel, opcode, payload, counter))
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


def _msg(channel: int, opcode: int, counter: int, payload: bytes) -> bytes:
    return Frame(b"\x00" * 8, channel, opcode, payload, counter).encode()


def _msg_with_len(channel: int, opcode: int, counter: int, payload: bytes, declared_len: int) -> bytes:
    """A message whose declared length differs from its real payload length
    (the game under-declares data messages; the size fields inside the payload
    are what actually delimit it)."""
    return b"\x00" * 8 + struct.pack(">HHI", channel, declared_len, opcode) + bytes([counter]) + payload


def build_login_payload(xuid: int, username: str, token: str, version: int = 0x02) -> bytes:
    """Build the prefix of the OP_LOGIN (0xF1) frame.

    Structure (verified in the 2026-08-10 and 2026-08-17 captures)::

        [1B version=0x02] [8B xuid LE] [4B name-len LE][name]
        [4B token-len LE][token]

    The version byte is 0x02 on the 1510 game service and 0x01 on the 1500
    lobby service.  The prefix is followed by the login-bundle messages built
    by :func:`build_login_bundle`.
    """
    name = username.encode("ascii")
    tok = token.encode("ascii")
    return bytes([version]) + struct.pack("<q", xuid) + struct.pack("<I", len(name)) + name + struct.pack("<I", len(tok)) + tok


# Client-settings options sent in login-bundle message 8 (0x57).  The option
# names mirror the game client's standard settings UI (the same set the game
# sends); the values are neutral defaults, i.e. what a fresh client install
# would report.  The server stores this document per account and only answers
# market queries after receiving a complete one, so a full document — not an
# empty shell — is the default.
DEFAULT_SETTINGS: tuple[tuple[str, str], ...] = (
    ("optionfriendorfoecolor", "false"),
    ("optionfullrolloverhelp", "true"),
    ("optionlanguagefilter", "true"),
    ("optioneasydragmilitary", "false"),
    ("optionshowaitips", "true"),
    ("optionattackmove", "false"),
    ("optiontooltipenableworld", "true"),
    ("optiontooltipworlddisptime", "250.000000"),
    ("optiontooltipgameuidisptime", "0.000000"),
    ("optiontooltipbackgroundalpha", "1.000000"),
    ("optionshowhponrollover", "true"),
    ("optionuishowtraining", "false"),
    ("optionuishowfame", "false"),
    ("optionuishowgametime", "false"),
    ("optionuishowvillagertasks", "false"),
    ("optionuishowscore", "false"),
    ("optionshowobjectivehints", "2"),
    ("optionuishowshipments", "false"),
    ("optionadvancedformationui", "false"),
    ("optionenablerobustrollover", "false"),
    ("optionrightclickecononly", "false"),
    ("optioncamerainertiaramptime", "200.000000"),
    ("optionesoprivacy", "false"),
    ("optionminimizedui", "false"),
    ("optionshowchatnotifications", "true"),
    ("optionsavefiltersettings", "false"),
    ("optionminimizedchatui", "false"),
    ("optionallowhotkeyconflicts", "false"),
    ("optionuishowvisualcontrolgroups", "false"),
    ("optionshowhponalt", "0"),
    ("optionsnaponfindunit", "true"),
    ("optioninvertcontrolshift", "false"),
    ("optionsnaptounitdelay", "350.000000"),
    ("optionadvancedunittypeinfo", "false"),
    ("optionprotipsdisable", "false"),
    ("coptionunused", ""),
    ("optiononeclickgarrison", "false"),
    ("optionignorelist", ""),
    ("optionfriendnotificationdisable", "false"),
)


def build_settings_xml() -> bytes:
    """Build the default settings document as UTF-16-LE XML (no BOM, like the
    game's own document)."""
    parts = ['<Settings Version="45">\r\n']
    for name, value in DEFAULT_SETTINGS:
        parts.append(f'\t<Setting Name="{name}">{value}</Setting>\r\n')
    parts.append("</Settings>")
    return "".join(parts).encode("utf-16-le")


def build_login_bundle(
    xuid: int,
    username: str,
    token: str,
    settings_zlib: bytes | None = None,
) -> bytes:
    """Build the complete 1510 login: the 0xF1 frame plus its message bundle.

    Reproduces byte-for-byte what ``Spartan.exe`` sends after connecting to
    the game service (capture ``capture_aoeo_login_separate_user_email_password
    .pcapng``, 2026-08-17):

    * the 0xF1 outer frame: header (channel 0x0101, opcode 0xF1) whose
      declared length covers the prefix plus the first 255 bytes of the
      message stream, then the prefix from :func:`build_login_payload`;
    * eight messages with counters 1..8: 0xFF (constant 16-byte blob, on
      channel 0x0000), 0x91 (``\\x00``), 0x1C (UTF-16 profile name + xuid),
      0x61 (xuid), 0xBE (empty), 0x55 (xuid), 0xAD (xuid), and 0x57 (xuid +
      zlib-compressed client settings XML).

    The settings message writes the account's stored client settings, and the
    server only answers market queries after a complete settings document, so
    by default a full default-values document (:func:`build_settings_xml`) is
    compressed and sent.  Pass ``settings_zlib`` to replay a captured document
    instead; the reference blob extracted from the capture is kept with the
    local-only capture tests.
    """
    prefix = build_login_payload(xuid, username, token)
    name_utf16 = username.encode("utf-16-le")
    bundle = b""
    bundle += _msg(0x0000, OP_BUNDLE_PREP, 1, BUNDLE_PREP_PAYLOAD)
    bundle += _msg(CH_GAME, OP_BUNDLE_XBOX, 2, b"\x00")
    bundle += _msg(
        CH_GAME,
        OP_BUNDLE_PROFILE,
        3,
        struct.pack("<I", len(name_utf16)) + name_utf16 + struct.pack("<q", xuid),
    )
    bundle += _msg(CH_GAME, OP_BUNDLE_XUID, 4, struct.pack("<q", xuid))
    bundle += _msg(CH_GAME, OP_BUNDLE_MARK, 5, b"")
    bundle += _msg(CH_GAME, OP_BUNDLE_XUID2, 6, struct.pack("<q", xuid))
    bundle += _msg(CH_GAME, OP_BUNDLE_XUID3, 7, struct.pack("<q", xuid))

    if settings_zlib is None:
        settings_zlib = zlib.compress(build_settings_xml())
    settings = struct.pack("<q", xuid) + struct.pack("<II", len(zlib.decompress(settings_zlib)), len(settings_zlib)) + settings_zlib
    # The game declares only 155 bytes of this message's payload length — the
    # size fields above are what actually delimit it (see module docstring).
    settings_declared = 16 + min(len(settings_zlib), 139)
    bundle += _msg_with_len(CH_GAME, 0x02000057, 8, settings, settings_declared)

    # The game declares the prefix plus the first 255 bytes of the message
    # stream as the 0xF1 payload length; the stream continues past it.
    first_chunk = (prefix + bundle)[: len(prefix) + 255]
    header = b"\x00" * 8 + struct.pack(">HHI", CH_LOGIN, len(first_chunk), OP_LOGIN)
    return header + prefix + bundle


def build_ping_payload(seq: int = 0) -> bytes:
    """OP_PING (0x7e) body: 8-byte little-endian field, zero-padded.

    Captured pings carry 24 zero bytes (the 8-byte field is always 0; the
    frame's counter byte is what sequences them).
    """
    return struct.pack("<Q", seq) + b"\x00" * 16


# Market query category selectors, as seen in captured 0xAB payloads. 0xFFFFFFFF
# is a wildcard ("any").  Each query carries nine 32-bit selector words; word[0]
# is the top-level category:
#
#     1 materials    2 blueprints    3 gear ("Trait")    4 designs
#     6 advisors     9 consumables
#
# An all-wildcard query is *not* answered, so every category keeps the game's
# own selector shape.  The sweep below replays the complete enumeration the
# game sent while iterating every category (capture
# ``capture_aoeo_login_market_iterate_over_several_listings.pcapng``,
# 2025-08-25, gear types browsed alphabetically).
WILDCARD = 0xFFFFFFFF

#: Gear ("Trait") type selector values (word[5]), in the order the game browses
#: them (alphabetical by type name).
_GEAR_TYPE_SELECTORS: tuple[int, ...] = (
    67,
    68,
    777,
    70,
    71,
    64,
    75,
    79,
    72,
    73,
    91,
    89,
    84,
    85,
    80,
    77,
    65,
    83,
    66,
    81,
    82,
    63,
    78,
    92,
    90,
    86,
    76,
    93,
    74,
    87,
    69,
    133,
    134,
    138,
    88,
)

#: The complete "whole market" sweep — one query per (category, sub-filter)
#: shape, exactly as the game browsed it.
DEFAULT_MARKET_SWEEP: tuple[tuple[int, ...], ...] = tuple(
    [(3, WILDCARD, WILDCARD, WILDCARD, WILDCARD, t, 0, WILDCARD, 0) for t in _GEAR_TYPE_SELECTORS]
    + [
        (6, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 0),  # advisors
        (9, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 0),  # consumables
        (4, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 3),  # designs
        (4, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 8),  # designs
        (4, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 0, 1),  # designs
        (1, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 0),  # materials
        (2, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, WILDCARD, 0),  # blueprints
    ]
)


def build_market_query_payload(seq: int, selectors: list[int]) -> bytes:
    """Build an OP_MARKET_QUERY (0xAB) body.

    ``selectors`` is the sequence of 32-bit little-endian filter words that
    follow the 8-byte sequence field in the captured requests — **nine** words,
    not six: every captured 0xAB payload is 44 bytes (8-byte sequence + 9
    selectors).  Use :data:`WILDCARD` for "any".  :data:`DEFAULT_MARKET_SWEEP`
    is the complete category enumeration (see its comment).
    """
    body = struct.pack("<Q", seq)
    for s in selectors:
        body += struct.pack("<I", s)
    return body
