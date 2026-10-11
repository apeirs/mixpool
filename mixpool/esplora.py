"""Esplora / mempool.space REST calls, all made through the Nym mixnet."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Optional

from . import tx as txmod
from .mixhttp import MixnetClient, MixnetError

NETWORKS: dict[str, list[str]] = {
    "mainnet": [
        "https://mempool.space/api",
        "https://blockstream.info/api",
        "https://mempool.emzy.de/api",
    ],
    "testnet4": ["https://mempool.space/testnet4/api"],
    "testnet": ["https://blockstream.info/testnet/api", "https://mempool.space/testnet/api"],
    "signet": ["https://mempool.space/signet/api", "https://blockstream.info/signet/api"],
}


@dataclass
class BroadcastResult:
    endpoint: str
    accepted: bool
    status: Optional[int]
    message: str
    elapsed: Optional[float]


def endpoints_for(network: str, override: Optional[list[str]] = None) -> list[str]:
    if override:
        return [e.rstrip("/") for e in override]
    try:
        return NETWORKS[network]
    except KeyError:
        raise ValueError(f"unknown network {network!r}; choose from {', '.join(NETWORKS)}") from None


def _first_ok(client: MixnetClient, endpoints: list[str], paths: list[str]):
    errors = []
    for base in endpoints:
        for path in paths:
            try:
                r = client.get(base + path)
            except MixnetError as e:
                errors.append(f"{base}{path}: {e}")
                break  # endpoint unreachable, try the next one
            if r.ok:
                return base, path, r
            errors.append(f"{base}{path}: HTTP {r.status} {r.text[:120]}")
    raise MixnetError("all endpoints failed: " + " | ".join(errors))


def tip_height(client: MixnetClient, endpoints: list[str]) -> tuple[str, int]:
    base, _, r = _first_ok(client, endpoints, ["/blocks/tip/height"])
    return base, int(r.text.strip())


def fees(client: MixnetClient, endpoints: list[str]) -> tuple[str, dict]:
    """Recommended fee rates in sat/vB.

    mempool.space style ``/v1/fees/recommended`` first, falling back to the
    plain Esplora ``/fee-estimates`` (target blocks -> sat/vB).
    """
    base, path, r = _first_ok(client, endpoints, ["/v1/fees/recommended", "/fee-estimates"])
    data = r.json()
    if path == "/fee-estimates":
        data = {
            "fastestFee": data.get("1"),
            "halfHourFee": data.get("3"),
            "hourFee": data.get("6"),
            "economyFee": data.get("144"),
        }
    return base, data


def tx_status(client: MixnetClient, endpoints: list[str], txid: str) -> tuple[str, dict]:
    txid = txid.strip().lower()
    if len(txid) != 64 or any(c not in "0123456789abcdef" for c in txid):
        raise ValueError("txid must be 64 hex characters")
    base, _, r = _first_ok(client, endpoints, [f"/tx/{txid}/status"])
    return base, r.json()


def _broadcast_one(client: MixnetClient, base: str, raw_hex: str, txid: str) -> BroadcastResult:
    try:
        r = client.post(base + "/tx", raw_hex.encode(), headers={"Content-Type": "text/plain"})
    except MixnetError as e:
        return BroadcastResult(base, False, None, f"no response over mixnet: {e}", None)
    body = r.text.strip()
    accepted = r.ok and body.lower() == txid
    if r.ok and not accepted:
        body = f"unexpected 2xx body (expected our txid): {body[:200]}"
    return BroadcastResult(base, accepted, r.status, body[:500], r.elapsed)


def broadcast(
    client: MixnetClient, endpoints: list[str], raw_hex: str, parallel: bool = True
) -> tuple[txmod.TxInfo, list[BroadcastResult]]:
    """Parse ``raw_hex`` locally, then POST it to every endpoint over the mixnet.

    Each endpoint gets its own mixnet stream. Sending the same transaction
    to several independent backends is harmless and makes propagation more
    robust if one API is down or filters the exit.
    """
    info = txmod.parse(raw_hex)
    clean = txmod.clean_hex(raw_hex).hex()
    if not parallel or len(endpoints) == 1:
        return info, [_broadcast_one(client, e, clean, info.txid) for e in endpoints]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as ex:
        futs = [ex.submit(_broadcast_one, client, e, clean, info.txid) for e in endpoints]
        return info, [f.result() for f in futs]


def exit_ip(client: MixnetClient) -> tuple[str, str]:
    """Ask a public echo service which IP it sees. Proves we are not on clearnet."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = client.get(url)
        except MixnetError:
            continue
        if r.ok:
            return url, r.text.strip()
    raise MixnetError("no IP echo service answered over the mixnet")
