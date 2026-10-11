"""Offline tests: SOCKS5 + HTTP + Esplora logic against in-process fakes."""

import json
import pathlib
import socket
import unittest

from mixpool import esplora, socks
from mixpool.mixhttp import MixnetClient, MixnetError

from .fakes import FakeEsplora, FakeSocks5

SEG = next(v for v in json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text()) if v["name"] == "seg")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Socks(unittest.TestCase):
    def test_encode_address(self):
        self.assertEqual(socks.encode_address("1.2.3.4"), b"\x01\x01\x02\x03\x04")
        self.assertEqual(socks.encode_address("::1")[:1], b"\x04")
        self.assertEqual(socks.encode_address("mempool.space"), b"\x03\x0dmempool.space")

    def test_parse_proxy(self):
        self.assertEqual(socks.parse_proxy("127.0.0.1:1080"), ("127.0.0.1", 1080))
        self.assertEqual(socks.parse_proxy("socks5h://localhost:9050"), ("localhost", 9050))
        self.assertEqual(socks.parse_proxy("[::1]:1081"), ("::1", 1081))
        self.assertEqual(socks.parse_proxy("localhost"), ("localhost", 1080))

    def test_no_proxy_means_no_request(self):
        """If the mixnet client is down we must fail, never go direct."""
        c = MixnetClient(proxy=f"127.0.0.1:{free_port()}", dns="exit", retries=0)
        with self.assertRaises(socks.ProxyUnreachable):
            c.get("http://example.invalid/")

    def test_socks_error_reply(self):
        proxy = FakeSocks5(route=None, reply_code=2)
        with self.assertRaises(socks.SocksError) as cm:
            socks.connect(socks.parse_proxy(proxy.addr), "example.com", 443, timeout=5)
        self.assertIn("exit policy", str(cm.exception))


class Esplora(unittest.TestCase):
    def setUp(self):
        self.apis = {}

        def route(host, port):
            return ("127.0.0.1", self.apis[host].port)

        self.proxy = FakeSocks5(route=route)

    def client(self, **kw):
        kw.setdefault("dns", "exit")
        kw.setdefault("timeout", 5)
        kw.setdefault("retries", 0)
        return MixnetClient(proxy=self.proxy.addr, **kw)

    def test_hostname_goes_to_exit_not_local_resolver(self):
        self.apis["api-a.test"] = FakeEsplora(height=123)
        base, h = esplora.tip_height(self.client(), ["http://api-a.test/api"])
        self.assertEqual(h, 123)
        # ATYP 3 = domain name handed to the proxy; nothing resolved locally
        self.assertEqual(self.proxy.targets[-1], (3, "api-a.test", 80))

    def test_fees_mempool_style_and_esplora_fallback(self):
        self.apis["m.test"] = FakeEsplora(fee_style="mempool")
        self.apis["e.test"] = FakeEsplora(fee_style="esplora")
        _, f = esplora.fees(self.client(), ["http://m.test/api"])
        self.assertEqual(f["fastestFee"], 5)
        _, f = esplora.fees(self.client(), ["http://e.test/api"])
        self.assertEqual(f, {"fastestFee": 7.5, "halfHourFee": 4.0, "hourFee": 2.1, "economyFee": 1.0})

    def test_failover_to_next_endpoint(self):
        self.apis["down.test"] = FakeEsplora(fail_status=503)
        self.apis["up.test"] = FakeEsplora(height=42)
        base, h = esplora.tip_height(self.client(), ["http://down.test/api", "http://up.test/api"])
        self.assertEqual((base, h), ("http://up.test/api", 42))

    def test_all_endpoints_down(self):
        self.apis["down.test"] = FakeEsplora(fail_status=503)
        with self.assertRaises(MixnetError):
            esplora.tip_height(self.client(), ["http://down.test/api"])

    def test_broadcast_accept_and_reject(self):
        good = FakeEsplora(accept_txid=SEG["txid"])
        bad = FakeEsplora()
        self.apis.update({"good.test": good, "bad.test": bad})
        info, results = esplora.broadcast(self.client(), ["http://good.test/api", "http://bad.test/api"], SEG["hex"])
        self.assertEqual(info.txid, SEG["txid"])
        by = {r.endpoint: r for r in results}
        self.assertTrue(by["http://good.test/api"].accepted)
        self.assertFalse(by["http://bad.test/api"].accepted)
        self.assertIn("missingorspent", by["http://bad.test/api"].message)
        path, body, headers = good.posts[0]
        self.assertEqual(path, "/api/tx")
        self.assertEqual(body, SEG["hex"])
        self.assertEqual(headers.get("User-Agent"), "Mozilla/5.0")

    def test_broadcast_wrong_txid_in_2xx_is_not_accepted(self):
        self.apis["liar.test"] = FakeEsplora(accept_txid="00" * 32)
        _, results = esplora.broadcast(self.client(), ["http://liar.test/api"], SEG["hex"])
        self.assertFalse(results[0].accepted)

    def test_broadcast_validates_before_sending(self):
        self.apis["x.test"] = FakeEsplora()
        from mixpool.tx import TxError

        with self.assertRaises(TxError):
            esplora.broadcast(self.client(), ["http://x.test/api"], "deadbeef")
        self.assertEqual(self.proxy.targets, [])

    def test_unreachable_endpoint_reports_no_reply(self):
        def route(host, port):
            return ("127.0.0.1", free_port())  # nothing listening behind the "exit"

        proxy = FakeSocks5(route=route)
        c = MixnetClient(proxy=proxy.addr, dns="exit", timeout=3, retries=0)
        _, results = esplora.broadcast(c, ["http://gone.test/api"], SEG["hex"])
        self.assertFalse(results[0].accepted)
        self.assertIsNone(results[0].status)


if __name__ == "__main__":
    unittest.main()
