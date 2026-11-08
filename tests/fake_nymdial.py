"""A stand-in for the nymdial binary, for offline tests.

    fake_nymdial.py [opts] connect HOST PORT --events
    fake_nymdial.py [opts] exits --json

Configured by environment variables:

    FAKE_NYMDIAL_ROUTES  JSON {"host:port": "127.0.0.1:port"}: where the "exit" connects
    FAKE_NYMDIAL_LOG     file to append one JSON line per run: {"pid", "argv"}
    FAKE_NYMDIAL_MODE    ok (default) | error | hang | silent-exit | fail-first
                         (fail-first: the first run in this log errors, later runs work)

Like the real one, it reports on stderr (human lines plus JSON events with
--events), speaks to the server over stdin/stdout, and exits when the server
closes the stream.
"""

import json
import os
import random
import socket
import sys
import threading
import time


def log_run():
    path = os.environ.get("FAKE_NYMDIAL_LOG")
    n = 0
    if path:
        if os.path.exists(path):
            with open(path) as f:
                n = sum(1 for _ in f)
        with open(path, "a") as f:
            f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:]}) + "\n")
    return n


def event(**kw):
    sys.stderr.write(json.dumps(kw) + "\n")
    sys.stderr.flush()


def pipe(client: socket.socket, upstream: socket.socket):
    def up():
        try:
            while True:
                d = client.recv(65536)
                if not d:
                    break
                upstream.sendall(d)
        except OSError:
            pass
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=up, daemon=True).start()
    while True:  # down: until the server closes
        d = upstream.recv(65536)
        if not d:
            break
        client.sendall(d)


def main():
    args = sys.argv[1:]
    prior_runs = log_run()
    mode = os.environ.get("FAKE_NYMDIAL_MODE", "ok")
    if "exits" in args:
        print(json.dumps([
            {"node_id": 2205, "identity": "GwA", "ipr_address": "c.k@GwA", "performance": 0.99, "version": "1.43.0", "country": "FR", "ips": ["192.0.2.5"]},
            {"node_id": 3396, "identity": "GwB", "ipr_address": "c.k@GwB", "performance": 0.5, "version": "1.43.0", "country": "CA", "ips": ["192.0.2.6", "2001:db8::6"]},
        ]))
        return 0
    i = args.index("connect")
    host, port = args[i + 1], int(args[i + 2])
    node = random.randint(1000, 9999)
    print(f"  exit node {node} (FR)           up after 0.01s (incl. health check)", file=sys.stderr, flush=True)
    if mode == "fail-first":
        mode = "error" if prior_runs == 0 else "ok"
    if mode == "error":
        event(event="attempt", node_id=node, label=f"node {node} (FR)", elapsed_s=0.01, ok=False, error="health check failed")
        event(event="error", step="exit", message="no working exit after 1 attempts (last: health check failed)")
        print("nymdial: no working exit after 1 attempts", file=sys.stderr)
        return 1
    if mode == "hang":
        time.sleep(3600)
        return 1
    if mode == "silent-exit":
        return 1
    event(event="attempt", node_id=node, label=f"node {node} (FR)", elapsed_s=0.01, ok=True, error=None)
    routes = json.loads(os.environ.get("FAKE_NYMDIAL_ROUTES", "{}"))
    target = routes.get(f"{host}:{port}")
    if target is None:
        event(event="error", step="dns", message=f"DNS over the mixnet for {host}: NXDOMAIN")
        return 1
    th, tp = target.rsplit(":", 1)
    try:
        upstream = socket.create_connection((th, int(tp)), timeout=10)
    except OSError as e:
        event(event="error", step="tcp", message=f"TCP connect to {target}: {e}")
        return 1
    upstream.settimeout(None)
    event(event="connected", host=host, port=port, tls=False, generation=1, tunnel_s=0.01, connect_s=0.01,
          exit={"node_id": node, "country": "FR", "identity": "GwFake", "version": "1.43.0",
                "ips": [f"192.0.2.{node % 250 + 1}"], "label": f"node {node} (FR)"})
    client = socket.socket(fileno=os.dup(0))
    pipe(client, upstream)
    print("closed by the server", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
