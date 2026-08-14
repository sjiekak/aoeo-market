# Marketplace protocol (decoded from captures)

The game client talks to the game server (`51.91.169.108`) over **TCP 1510**, a
custom binary protocol. (TCP 4513 is a separate TLS channel; UDP 900 is chat.)

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
|                         Opcode (BE)                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      Payload (variable)                       |
:                                                               :
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
| Trailer |
+-+-+-+-+-+-+
```

- `channel 0x0032` = game service; `channel 0x0101` = login.
- `opcode 0x7E` = keepalive/ping (payload starts with an 8-byte little-endian
  global sequence counter).
- `opcode 0xF1` = login (channel 0x0101): `version • xuid(8B LE) •
  len-prefixed username • len-prefixed 32-char token • …`.
- `opcode 0xAB` = **market browse query**. Payload is the sequence counter
  followed by 32-bit little-endian filter words; `0xFFFFFFFF` = wildcard. The
  client issues several queries iterating category/rarity/level tuples — a
  broadly-wildcarded query is expected to return the whole market.

## Application messages

Application messages are zlib streams (`0x78 0x9c` / `0x78 0xda`) whose inflated
content is XML. Large messages are split across frames, so we reassemble by
scanning the decoded byte stream for complete zlib members rather than relying
on frame boundaries (`protocol.iter_zlib_members`).

## Marketplace record

`market.Listing`:

```xml
<MarketPlaceItemInfo SellerEmpireId="...">
  <TransactionId>...</TransactionId>       <!-- stable per-listing primary key -->
  <BuyerCharacterId>-1</BuyerCharacterId>  <!-- always -1 in the public browse -->
  <ItemID>ArmorPlt_Halloween2025</ItemID>
  <ItemType>Trait|Advisor|Material|Design</ItemType>
  <ItemLevel/> <ItemCount/> <ItemPrice/> <ItemSeed/>
  <SecondsTillExpiry>...</SecondsTillExpiry> <!-- countdown; max 30 days -->
</MarketPlaceItemInfo>
```
