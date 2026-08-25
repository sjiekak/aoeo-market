# Marketplace protocol (decoded from captures)

The game client talks to the game server (`51.91.169.108`) over **TCP 1510**, a
custom binary protocol. (TCP 4513 is a separate TLS channel used by the
launcher GUI; TCP 1500 is the lobby/realm service; UDP 900 is chat.)

## Frame format

Header fields are big-endian:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                 Context / session id (8 bytes)                |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         Channel (BE)          |      Payload length (BE)      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   Flags (1B)  |                Opcode (3B, BE)                |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   Counter (1B)    |            Payload (variable)             |
+-+-+-+-+-+-+-+-+-+-+-+-+                                       :
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

- The 8-byte context id is zero before and after login in every capture.
- `channel 0x0032` = game service; `channel 0x0101` = login; the lobby/realm
  service (TCP 1500) uses `channel 0x0028`.
- The **counter** byte is a per-connection correlation id: it starts at 1 in
  the login bundle, increments on every client message, and is **echoed** by
  the server's reply (0xFE/0x92/0x1D/0x62 reply to counters 1..4; ping 0x7E
  with counter N is answered by 0x7F with counter N).
- The 4-byte opcode field splits into 1 flag byte + 3 opcode bytes. Flag 0 =
  plain; data messages that embed a zlib member use flag 2 (client→server,
  e.g. `02 00 00 57`) or flag 1 (server→client, e.g. `01 00 00 62`).
- **The 16-bit length is not reliable for framing.** It records only the part
  of the payload the game had buffered when it wrote the header. Data
  messages carry their own `[u32 inflated size][u32 deflated size][zlib]`
  inside the payload and continue past the declared length; the login frame's
  declared length covers its prefix plus the first 255 bytes of the embedded
  message stream, which then continues in later TCP segments. Parsers must
  either follow the payload structure (the u32 size fields) or scan the raw
  byte stream for complete zlib members (`protocol.iter_zlib_members`).

## Login (channel 0x0101)

`opcode 0xF1` — the login frame. Its payload starts with
`version(1B = 0x02) • xuid(8B LE) • len-prefixed username • len-prefixed
32-char token` and is followed by a **message bundle** sent with counters
1..8 (each message is a normal frame with a zero context):

| ctr | ch     | op   | payload                                                        |
|-----|--------|------|----------------------------------------------------------------|
| 1   | 0x0000 | 0xFF | 16-byte constant blob `04 00*12 04 18 00 00` (same in both captures) |
| 2   | 0x0032 | 0x91 | `00`                                                           |
| 3   | 0x0032 | 0x1C | `u32 utf16-len` + UTF-16 profile name + xuid                   |
| 4   | 0x0032 | 0x61 | xuid                                                           |
| 5   | 0x0032 | 0xBE | empty                                                          |
| 6   | 0x0032 | 0x55 | xuid                                                           |
| 7   | 0x0032 | 0xAD | xuid                                                           |
| 8   | 0x0032 | 0x57 | xuid + `u32 inflated` + `u32 deflated` + zlib `<Settings>` XML |

The server answers `opcode 0xF2` (prefix `02 01`) with replies echoing the
counters: `0xFE` (ctr 1), `0x92` (ctr 2), `0x1D` (ctr 3), and `0x62` (ctr 4)
whose payload is `xuid + u32 inflated + u32 deflated + zlib` — the initial
**market offers** document (`<?xml …><Empire><Offers>…`, UTF-16-LE with BOM).
This sequence is byte-identical across the 2026-08-10 and 2026-08-17 captures
except for the account fields (xuid/name/token) and the settings document.

## Application messages

Application messages are zlib streams (`0x78 0x9c` / `0x78 0xda`) whose inflated
content is XML — either UTF-8 (the offline market captures) or UTF-16-LE with a
BOM (the live login's offers document; `market.parse_listings` handles both).
Messages are split across frames and even across the declared frame lengths,
so we reassemble by scanning the decoded byte stream for complete zlib members
rather than relying on frame boundaries (`protocol.iter_zlib_members`).

## Marketplace record

`market.Listing`:

```xml
<MarketPlaceItemInfo SellerEmpireId="...">
  <TransactionId>...</TransactionId>       <!-- stable per-listing primary key -->
  <BuyerCharacterId>-1</BuyerCharacterId>  <!-- always -1 in the public browse -->
  <ItemID>ArmorPlt_Halloween2025</ItemID>
  <ItemType>Trait|Advisor|Material|Design|Blueprint|Consumable</ItemType>
  <ItemLevel/> <ItemCount/> <ItemPrice/> <ItemSeed/>
  <SecondsTillExpiry>...</SecondsTillExpiry> <!-- countdown; max 30 days -->
</MarketPlaceItemInfo>
```

## Market browse query

`opcode 0xAB` — **market browse query**. The payload is an 8-byte little-endian
sequence field followed by **nine** 32-bit little-endian filter words;
`0xFFFFFFFF` = wildcard. An all-wildcard query is **not** answered, so the
client enumerates the market with one query per (category, sub-filter) shape.

The first word (`word[0]`) is the top-level category:

| word[0] | category | wire `ItemType` |
|---|---|---|
| 1 | materials | `Material` |
| 2 | blueprints | `Blueprint` |
| 3 | gear | `Trait` |
| 4 | recipes (designs) | `Design` |
| 6 | advisors | `Advisor` |
| 9 | consumables | `Consumable` |

Gear (category 3) is the only category with sub-filters: its sixth word
(`word[5]`) selects the gear type, one query per type in alphabetical order —
the name → id mapping lives in `aoeo_market.protocol.GEAR_TYPE_SELECTORS`. The
complete 42-query sweep (`protocol.DEFAULT_MARKET_SWEEP`) replays exactly what
the game sent while browsing every category (capture
`capture_aoeo_login_market_iterate_over_several_listings.pcapng`, 2025-08-25).
