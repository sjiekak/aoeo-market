"""Unit tests for the live client's message counter and drain budget."""

from aoeo_market.client import MarketClient
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
