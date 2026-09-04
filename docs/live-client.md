# Live client — status

`aoeo_market/client.py` implements connect / framing / login bundle / market
query / response parse / observe loop. The login path was fully reversed from
two independent captures (2026-08-10 and 2026-08-17) and **validated live**
from the capture machine (2026-08-17):

```
$ uv run python -m aoeo_market.live_probe --game
OK - 4564 login accepted
  xuid        = <64-bit account id>
  username    = <profile name>
  token       = <32-char session token>   # same token as the Aug-13 capture
  external_ip = <public IP as seen by the server>
  sent 0xF1 login bundle (8 messages, counters 1..8)
  server reply: … bytes (0xF2 received: True)
```

Followed by a live market sweep: the default captured query shapes returned a
657-listing snapshot and the observer emitted 657 `LISTED` events, with a
second poll correctly reporting 0 changes.

What the live run confirmed:

- `MarketClient.acquire_session(mail, password, local_ip, device_hash=...,
  xlive_crc32=...)` — the plaintext TCP 4564 "Celeste Network" login works
  with the per-install `DEVICE_HASH` and the CRC-32 of the installed
  xlive.dll from `aoeo_market/auth.py` (`fetch_xlive_crc32`, the CLI default
  behind `--xlive-crc`; nothing is inferred in the client); the server
  re-issued the exact token seen in the 2026-08-13 capture.
- `MarketClient.login(session)` — the TCP 1510 login: 0xF1 frame + the
  eight-message bundle (counters 1..8), byte-identical to what the game
  sends; the server answered 0xF2 with the `02 01` status prefix and the
  initial `<Empire><Offers>` document.
- `MarketClient.poll_once()` — the captured 42-query selector sweep returns
  the whole market across all six categories (an all-wildcard query is *not*
  answered), and the UTF-8/UTF-16 XML parsing + observer diffing behave as
  designed.

Re-validated live on 2026-09-04 after the 2026-09-03 server maintenance: the
wire format is unchanged, but the maintenance shipped a new xlive.dll build
(1.0.0.106). Its CRC-32 (`0x0961A18C` → little-endian `8ca16109`, published
in `https://downloads.projectceleste.com/game_files/xlive.json`) and the new
`DEVICE_HASH` (`1cb498f3…`) plus the current account password log in cleanly
(4564 re-issues the official client's token, the 1510 handshake returns the
full 0xF2 bundle); the pre-upgrade values are rejected with an empty-session
frame even with the correct password, and the stale secret-store password is
rejected with the new values. The install-signature CRC is now fetched from
the manifest on every run, so it tracks future xlive.dll builds by itself.

Rejection handling:

- a rejected 4564 login (empty session: `xuid == 0`, no token, server FIN)
  raises `aoeo_market.auth.LoginRejected` before the session register is
  sent; `probe` reports `FAILED - 4564 login rejected` and exits 1, and
  `fetch` reports `error: login rejected`;
- a 1510 0xF2 reply whose payload starts with `0x00` (accepted logins start
  with `0x01`) is also surfaced as `LoginRejected`.

Remaining caveats:

- The device hash is **per install**. Running from another machine requires
  re-capturing a login there and passing the new value via `--device-hash`
  (or refreshing `DEVICE_HASH`, which the CLI defaults to). A future client
  update may change it again — re-capture and refresh.
- The xlive CRC-32 needs no capture: `--xlive-crc` defaults to the value in
  the live manifest (`fetch_xlive_crc32`); pass an explicit hex value to
  override (e.g. for replay tests). If the manifest cannot be fetched the
  command stops with an error rather than sending a stale CRC — a stale CRC
  reads exactly like a rejected login.
- The account password in the secret store (`secret-tool lookup
  login.password aoeo.market`) must match the real account password.
- The game re-logs in (4564 packet 7) shortly before the 1510 login; the
  client currently only does packets 1+2, which the server accepted fine.

## CLI

The live path is exposed as a command-line command:

```
$ uv run python -m aoeo_market.cli fetch
Logging in over Celeste Network 51.91.169.108:4564 ...
657 active listings

ITEM_ID                      TYPE      LVL CNT    PRICE EXPIRES(d)  SELLER
...                          Trait      43   1    99000       30.0  4072340471133720139

$ uv run python -m aoeo_market.cli fetch --watch
... same table, then ...
[12:00:30] LISTED   tx=... ItemID @ 12345
[12:01:00] REMOVED  tx=... ItemID -> EXPIRED   # vanished with <1 day left
[12:02:00] REMOVED  tx=... ItemID -> REMOVED   # sold or withdrawn, indistinguishable
```

Credentials come from `--email`/`--password`, the `AOEO_EMAIL`/`AOEO_PASSWORD`
environment variables, or an interactive prompt. The local IPv4 address is
auto-detected from the kernel route and used as the default; pass
`--local-ip <ip>` to override it. `--device-hash` (64 hex chars) defaults to
the captured per-install value (`auth.DEVICE_HASH`); `--xlive-crc` (4 hex
bytes) defaults to the CRC-32 published in the live xlive.json manifest
(`auth.fetch_xlive_crc32`) — pass them explicitly when running from a
different machine or to replay a specific build.

## Operational note

The backend may permit only one live session per account. Run the game **or**
this poller, not both — ideally give the poller its own account.
