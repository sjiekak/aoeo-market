"""Tests for the TCP 4564 "Celeste Network" login (aoeo_market.auth)."""

import pytest

from aoeo_market.auth import build_login_request, build_login_tail


def test_build_login_request_rejects_bad_device_hash():
    with pytest.raises(ValueError):
        build_login_request(
            "dummy@example.com",
            "dummy-password",
            "127.0.0.1",
            device_hash="tooshort",
        )


@pytest.mark.parametrize(
    ("ip", "expected_payload"),
    [
        ("192.168.1.37", "458e0d1ec0a8012540000000"),
        ("192.168.0.1", "458e0d1ec0a8000140000000"),
    ],
)
def test_build_login_tail(ip: str, expected_payload: str):
    """A local IPv4 address encodes to the expected tail bytes."""
    assert build_login_tail(ip) == bytes.fromhex(expected_payload)
