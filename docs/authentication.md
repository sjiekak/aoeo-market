# Authentication — how Spartan logs in

`Spartan.exe` does **not** use the launcher's TLS WebSocket. There are two
separate account systems:

1. **Launcher GUI** → `wss://prod.projectceleste.com:4513/` (TLS WebSocket,
   JSON) — launcher-only features (friends, news). Not used by the game.
2. **Game (`xlive.dll`)** → `51.91.169.108:4564` (plaintext TCP, "Celeste
   Network") — this is how the game authenticates.

The game then uses the returned session to log into **two** more plaintext
services, sending the same credentials bundle to both:

3. **Game service** — `51.91.169.108:1510` (channel 0x0101, opcode 0xF1).
4. **Lobby/realm service** — `51.91.169.108:1500` (channel 0x0028; the 0xF1
   login version byte is 0x01 there instead of 0x02). Returns realm info
   ("Celeste Fan Server" / "Celeste-Production1"). The market client does not
   need it.

## Captures used to verify the constants

| capture | date | machine / local IP | account | purpose |
|---|---|---|---|---|
| `capture_aoeo_login_market_query_towards_server.pcapng` | 2026-08-10 | A / 192.168.1.37 | account 1 | 4564 login + 1510 login + market queries |
| `capture_aoeo_only.pcapng` | 2026-08-13 | B / 192.168.0.17 | account 1 | launcher + 4564 login only |
| `capture_aoeo_login_separate_user_email_password.pcapng` | 2026-08-17 | B / 192.168.0.17 | account 2 | 4564 + 1510 + 1500 logins |

## The 4564 login protocol

The 4564 login is a small length-framed binary protocol. Every packet starts
with the same 8-byte header; all multi-byte integers are little-endian:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        Packet ID (LE)                         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                Total Length (LE, incl. header)                |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                            Body                               |
|                             ...                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### Login request — client → server (id = 1)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         zeros (40 bytes)                      |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x01 |              Version = 2018 (LE, 4 bytes)              |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|       Email length (LE)       |             Email             |
:                            (variable)                         :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|      Password length (LE)     |           Password            |
:                            (variable)                         :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|               opaque (4 bytes, per install)                   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|               Local IPv4 (4 bytes, network order)             |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x40 |              zeros (3 bytes)                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           Device hash (64 ASCII hex chars = 64 bytes)         |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

The email and password are sent **in plaintext**. The 12-byte tail is built by
`build_login_tail(local_ip, opaque)`: 4 opaque bytes, the caller's local IPv4
address (network byte order), then `40 00 00 00`. The `opaque` bytes and the
device hash are per-install values that callers pass explicitly — no function
defaults to them. The local IPv4 is detected from the kernel route when the
CLI's `--local-ip` is omitted (`aoeo_market.cli_args.detect_local_ip`).

**Corrected constants** (what the captures actually show):

| field | machine A (2026-08-10) | machine B (2026-08-13 & -17) | verdict |
|---|---|---|---|
| version | 2018 | 2018 | **constant** |
| tail bytes 0..4 | `45 8e 0d 1e` | `f6 9b 99 1a` | **per install** — *not* a constant `0x45` |
| tail bytes 4..8 | `c0 a8 01 25` = 192.168.1.37 | `c0 a8 00 11` = 192.168.0.17 | caller's local IPv4 (matches packet source) |
| tail bytes 8..12 | `40 00 00 00` | `40 00 00 00` | **constant** |
| device hash | 64 hex chars (value A) | 64 hex chars (value B) | **per install** — same account on both machines, different hash; different accounts on machine B, same hash |

### Login response — server → client (id = 1)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         zeros (8 bytes)                       |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                       spaces (32 bytes)                       |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|0x02|0x0A|                  XUID (8 bytes LE)                  |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|       Profile length (LE)     |          Profile name         |
:                            (variable)                         :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|        Token length (LE)      |         Token (32 chars)      |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     External IP length (LE)   |          External IP          |
:                            (variable)                         :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|      Extra length (LE)        |             Extra             |
:                            (variable)                         :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x01 |
+-+-+-+-+
```

The 4th field is **not** a constant `"None"` (an earlier reading): it is
`"None"` on machine A and `"Summer"` on machine B. The external IP is the
server's view of the client's public address (it differed between the two
machines). Everything else — 8 zeros, 32 spaces, `02 0A`, xuid, token,
trailing `0x01` — matches across all captures.

### Session register — client → server (id = 2)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                       XUID (8 bytes LE)                       |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                    Token (32 ASCII chars)                     |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x2B |
+-+-+-+-+
```

The reply is a **~135 KB JSON game-file manifest** (135715 bytes on 2026-08-10,
135711 on 2026-08-17 — content drifts with the game version, size is stable).

### Other packets the game sends on the same connection

| id | client → server | server reply | when |
|---|---|---|---|
| 3 | `xuid + token + 0x10 + u32` | `xuid + 32 spaces + 0x11 0x01` | keepalive/status, ~22 s after login (both captures) |
| 4, 5 | `xuid + token + 0x0e` | `xuid + 32 spaces + 0x0f 0x15 + u32 len + {"friend-results": …}` | friend-list poll |
| 7 | re-login: `xuid + token + 0x01 + version 2018 + email + password + tail + hash` | same layout as the id-1 response but with the leading 8 zero bytes replaced by the xuid | right before the 1510/1500 logins |

The id-7 re-login returns the **same token** as the id-1 login, confirming the
session flow the client uses: id-1 login → id-2 register → (token) → 1510 login.

## The 1510 / 1500 login

See [protocol.md](protocol.md) — the 0xF1 login frame, its eight-message
bundle (counters 1..8) and the 0xF2 reply are fully reversed and implemented
in `aoeo_market.protocol.build_login_bundle` / `aoeo_market.client.login`,
byte-verified against the 2026-08-17 capture.

## Status

Validated live from the capture machine (2026-08-17): the 4564 login with the
corrected constants was accepted and re-issued the exact token seen in the
2026-08-13 capture, the 1510 login bundle was answered with the 0xF2 reply and
its `<Empire><Offers>` document, and a market sweep returned the full
marketplace (~650 listings). See [live-client.md](live-client.md).

Remaining caveats:

- The tail/hash constants are **per install**: running from another machine
  requires re-capturing a login there and refreshing `LOGIN_TAIL_OPAQUE` /
  `DEVICE_HASH`.
- The game re-logs in (4564 packet 7) shortly before the 1510 login; the
  client currently only does packets 1+2, which the server accepted fine.
