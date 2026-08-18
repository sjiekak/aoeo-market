"""Unit tests for the frame codec, the login payload layout, and the observer
logic — no packet captures required."""

import zlib

from aoeo_market.observer import MarketObserver, RemovalReason
from aoeo_market.protocol import (
    DEFAULT_SETTINGS,
    Frame,
    build_login_payload,
    build_settings_xml,
    decode_frames,
)

# --- protocol codec -------------------------------------------------------


def test_frame_roundtrip():
    f = Frame(context=b"\x00" * 8, channel=0x0032, opcode=0xAB, payload=b"hello")
    frames, rest = decode_frames(f.encode())
    assert rest == b""
    assert len(frames) == 1
    g = frames[0]
    assert (g.channel, g.opcode, g.payload) == (0x0032, 0xAB, b"hello")


def test_decode_partial_frame_is_buffered():
    f = Frame(b"\x00" * 8, 0x0032, 0x7E, b"abcd").encode()
    frames, rest = decode_frames(f[:-3])  # cut mid-frame
    assert frames == []
    assert rest == f[:-3]


def test_login_payload_layout():
    body = build_login_payload(xuid=0x0123456789ABCDEF, username="dummy", token="K" * 32)
    assert body[0] == 0x02
    assert body[1:9] == (0x0123456789ABCDEF).to_bytes(8, "little")
    assert body[9:13] == (5).to_bytes(4, "little")
    assert body[13:18] == b"dummy"


def test_default_settings_document():
    """The default settings document is a complete, game-shaped UTF-16-LE
    ``<Settings Version="45">`` document that compresses like the captured
    one (the server answers market queries only after such a document)."""
    xml = build_settings_xml()
    text = xml.decode("utf-16-le")
    assert text.startswith('<Settings Version="45">')
    assert text.rstrip().endswith("</Settings>")
    for name, value in DEFAULT_SETTINGS:
        assert f'<Setting Name="{name}">{value}</Setting>' in text
    # roughly the captured document's size class, not an empty shell
    assert 2000 <= len(xml) <= 5000
    assert len(zlib.compress(xml)) > 500


# --- observer semantics ---------------------------------------------------


def _mk(tx, expiry, price=100):
    from aoeo_market.market import Listing

    return Listing(
        transaction_id=tx,
        seller_empire_id=1,
        buyer_character_id=-1,
        item_id="X",
        item_type="Trait",
        item_level=1,
        item_count=1,
        item_price=price,
        item_seed=0,
        seconds_till_expiry=expiry,
    )


def test_listed_then_sold_vs_expired():
    obs = MarketObserver(expiry_grace_seconds=60, clock=lambda: 0.0)

    # t=0: two listings appear. A expires in 100s, B in 100000s.
    evs = obs.observe([_mk(1, 100), _mk(2, 100_000)], at=0.0)
    assert {e.kind for e in evs} == {"LISTED"}
    assert len(evs) == 2

    # t=200: both gone. A vanished after its expiry -> EXPIRED.
    #                    B vanished long before expiry -> SOLD_OR_CANCELLED.
    evs = obs.observe([], at=200.0)
    reasons = {e.listing.transaction_id: e.reason for e in evs}
    assert reasons[1] == RemovalReason.EXPIRED
    assert reasons[2] == RemovalReason.SOLD_OR_CANCELLED


def test_stable_listing_emits_no_event():
    obs = MarketObserver(clock=lambda: 0.0)
    obs.observe([_mk(1, 1000)], at=0.0)
    evs = obs.observe([_mk(1, 970)], at=30.0)  # same listing, ticked down
    assert evs == []
