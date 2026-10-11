"""Offline sanity checks for a raw Bitcoin transaction.

We parse the transaction locally before anything touches the network so that
(1) typos fail fast without a mixnet round trip, and (2) we can compute the
txid ourselves and compare it with what the API answers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


class TxError(ValueError):
    pass


@dataclass
class TxInfo:
    txid: str
    wtxid: str
    size: int
    vsize: int
    segwit: bool
    n_inputs: int
    n_outputs: int
    output_sats: int


def _dsha(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class _Reader:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise TxError("transaction is truncated")
        out = self.b[self.i : self.i + n]
        self.i += n
        return out

    def varint(self) -> int:
        first = self.take(1)[0]
        if first < 0xFD:
            return first
        size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
        return int.from_bytes(self.take(size), "little")


def clean_hex(raw: str) -> bytes:
    s = re.sub(r"\s+", "", raw)
    if not s:
        raise TxError("empty transaction")
    if len(s) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", s):
        raise TxError("transaction must be an even-length hex string")
    return bytes.fromhex(s)


def parse(raw: str | bytes) -> TxInfo:
    b = clean_hex(raw) if isinstance(raw, str) else raw
    r = _Reader(b)
    version = r.take(4)
    segwit = False
    if r.b[r.i : r.i + 1] == b"\x00":
        marker_flag = r.take(2)
        if marker_flag[1] != 0x01:
            raise TxError("bad segwit marker/flag")
        segwit = True
    body_start = r.i
    n_in = r.varint()
    if n_in == 0:
        raise TxError("transaction has no inputs")
    for _ in range(n_in):
        r.take(36)  # outpoint
        r.take(r.varint())  # scriptSig
        r.take(4)  # sequence
    n_out = r.varint()
    if n_out == 0:
        raise TxError("transaction has no outputs")
    total = 0
    for _ in range(n_out):
        total += int.from_bytes(r.take(8), "little")
        r.take(r.varint())
    body_end = r.i
    if segwit:
        for _ in range(n_in):
            for _ in range(r.varint()):
                r.take(r.varint())
    locktime = r.take(4)
    if r.i != len(b):
        raise TxError(f"{len(b) - r.i} trailing bytes after transaction")
    stripped = version + b[body_start:body_end] + locktime
    weight = len(stripped) * 3 + len(b)
    return TxInfo(
        txid=_dsha(stripped)[::-1].hex(),
        wtxid=_dsha(b)[::-1].hex(),
        size=len(b),
        vsize=(weight + 3) // 4,
        segwit=segwit,
        n_inputs=n_in,
        n_outputs=n_out,
        output_sats=total,
    )
