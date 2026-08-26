"""Read marketplace listings out of a packet capture.

This is the offline data source: it reassembles the server->client bytes on the
game-service TCP stream, inflates the zlib application messages, and parses the
``MarketPlaceItemInfo`` records. It lets the whole parse/observe pipeline run and
be tested without a live connection.
"""

from __future__ import annotations

import gzip
import os
import struct
from pathlib import Path

from scapy.all import IP, TCP, Raw, rdpcap

from .constants import GAME_SERVER_HOST, GAME_SERVER_PORT
from .market import Listing, parse_listings
from .protocol import iter_zlib_members


def _maybe_gunzip(path: str | os.PathLike) -> str:
    path = Path(path)
    if path.suffix != ".gz":
        return str(path)
    out = path.with_suffix("")  # drop .gz
    if not out.exists():
        with gzip.open(path, "rb") as f, open(out, "wb") as o:
            o.write(f.read())
    return str(out)


def _reassemble(path: str | os.PathLike, server: str, port: int, *, s2c: bool) -> bytes:
    """Concatenate one direction of the game-service stream, ordered by TCP
    sequence number (deduplicating retransmits)."""
    pkts = rdpcap(_maybe_gunzip(path))
    segs: list[tuple[int, bytes]] = []
    for p in pkts:
        if IP not in p or TCP not in p or Raw not in p:
            continue
        match = (p[IP].src == server and p[TCP].sport == port) if s2c else (p[IP].dst == server and p[TCP].dport == port)
        if match:
            segs.append((p[TCP].seq, bytes(p[Raw].load)))
    segs.sort()
    seen: set[int] = set()
    data = b""
    for seq, payload in segs:
        if seq in seen:
            continue
        seen.add(seq)
        data += payload
    return data


def reassemble_s2c(path: str | os.PathLike, server: str = GAME_SERVER_HOST, port: int = GAME_SERVER_PORT) -> bytes:
    """Concatenate server->client payloads on the game-service stream."""
    return _reassemble(path, server, port, s2c=True)


def reassemble_c2s(path: str | os.PathLike, server: str = GAME_SERVER_HOST, port: int = GAME_SERVER_PORT) -> bytes:
    """Concatenate client->server payloads on the game-service stream."""
    return _reassemble(path, server, port, s2c=False)


def listings_from_pcap(path: str | os.PathLike, server: str = GAME_SERVER_HOST, port: int = GAME_SERVER_PORT) -> list[Listing]:
    """All marketplace listings found anywhere in a capture (deduped by tx id)."""
    data = reassemble_s2c(path, server, port)
    merged = b"".join(out for _, out in iter_zlib_members(data))
    by_tx: dict[int, Listing] = {}
    for lst in parse_listings(merged):
        by_tx.setdefault(lst.transaction_id, lst)
    return list(by_tx.values())


# A market query frame: context(8)=0, channel 0x0032, declared length 0x002c
# (44-byte payload), opcode 0x000000AB, then the counter byte + 44-byte body.
_MARKET_QUERY_MARK = b"\x00\x32\x00\x2c\x00\x00\x00\xab"


def market_queries_from_pcap(path: str | os.PathLike, server: str = GAME_SERVER_HOST, port: int = GAME_SERVER_PORT) -> list[tuple[int, ...]]:
    """Every 0xAB market query's nine selector words, in wire order.

    The query body is 44 bytes (8-byte sequence field + nine 32-bit selectors);
    only the selectors are returned.  This is the client->server counterpart of
    :func:`listings_from_pcap`.
    """
    data = reassemble_c2s(path, server, port)
    out: list[tuple[int, ...]] = []
    i = 0
    while True:
        j = data.find(_MARKET_QUERY_MARK, i)
        if j < 0:
            return out
        body = data[j + len(_MARKET_QUERY_MARK) + 1 : j + len(_MARKET_QUERY_MARK) + 1 + 44]
        if len(body) == 44:
            out.append(struct.unpack("<9I", body[8:]))
        i = j + 1
