"""Unit tests for the live client's per-connection message counter."""

from aoeo_market.client import MarketClient


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
