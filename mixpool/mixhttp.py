"""HTTP(S) over the Nym mixnet.

Two transports carry the bytes:

- ``nymdial`` (the default): every request is a ``nymdial connect`` child
  process with its own fresh, ephemeral mixnet client and exit (see
  :mod:`mixpool.nymdial`). nymdial resolves the hostname itself, with
  DNS-over-HTTPS through its tunnel, so ``dns`` does not apply.
- ``socks5`` (opt-in): a SOCKS5 stream per request through a running
  nym-socks5-client.

Nothing here ever opens a direct connection to the destination: if the
transport is down, requests fail.

DNS on the socks5 transport: by default (``dns="doh"``) hostnames are resolved with DNS-over-HTTPS
*through the mixnet* (Cloudflare 1.1.1.1, then Google 8.8.8.8, both reached
by IP literal so no DNS lookup is needed to find them) and the stream is
opened to an IPv4 address. Reason: some exit gateways have broken IPv6
routes, and a network requester that resolves a dual-stack name itself may
pick an unreachable AAAA record and stall (seen live against mempool.space,
2026-10-10). ``dns="exit"`` passes the hostname to the exit instead.
Either way the local resolver never sees the destination name.
"""

from __future__ import annotations

import http.client
import json
import random
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import nymdial as nymdial_mod
from . import socks

DEFAULT_PROXY = "127.0.0.1:1080"
# Generic UA: don't advertise the tool to the API operator.
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Connection": "close"}

DOH_RESOLVERS = [
    # (ip, tls server name, path template)
    ("1.1.1.1", "1.1.1.1", "/dns-query?name={name}&type=A"),
    ("8.8.8.8", "8.8.8.8", "/resolve?name={name}&type=A"),
]


class MixnetError(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    reason: str
    body: bytes
    elapsed: float
    connect_host: str
    attempts: int
    headers: dict = field(default_factory=dict)
    # nymdial transport: the exit the request left from (node id, country, IPs)
    exit: Optional[nymdial_mod.ExitInfo] = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class SocksTransport:
    """Streams through a running nym-socks5-client (SOCKS5 CONNECT)."""

    name = "socks5"
    resolves_names = False  # we resolve (DoH over the mixnet) or hand the name to the exit

    def __init__(self, proxy: str = DEFAULT_PROXY):
        self.proxy = socks.parse_proxy(proxy)

    def describe(self) -> str:
        return f"socks5 {self.proxy[0]}:{self.proxy[1]}"

    def open(self, host: str, port: int, timeout: float):
        return socks.connect(self.proxy, host, port, timeout), None


class NymdialTransport:
    """A fresh ``nymdial connect`` process (new mixnet identity, new exit) per stream."""

    name = "nymdial"
    resolves_names = True  # DoH through nymdial's own tunnel

    def __init__(self, dialer: Optional[nymdial_mod.Nymdial] = None, **kw):
        self.dialer = dialer or nymdial_mod.Nymdial(**kw)

    def describe(self) -> str:
        return f"nymdial ({self.dialer.binary}), a fresh mixnet client per connection"

    def open(self, host: str, port: int, timeout: float):
        st = self.dialer.connect(host, port, timeout)
        return st.sock, st


class _MixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, connect_host, transport, timeout):
        super().__init__(host, port, timeout=timeout)
        self._connect_host = connect_host
        self._transport = transport
        self.stream = None

    def connect(self):
        self.sock, self.stream = self._transport.open(self._connect_host, self.port, self.timeout)

    def close(self):
        super().close()
        if self.stream is not None:
            self.stream.close()


class _MixHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, connect_host, transport, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._connect_host = connect_host
        self._transport = transport
        self.stream = None

    def connect(self):
        raw, self.stream = self._transport.open(self._connect_host, self.port, self.timeout)
        # TLS is end-to-end with the destination: the exit gateway only relays
        # ciphertext. SNI/certificate are checked against the real hostname.
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            if self.stream is not None:
                self.stream.close()
            raise

    def close(self):
        super().close()
        if self.stream is not None:
            self.stream.close()


class MixnetClient:
    """Tiny HTTP client whose every byte goes through the Nym mixnet."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: float = 45.0,
        retries: int = 2,
        dns: str = "doh",
        trace: Optional[Callable[[str], None]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        transport=None,
    ):
        """``transport``: a :class:`NymdialTransport` or :class:`SocksTransport`.
        Without one, ``proxy`` selects the socks5 transport, and no proxy means nymdial."""
        if dns not in ("doh", "exit"):
            raise ValueError("dns must be 'doh' or 'exit'")
        if transport is None:
            transport = SocksTransport(proxy) if proxy else NymdialTransport(trace=trace)
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self.dns = dns
        self.trace = trace or (lambda msg: None)
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._dns_cache: dict[str, tuple[float, list[str]]] = {}

    # -- low level -------------------------------------------------------
    def _once(self, method, parsed, connect_host, body, headers):
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https":
            conn = _MixHTTPSConnection(parsed.hostname, port, connect_host, self.transport, self.timeout, self.ssl_context)
        elif parsed.scheme == "http":
            conn = _MixHTTPConnection(parsed.hostname, port, connect_host, self.transport, self.timeout)
        else:
            raise ValueError(f"unsupported scheme {parsed.scheme}")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            exit_info = conn.stream.exit if conn.stream is not None else None
            return resp.status, resp.reason, data, dict(resp.getheaders()), exit_info
        finally:
            conn.close()

    def _resolve(self, host: str) -> list[str]:
        """Resolve ``host`` to IPv4 addresses using DoH over the mixnet."""
        try:
            import ipaddress

            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        cached = self._dns_cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        errors = []
        for ip, sni, tmpl in DOH_RESOLVERS:
            parsed = urllib.parse.urlsplit(f"https://{sni}" + tmpl.format(name=urllib.parse.quote(host)))
            t0 = time.monotonic()
            try:
                status, _, data, _, _ = self._once(
                    "GET", parsed, ip, None, dict(DEFAULT_HEADERS, Accept="application/dns-json")
                )
                if status != 200:
                    raise MixnetError(f"DoH HTTP {status}")
                answers = [a["data"] for a in json.loads(data).get("Answer", []) if a.get("type") == 1]
                if not answers:
                    raise MixnetError("no A records")
                self._dns_cache[host] = (time.monotonic() + 300.0, answers)
                self.trace(f"dns  {host} -> {', '.join(answers)} (DoH via {ip}, over mixnet, {time.monotonic() - t0:.2f}s)")
                return answers
            except (OSError, MixnetError, ValueError, http.client.HTTPException) as e:
                errors.append(f"{ip}: {e}")
                self.trace(f"dns  DoH via {ip} failed after {time.monotonic() - t0:.2f}s: {e}")
        raise MixnetError(f"could not resolve {host} over the mixnet: {'; '.join(errors)}")

    # -- public ----------------------------------------------------------
    def request(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[dict] = None) -> Response:
        parsed = urllib.parse.urlsplit(url)
        hdrs = dict(DEFAULT_HEADERS)
        hdrs.update(headers or {})
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.retries + 2):
            connect_host = parsed.hostname
            if self.dns == "doh" and not self.transport.resolves_names:
                try:
                    connect_host = random.choice(self._resolve(parsed.hostname))
                except MixnetError as e:
                    self.trace(f"dns  falling back to exit-side resolution: {e}")
                    connect_host = parsed.hostname
            t0 = time.monotonic()
            via = "nymdial -> mixnet (DoH in the tunnel)" if self.transport.name == "nymdial" else f"socks5 {self.transport.proxy[0]}:{self.transport.proxy[1]} -> mixnet"
            self.trace(f"send {method} {url} -> {via} -> {connect_host}:{parsed.port or 443} (attempt {attempt})")
            try:
                status, reason, data, rh, exit_info = self._once(method, parsed, connect_host, body, hdrs)
            except (socks.ProxyUnreachable, nymdial_mod.NymdialMissing):
                raise
            except (OSError, http.client.HTTPException) as e:
                last_exc = e
                self.trace(f"fail {url} after {time.monotonic() - t0:.2f}s: {type(e).__name__}: {e}")
                continue
            elapsed = time.monotonic() - t0
            self.trace(f"recv HTTP {status} {reason} from {parsed.hostname} in {elapsed:.2f}s ({len(data)} bytes)")
            return Response(url, status, reason, data, elapsed, connect_host, attempt, rh, exit_info)
        raise MixnetError(f"{method} {url} failed after {self.retries + 1} attempts: {last_exc}")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, body: bytes, **kw) -> Response:
        return self.request("POST", url, body=body, **kw)
