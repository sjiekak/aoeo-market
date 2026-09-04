"""Unit tests for the shared login argument declarations and local-IP default."""

import argparse

import pytest

from aoeo_market import cli_args
from aoeo_market.cli_args import add_login_args, detect_local_ip, resolve_local_ip


def test_local_ip_arg_is_optional():
    p = argparse.ArgumentParser()
    add_login_args(p)
    args = p.parse_args([])
    assert args.local_ip is None
    args = p.parse_args(["--local-ip", "10.1.2.3"])
    assert args.local_ip == "10.1.2.3"


def test_detect_local_ip_loopback():
    # The UDP connect trick against loopback is deterministic: no packets are
    # sent and the kernel reports 127.0.0.1 as the source address.
    assert detect_local_ip("127.0.0.1", 9) == "127.0.0.1"


def test_resolve_local_ip_prefers_explicit_value():
    p = argparse.ArgumentParser()
    assert resolve_local_ip(p, "10.1.2.3") == "10.1.2.3"


def test_resolve_local_ip_uses_detection(monkeypatch):
    monkeypatch.setattr(cli_args, "detect_local_ip", lambda: "192.168.0.17")
    p = argparse.ArgumentParser()
    assert resolve_local_ip(p, None) == "192.168.0.17"


def test_resolve_local_ip_reports_parser_error(monkeypatch):
    def no_route():
        raise OSError("Network is unreachable")

    monkeypatch.setattr(cli_args, "detect_local_ip", no_route)
    p = argparse.ArgumentParser()
    with pytest.raises(SystemExit) as excinfo:
        resolve_local_ip(p, None)
    assert excinfo.value.code == 2


def test_xlive_crc_arg_is_optional():
    p = argparse.ArgumentParser()
    add_login_args(p)
    args = p.parse_args([])
    assert args.xlive_crc is None
    args = p.parse_args(["--xlive-crc", "8ca16109"])
    assert args.xlive_crc == "8ca16109"


def test_resolve_xlive_crc_explicit_value():
    assert cli_args.resolve_xlive_crc("8ca16109") == bytes.fromhex("8ca16109")


def test_resolve_xlive_crc_fetches_manifest(monkeypatch):
    from aoeo_market import auth

    monkeypatch.setattr(auth, "fetch_xlive_crc32", lambda: bytes.fromhex("8ca16109"))
    assert cli_args.resolve_xlive_crc(None) == bytes.fromhex("8ca16109")


def test_resolve_xlive_crc_stops_on_fetch_failure(monkeypatch):
    """A failed manifest fetch raises XliveManifestError instead of guessing
    with a stale captured CRC."""
    from aoeo_market import auth

    def boom():
        raise auth.XliveManifestError("could not fetch or parse the xlive manifest: offline")

    monkeypatch.setattr(auth, "fetch_xlive_crc32", boom)
    with pytest.raises(auth.XliveManifestError, match="--xlive-crc"):
        cli_args.resolve_xlive_crc(None)


def test_resolve_xlive_crc_rejects_bad_hex():
    with pytest.raises(ValueError):
        cli_args.resolve_xlive_crc("zz")
    with pytest.raises(ValueError):
        cli_args.resolve_xlive_crc("8ca161")
