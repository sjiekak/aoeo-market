"""Unit tests for the CLI printers and commands — no network or captures required."""

from aoeo_market import store
from aoeo_market.cli import _print_event, _print_listings, main
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


def test_init_db_command_creates_and_is_idempotent(tmp_path, capsys):
    db = tmp_path / "market.db"
    assert main(["init-db", "--db", str(db)]) == 0
    assert db.exists()
    conn = store.open_store(db, read_only=False)
    assert store.snapshot_count(conn) == 0
    conn.close()
    # re-running is safe and reports the existing (empty) state
    assert main(["init-db", "--db", str(db)]) == 0
    assert "0 snapshots present" in capsys.readouterr().out


def test_probe_reports_rejected_login(monkeypatch, capsys):
    """A rejected 4564 login makes `probe` fail with a clear message."""
    from types import SimpleNamespace

    from aoeo_market import auth as auth_mod
    from aoeo_market.cli import _probe

    class _RejectingClient:
        manifest_received = False

        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            raise auth_mod.LoginRejected("test rejection")

        def close(self):
            pass

    monkeypatch.setattr(auth_mod, "CelesteNetworkClient", _RejectingClient)
    monkeypatch.setattr("aoeo_market.cli_args.resolve_xlive_crc", lambda value: bytes.fromhex("8ca16109"))
    args = SimpleNamespace(
        local_ip="192.168.0.17",
        email="a@b.co",
        password="pw",
        host="51.91.169.108",
        port=4564,
        timeout=5.0,
        game=False,
        device_hash=auth_mod.DEVICE_HASH,
        xlive_crc=None,
    )
    assert _probe(args) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "rejected" in err


def test_live_commands_stop_when_xlive_crc_unresolvable(monkeypatch, capsys):
    """A manifest fetch failure stops the command (exit 2) instead of
    guessing with a stale captured CRC."""
    from types import SimpleNamespace

    from aoeo_market import auth as auth_mod
    from aoeo_market import cli as cli_mod
    from aoeo_market.cli import _login_identity

    def boom(value):
        raise auth_mod.XliveManifestError("could not fetch or parse the xlive manifest: offline")

    monkeypatch.setattr(cli_mod, "resolve_xlive_crc", boom)
    args = SimpleNamespace(device_hash=auth_mod.DEVICE_HASH, xlive_crc=None)
    assert _login_identity(args) == 2
    err = capsys.readouterr().err
    assert "error" in err
    assert "xlive manifest" in err
