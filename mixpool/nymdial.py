"""TCP streams through the Nym mixnet with the ``nymdial`` binary.

nymdial (https://github.com/apeirs/nymdial) runs Nym's mixnet client and a
userspace TCP/IP stack in-process, over an IP Packet Router exit. There is no
nym-socks5-client to install and no local proxy port.

Every stream mixpool opens is its own ``nymdial connect HOST PORT --events``
child process, so every stream gets:

- a new ephemeral mixnet client: a fresh Nym identity and keys, never reused;
- its own exit, drawn from the nym-api (performance-weighted, with rotation
  past exits that don't answer and a health check on the tunnel);
- DNS over the mixnet: the hostname is passed to nymdial unresolved and looked
  up with DNS-over-HTTPS through the tunnel. The local resolver never sees it.

The child's stdin and stdout are one end of a socketpair. The other end is
the socket handed back here, so ``http.client`` and ``ssl`` use it like any
TCP socket. TLS runs in this process, end to end with the server: the exit
only relays ciphertext.

There is no clearnet fallback. If nymdial is missing or no exit works, the
stream fails.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence


class NymdialMissing(FileNotFoundError):
    """No nymdial binary. We never fall back to a direct connection."""


class NymdialError(OSError):
    """nymdial could not open the stream (no working exit, DNS, TCP, TLS)."""

    def __init__(self, msg: str, step: Optional[str] = None):
        super().__init__(msg)
        self.step = step


def find_binary(explicit: Optional[str] = None) -> str:
    for cand in (explicit, os.environ.get("MIXPOOL_NYMDIAL"), shutil.which("nymdial")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise NymdialMissing(
        "nymdial not found. Build it from https://github.com/apeirs/nymdial "
        "(cargo install --git https://github.com/apeirs/nymdial), then put it on PATH, "
        "pass --nymdial PATH or set MIXPOOL_NYMDIAL. Refusing to fall back to a direct "
        "(clearnet) connection."
    )


@dataclass
class ExitInfo:
    """The Nym exit gateway a stream left from: what the server sees."""

    node_id: int
    country: Optional[str]
    ips: list[str]
    label: str
    identity: str = ""
    version: str = ""

    @classmethod
    def from_event(cls, ev: dict) -> "ExitInfo":
        x = ev.get("exit") or {}
        return cls(
            node_id=int(x.get("node_id") or 0),
            country=x.get("country"),
            ips=[str(i) for i in x.get("ips") or []],
            label=str(x.get("label") or f"node {x.get('node_id')}"),
            identity=str(x.get("identity") or ""),
            version=str(x.get("version") or ""),
        )


@dataclass
class Stream:
    """One open stream: ``sock`` carries the bytes, ``close()`` ends the nymdial process too."""

    sock: socket.socket
    exit: ExitInfo
    tunnel_s: float
    connect_s: float
    attempts: list[dict] = field(default_factory=list)
    proc: Optional[subprocess.Popen] = None

    def close(self, grace: float = 3.0) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        _reap(self.proc, grace)


def _reap(proc: Optional[subprocess.Popen], grace: float) -> None:
    """Let nymdial exit on its own (it does once the server closes), else stop it."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.wait(grace)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Nymdial:
    """Opens streams with ``nymdial connect``.

    ``args`` are extra nymdial options for every stream, e.g.
    ``["--country", "DE"]`` or ``["--exit", "2205"]``.
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        args: Sequence[str] = (),
        setup_timeout: float = 180.0,
        trace: Optional[Callable[[str], None]] = None,
    ):
        self.binary = find_binary(binary)
        self.args = list(args)
        self.setup_timeout = setup_timeout
        self.trace = trace or (lambda msg: None)

    def connect(self, host: str, port: int, timeout: Optional[float] = None) -> Stream:
        """Open a TCP stream to ``host:port`` through a fresh mixnet client.

        Blocks until nymdial reports the stream open (``connected``), at most
        ``setup_timeout`` seconds. ``timeout`` becomes the socket timeout.
        """
        cmd = [self.binary, *self.args, "connect", host, str(int(port)), "--events"]
        ours, theirs = socket.socketpair()
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=theirs.fileno(),
                stdout=theirs.fileno(),
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as e:
            ours.close()
            raise NymdialMissing(f"could not start {self.binary}: {e}") from e
        finally:
            theirs.close()

        events: queue.Queue = queue.Queue()
        tag = f"nymdial[{proc.pid}]"
        threading.Thread(target=_read_stderr, args=(proc, events, self.trace, tag), daemon=True).start()
        attempts: list[dict] = []
        deadline = t0 + self.setup_timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise NymdialError(
                        f"{host}:{port}: no stream through the mixnet within {self.setup_timeout:.0f}s", "timeout"
                    )
                try:
                    ev = events.get(timeout=left)
                except queue.Empty:
                    continue
                if ev is None:  # stderr closed: nymdial exited without saying it connected
                    code = proc.wait()
                    raise NymdialError(f"{host}:{port}: nymdial exited (code {code}) before the stream opened", "exit")
                kind = ev.get("event")
                if kind == "attempt":
                    attempts.append(ev)
                elif kind == "error":
                    raise NymdialError(f"{host}:{port}: {ev.get('message')}", ev.get("step"))
                elif kind == "connected":
                    break
        except BaseException:
            ours.close()
            _reap(proc, 0.5)
            raise
        info = ExitInfo.from_event(ev)
        ours.settimeout(timeout)
        self.trace(
            f"nym  {host}:{port} open via exit {info.label} [{', '.join(info.ips)}] "
            f"(fresh mixnet client; tunnel {ev.get('tunnel_s', 0):.2f}s, DNS+TCP {ev.get('connect_s', 0):.2f}s)"
        )
        return Stream(ours, info, float(ev.get("tunnel_s") or 0), float(ev.get("connect_s") or 0), attempts, proc)

    def exits(self, timeout: float = 60.0) -> list[dict]:
        """The IPR exits nymdial would pick from (``nymdial exits --json``)."""
        out = subprocess.run(
            [self.binary, *self.args, "exits", "--json"], capture_output=True, timeout=timeout
        )
        if out.returncode != 0:
            raise NymdialError(out.stderr.decode("utf-8", "replace").strip() or f"nymdial exits: code {out.returncode}")
        return json.loads(out.stdout)


def _read_stderr(proc: subprocess.Popen, events: queue.Queue, trace: Callable[[str], None], tag: str) -> None:
    """JSON lines become events; everything else is nymdial's own log, passed to the tracer."""
    assert proc.stderr is not None
    for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if line.startswith("{"):
            try:
                events.put(json.loads(line))
                continue
            except ValueError:
                pass
        if line:
            trace(f"{tag} {line.strip()}")
    events.put(None)
