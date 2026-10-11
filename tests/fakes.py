"""In-process fakes: a SOCKS5 proxy and an Esplora-like HTTP API."""

import json
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSocks5(socketserver.ThreadingTCPServer):
    """SOCKS5 server that records CONNECT targets and forwards to ``route(host, port)``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, route, reply_code=0):
        self.route = route
        self.reply_code = reply_code
        self.targets = []
        super().__init__(("127.0.0.1", 0), _SocksHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def addr(self):
        return f"127.0.0.1:{self.server_address[1]}"


class _SocksHandler(socketserver.BaseRequestHandler):
    def _recv(self, n):
        b = b""
        while len(b) < n:
            c = self.request.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def handle(self):
        ver, nmeth = self._recv(2)
        self._recv(nmeth)
        self.request.sendall(b"\x05\x00")
        ver, cmd, _, atyp = self._recv(4)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, self._recv(4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._recv(16))
        else:
            host = self._recv(self._recv(1)[0]).decode()
        (port,) = struct.unpack(">H", self._recv(2))
        self.server.targets.append((atyp, host, port))
        if self.server.reply_code:
            self.request.sendall(bytes([5, self.server.reply_code, 0, 1]) + b"\0" * 6)
            return
        # Like nym-socks5-client: report success at once, the far end may still fail.
        self.request.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
        try:
            upstream = socket.create_connection(self.server.route(host, port))
        except OSError:
            return  # stream just closes, as when an exit can't reach the host
        _pipe(self.request, upstream)


def _pipe(a, b):
    def fwd(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=fwd, args=(b, a), daemon=True)
    t.start()
    fwd(a, b)
    t.join(5)


class FakeEsplora(ThreadingHTTPServer):
    """Plain-HTTP Esplora subset. ``accept`` decides what /tx does."""

    daemon_threads = True

    def __init__(self, accept_txid=None, fee_style="mempool", height=900000, fail_status=None):
        self.accept_txid = accept_txid
        self.fee_style = fee_style
        self.height = height
        self.fail_status = fail_status
        self.posts = []
        super().__init__(("127.0.0.1", 0), _EsploraHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class _EsploraHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        s = self.server
        if s.fail_status:
            return self._send(s.fail_status, "down")
        if self.path.endswith("/blocks/tip/height"):
            return self._send(200, str(s.height))
        if self.path.endswith("/v1/fees/recommended") and s.fee_style == "mempool":
            return self._send(200, json.dumps({"fastestFee": 5, "halfHourFee": 3, "hourFee": 2, "economyFee": 1, "minimumFee": 1}), "application/json")
        if self.path.endswith("/fee-estimates"):
            return self._send(200, json.dumps({"1": 7.5, "3": 4.0, "6": 2.1, "144": 1.0}), "application/json")
        if "/tx/" in self.path and self.path.endswith("/status"):
            return self._send(200, json.dumps({"confirmed": True, "block_height": 700000, "block_hash": "00" * 32}), "application/json")
        self._send(404, "not found")

    def do_POST(self):
        s = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        s.posts.append((self.path, body, dict(self.headers)))
        if s.accept_txid:
            return self._send(200, s.accept_txid)
        self._send(400, 'sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}')
