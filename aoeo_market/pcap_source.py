"""Read marketplace listings out of a packet capture.

This is the offline data source: it reassembles the server->client bytes on the
game-service TCP stream, inflates the zlib application messages, and parses the
``MarketPlaceItemInfo`` records. It lets the whole parse/observe pipeline run and
be tested without a live connection.
"""

from __future__ import annotations

import gzip
import os
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


def reassemble_s2c(path: str | os.PathLike, server: str = GAME_SERVER_HOST,
                   port: int = GAME_SERVER_PORT) -> bytes:
    """Concatenate server->client payloads on the game-service stream, ordered by
    TCP sequence number (deduplicating retransmits)."""
    pkts = rdpcap(_maybe_gunzip(path))
    segs: list[tuple[int, bytes]] = []
    for p in pkts:
        if IP in p and TCP in p and Raw in p:
            if p[IP].src == server and p[TCP].sport == port:
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


def listings_from_pcap(path: str | os.PathLike, server: str = GAME_SERVER_HOST,
                       port: int = GAME_SERVER_PORT) -> list[Listing]:
    """All marketplace listings found anywhere in a capture (deduped by tx id)."""
    data = reassemble_s2c(path, server, port)
    merged = b"".join(out for _, out in iter_zlib_members(data))
    by_tx: dict[int, Listing] = {}
    for lst in parse_listings(merged):
        by_tx.setdefault(lst.transaction_id, lst)
    return list(by_tx.values())
