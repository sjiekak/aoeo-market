# Authentication — how Spartan logs in

`Spartan.exe` does **not** use the launcher's TLS WebSocket. There are two
separate account systems:

1. **Launcher GUI** → `wss://prod.projectceleste.com:4513/` (TLS WebSocket,
   JSON) — launcher-only features (friends, news). Not used by the game.
2. **Game (`xlive.dll`)** → `51.91.169.108:4564` (plaintext TCP, "Celeste
   Network") — this is how the game authenticates.

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

- **Packet ID** — `1` for login, `2` for session register, …
- **Total Length** — the length of the entire packet, including these 8 header
  bytes.

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
| 0x45 |              opaque (3 bytes)                          |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|               Local IPv4 (4 bytes, network order)             |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x40 |              zeros (3 bytes)                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           Device hash (64 ASCII hex chars = 64 bytes)         |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

The email and password are sent **in plaintext**. The 12-byte tail is built
by `build_login_tail(local_ip)`: `0x45`, three opaque bytes, the caller's
local IPv4 address (network byte order), `0x40`, then three zero bytes. The
opaque bytes are stable per install and are replayed verbatim.

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
|       "None" length (LE)      |             "None"            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| 0x01 |
+-+-+-+-+
```

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

The reply to the session register is a **~135 KB JSON game-file manifest**
(not diagrammed).

The returned `xuid` / `username` / `token` are then fed into the 1510
game-service login (`client.MarketClient.login`, channel 0x0101 opcode 0xF1).

## Status

`auth.py` implements the 4564 login and is unit-tested against the captured
login bytes. What remains is validating the full round-trip against the live
servers (no live run has been performed from this environment):

1. **Live-test the 4564 login** — `MarketClient.acquire_session(mail, password,
   local_ip)` should return `xuid`/`username`/`token`. The login tail is built
   from `local_ip`; the device hash is still replayed from the capture, so if
   the server rejects it that blob needs regenerating.
2. **Complete the 1510 login handshake** — the short control exchange around the
   `0xF1` frame (opcodes `0xf2/0xff/0xfe/0x91/0x92` appear in the login capture)
   is still only partially reversed in `client.login()`. Finish from a fresh
   capture if the server does not accept the bare `0xF1` frame.
