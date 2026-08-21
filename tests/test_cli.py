"""Unit tests for the CLI printers — no network or captures required."""

from aoeo_market.cli import _print_event, _print_listings
from aoeo_market.market import Listing
from aoeo_market.observer import ListedEvent, RemovalReason, RemovedEvent


def _mk(tx: int, **kw) -> Listing:
    base = {
        "transaction_id": tx,
        "seller_empire_id": 1,
        "buyer_character_id": -1,
        "item_id": "ArmorPlt_Halloween2025",
        "item_type": "Trait",
        "item_level": 43,
        "item_count": 1,
        "item_price": 99000,
        "item_seed": 0,
        "seconds_till_expiry": 2590620,
    }
    base.update(kw)
    return Listing(**base)


def test_print_listings_table(capsys):
    _print_listings([_mk(1), _mk(2, item_price=1234)])
    out = capsys.readouterr().out
    assert out.startswith("2 active listings")
    assert "ITEM_ID" in out
    assert "ArmorPlt_Halloween2025" in out
    # sorted by (type, item_id, price)
    assert out.index("99000") > out.index("1234")


def test_print_listed_event(capsys):
    _print_event(ListedEvent(at=0.0, listing=_mk(1)))
    out = capsys.readouterr().out
    assert "LISTED" in out
    assert "tx=1" in out
    assert "ArmorPlt_Halloween2025" in out
    assert "@ 99000" in out


def test_print_removed_event_unknown_cause(capsys):
    _print_event(RemovedEvent(at=0.0, listing=_mk(1), reason=RemovalReason.REMOVED, expected_expiry_at=1.0))
    out = capsys.readouterr().out
    assert "REMOVED" in out
    assert "-> REMOVED" in out


def test_print_removed_event_expired(capsys):
    _print_event(RemovedEvent(at=0.0, listing=_mk(1), reason=RemovalReason.EXPIRED, expected_expiry_at=1.0))
    out = capsys.readouterr().out
    assert "REMOVED" in out
    assert "-> EXPIRED" in out
