"""mixpool command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

from . import __version__, esplora, nymclient
from . import tx as txmod
from .mixhttp import MixnetClient, MixnetError
from .socks import ProxyUnreachable

_T0 = time.monotonic()


def _trace(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:7.2f}s] {msg}", file=sys.stderr, flush=True)


def _proxy(args) -> str:
    if args.proxy:
        return args.proxy
    if os.environ.get("MIXPOOL_PROXY"):
        return os.environ["MIXPOOL_PROXY"]
    try:
        return nymclient.read_config(args.id)["core"]["socks5"]["bind_address"]
    except (OSError, KeyError):
        return "127.0.0.1:1080"


def _client(args) -> MixnetClient:
    return MixnetClient(proxy=_proxy(args), timeout=args.timeout, retries=args.retries, dns=args.dns,
                        trace=_trace if args.verbose else None)


def _out(args, obj, human: str) -> None:
    print(json.dumps(obj, indent=2) if args.json else human)


def _header(args) -> None:
    if not args.verbose:
        return
    try:
        st = nymclient.status(args.id)
        _trace(f"nym  socks5 client '{st['id']}' pid={st['pid']} listening {st['socks5']}")
        _trace(f"nym  our mixnet address: {st['client_address']}")
        _trace(f"nym  entry gateway: {st['gateway']}")
        _trace(f"nym  exit (network requester): {st['provider']}")
        _trace(f"nym  anonymous replies (SURBs): {st['anonymous_replies']}")
    except OSError:
        _trace(f"nym  using SOCKS5 proxy {_proxy(args)} (no managed client '{args.id}')")


# -- commands -------------------------------------------------------------
def cmd_exits(args):
    provs = nymclient.list_exit_providers(min_performance=args.min_performance)
    provs.sort(key=lambda p: -p.performance)
    if args.json:
        print(json.dumps([asdict(p) for p in provs], indent=2))
        return 0
    for p in provs[: args.limit]:
        print(f"{p.node_id:5} {p.country or '--':3} perf={p.performance:.2f} v{p.version}  {p.address}")
    print(f"({len(provs)} active exit gateways with a network requester)", file=sys.stderr)
    return 0


def cmd_setup(args):
    binary = nymclient.find_binary(args.binary)
    cfg = nymclient.config_path(args.id)
    if cfg.exists() and not args.force:
        print(f"client '{args.id}' already initialised ({cfg}); use --force to re-init", file=sys.stderr)
    else:
        if args.force and nymclient.running_pid(args.id):
            nymclient.stop(args.id)
        provider = args.provider
        if provider == "auto":
            p = nymclient.pick_provider(nymclient.list_exit_providers())
            print(f"exit gateway: node {p.node_id} ({p.country}) perf={p.performance:.2f}", file=sys.stderr)
            provider = p.address
        if args.force and cfg.exists():
            import shutil

            shutil.rmtree(cfg.parent.parent)
        nymclient.init(binary, args.id, provider, args.port, anonymous=not args.no_anonymous, gateway=args.gateway)
        print(f"initialised '{args.id}' -> {provider}", file=sys.stderr)
    return cmd_start(args)


def cmd_start(args):
    binary = nymclient.find_binary(args.binary)
    st = nymclient.start(binary, args.id)
    _out(args, st, "\n".join(f"{k:18} {v}" for k, v in st.items()))
    return 0


def cmd_stop(args):
    print("stopped" if nymclient.stop(args.id) else "not running")
    return 0


def cmd_status(args):
    st = nymclient.status(args.id)
    _out(args, st, "\n".join(f"{k:18} {v}" for k, v in st.items()))
    return 0


def cmd_ip(args):
    _header(args)
    url, ip = esplora.exit_ip(_client(args))
    _out(args, {"service": url, "seen_ip": ip}, f"{url} sees this request coming from {ip} (a Nym exit gateway)")
    return 0


def cmd_tip(args):
    _header(args)
    base, h = esplora.tip_height(_client(args), esplora.endpoints_for(args.network, args.endpoint))
    _out(args, {"endpoint": base, "height": h}, f"{h}    ({base}, via Nym mixnet)")
    return 0


def cmd_fees(args):
    _header(args)
    base, fees = esplora.fees(_client(args), esplora.endpoints_for(args.network, args.endpoint))
    human = "\n".join(f"{k:12} {v} sat/vB" for k, v in fees.items()) + f"\n({base}, via Nym mixnet)"
    _out(args, {"endpoint": base, "fees": fees}, human)
    return 0


def cmd_status_tx(args):
    _header(args)
    base, st = esplora.tx_status(_client(args), esplora.endpoints_for(args.network, args.endpoint), args.txid)
    if st.get("confirmed"):
        human = f"confirmed in block {st.get('block_height')} ({st.get('block_hash')})"
    else:
        human = "unconfirmed (in mempool)"
    _out(args, {"endpoint": base, "status": st}, f"{human}    ({base}, via Nym mixnet)")
    return 0


def _read_tx(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if os.path.isfile(arg):
        with open(arg) as f:
            return f.read()
    return arg


def cmd_decode(args):
    info = txmod.parse(_read_tx(args.tx))
    _out(args, asdict(info), "\n".join(f"{k:12} {v}" for k, v in asdict(info).items()))
    return 0


def cmd_broadcast(args):
    raw = _read_tx(args.tx)
    info = txmod.parse(raw)  # fail fast, offline
    endpoints = esplora.endpoints_for(args.network, args.endpoint)
    print(f"txid {info.txid}  ({info.vsize} vB, {info.n_inputs} in / {info.n_outputs} out)", file=sys.stderr)
    if args.dry_run:
        print("dry run: not sending", file=sys.stderr)
        return 0
    _header(args)
    _, results = esplora.broadcast(_client(args), endpoints, raw, parallel=not args.sequential)
    if args.json:
        print(json.dumps({"txid": info.txid, "results": [asdict(r) for r in results]}, indent=2))
    else:
        for r in results:
            mark = "ACCEPTED" if r.accepted else "REJECTED" if r.status else "NO REPLY"
            t = f"{r.elapsed:.2f}s" if r.elapsed is not None else "-"
            print(f"{mark:9} {r.endpoint}  HTTP {r.status}  {t}  {r.message}")
    return 0 if any(r.accepted for r in results) else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mixpool", description="Bitcoin transaction broadcast and mempool lookups through the Nym mixnet.")
    p.add_argument("--version", action="version", version=f"mixpool {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--id", default=nymclient.DEFAULT_ID, help="nym-socks5-client id (default: mixpool)")
    common.add_argument("--proxy", help="SOCKS5 address of a running nym-socks5-client (default: from the client config, else 127.0.0.1:1080)")
    common.add_argument("--network", default="mainnet", choices=list(esplora.NETWORKS))
    common.add_argument("--endpoint", action="append", help="Esplora API base URL (repeatable); overrides the network's defaults")
    common.add_argument("--timeout", type=float, default=45.0, help="per-attempt timeout in seconds (default 45)")
    common.add_argument("--retries", type=int, default=2)
    common.add_argument("--dns", choices=["doh", "exit"], default="doh", help="doh: resolve via DNS-over-HTTPS over the mixnet, connect by IPv4 (default); exit: let the exit gateway resolve")
    common.add_argument("-v", "--verbose", action="store_true", help="trace the mixnet path and timings on stderr")
    common.add_argument("--json", action="store_true")
    common.add_argument("--binary", help="path to nym-socks5-client")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", parents=[common], help="init + start a nym-socks5-client on a random active exit gateway")
    s.add_argument("--provider", default="auto", help="network requester address, or 'auto'")
    s.add_argument("--gateway", help="entry gateway identity (default: chosen by nym)")
    s.add_argument("--port", type=int, default=nymclient.DEFAULT_PORT)
    s.add_argument("--no-anonymous", action="store_true", help="don't use reply SURBs (faster; the exit then learns our mixnet address)")
    s.add_argument("--force", action="store_true", help="wipe and re-init the client")
    s.set_defaults(fn=cmd_setup)
    sub.add_parser("start", parents=[common], help="start the managed nym-socks5-client").set_defaults(fn=cmd_start)
    sub.add_parser("stop", parents=[common], help="stop it").set_defaults(fn=cmd_stop)
    sub.add_parser("nym-status", parents=[common], help="show the mixnet client's addresses").set_defaults(fn=cmd_status)
    e = sub.add_parser("exits", parents=[common], help="list active exit gateways with a network requester (queries nym-api)")
    e.add_argument("--limit", type=int, default=20)
    e.add_argument("--min-performance", type=float, default=0.95)
    e.set_defaults(fn=cmd_exits)
    sub.add_parser("ip", parents=[common], help="show the IP that web servers see (should be a Nym exit)").set_defaults(fn=cmd_ip)
    sub.add_parser("tip", parents=[common], help="current block height").set_defaults(fn=cmd_tip)
    sub.add_parser("fees", parents=[common], help="recommended fee rates").set_defaults(fn=cmd_fees)
    st = sub.add_parser("status", parents=[common], help="confirmation status of a txid")
    st.add_argument("txid")
    st.set_defaults(fn=cmd_status_tx)
    d = sub.add_parser("decode", parents=[common], help="offline: parse a raw tx, print txid/vsize")
    d.add_argument("tx", help="hex, a file containing hex, or - for stdin")
    d.set_defaults(fn=cmd_decode)
    b = sub.add_parser("broadcast", parents=[common], help="broadcast a signed raw tx to every endpoint, over the mixnet")
    b.add_argument("tx", help="hex, a file containing hex, or - for stdin")
    b.add_argument("--dry-run", action="store_true", help="parse and print the txid, send nothing")
    b.add_argument("--sequential", action="store_true", help="post to endpoints one after another instead of in parallel")
    b.set_defaults(fn=cmd_broadcast)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ProxyUnreachable as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except (MixnetError, txmod.TxError, ValueError, FileNotFoundError, RuntimeError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
