"""Esplora / mempool.space REST calls, all made through the Nym mixnet."""

from __future__ import annotations

import concurrent.futures
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

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
    # nymdial transport: the exit this POST left from (a fresh mixnet client per POST)
    exit: Optional[dict] = None
    # seconds waited before posting (--delay)
    delay: float = 0.0


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


def parse_delay(spec: Optional[str]) -> Optional[tuple[float, float]]:
    """``"30-120"`` -> (30.0, 120.0) seconds; ``"45"`` -> (45.0, 45.0); empty/None -> None."""
    if spec is None or str(spec).strip() in ("", "0"):
        return None
    lo, sep, hi = str(spec).strip().partition("-")
    try:
        a, b = float(lo), float(hi if sep else lo)
    except ValueError:
        raise ValueError(f"--delay wants MIN-MAX seconds, e.g. 30-300 (got {spec!r})") from None
    if a < 0 or b < a or b > 86400:
        raise ValueError(f"--delay wants 0 <= MIN <= MAX <= 86400 seconds (got {spec!r})")
    return a, b


_SYSRANDOM = random.SystemRandom()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _broadcast_one(
    client: MixnetClient,
    base: str,
    raw_hex: str,
    txid: str,
    delay: float = 0.0,
    sleep: Optional[Callable[[float], None]] = None,
    note: Optional[Callable[[str], None]] = None,
) -> BroadcastResult:
    if delay > 0:
        if note:
            note(f"{base}: waiting {delay:.1f}s before posting")
        (sleep or _sleep)(delay)
    try:
        r = client.post(base + "/tx", raw_hex.encode(), headers={"Content-Type": "text/plain"})
    except MixnetError as e:
        return BroadcastResult(base, False, None, f"no response over mixnet: {e}", None, None, delay)
    body = r.text.strip()
    accepted = r.ok and body.lower() == txid
    if r.ok and not accepted:
        body = f"unexpected 2xx body (expected our txid): {body[:200]}"
    exit_info = None
    if r.exit is not None:
        exit_info = {"node_id": r.exit.node_id, "country": r.exit.country, "ips": r.exit.ips, "label": r.exit.label}
    return BroadcastResult(base, accepted, r.status, body[:500], r.elapsed, exit_info, delay)


def broadcast(
    client: MixnetClient,
    endpoints: list[str],
    raw_hex: str,
    parallel: bool = True,
    delay: Optional[tuple[float, float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    rng: Optional[random.Random] = None,
    note: Optional[Callable[[str], None]] = None,
) -> tuple[txmod.TxInfo, list[BroadcastResult]]:
    """Parse ``raw_hex`` locally, then POST it to every endpoint over the mixnet.

    Each endpoint gets its own mixnet stream; on the nymdial transport that is
    a fresh mixnet client (new identity, own exit) per endpoint and per
    broadcast, never reused. Sending the same transaction to several
    independent backends is harmless and makes propagation more robust if one
    API is down or filters the exit.

    ``delay=(lo, hi)``: before posting, each endpoint waits its own random
    time, drawn uniformly from [lo, hi] seconds with the OS random source, so
    the moment a transaction appears is decoupled from when it was signed.
    """
    info = txmod.parse(raw_hex)
    clean = txmod.clean_hex(raw_hex).hex()
    rng = rng or _SYSRANDOM
    waits = [rng.uniform(*delay) if delay else 0.0 for _ in endpoints]
    if not parallel or len(endpoints) == 1:
        return info, [_broadcast_one(client, e, clean, info.txid, w, sleep, note) for e, w in zip(endpoints, waits)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as ex:
        futs = [ex.submit(_broadcast_one, client, e, clean, info.txid, w, sleep, note) for e, w in zip(endpoints, waits)]
        return info, [f.result() for f in futs]


def exit_ip(client: MixnetClient, with_exit: bool = False):
    """Ask a public echo service which IP it sees. Proves we are not on clearnet.

    Returns ``(url, ip)``, or ``(url, ip, exit)`` with ``with_exit=True``, where
    ``exit`` is the nymdial exit the request left from (None on socks5).
    """
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = client.get(url)
        except MixnetError:
            continue
        if r.ok:
            return (url, r.text.strip(), r.exit) if with_exit else (url, r.text.strip())
    raise MixnetError("no IP echo service answered over the mixnet")
