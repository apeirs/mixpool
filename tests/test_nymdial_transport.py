"""Offline tests for the nymdial transport, with a fake nymdial binary (tests/fake_nymdial.py)."""

import contextlib
import io
import json
import os
import pathlib
import random
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from mixpool import cli, esplora
from mixpool import nymdial as nd
from mixpool.mixhttp import MixnetClient, MixnetError, NymdialTransport

from .fakes import FakeEsplora

HERE = pathlib.Path(__file__).parent
SEG = next(v for v in json.loads((HERE / "vectors.json").read_text()) if v["name"] == "seg")


class FakeNymdialCase(unittest.TestCase):
    """Puts an executable fake nymdial in a temp dir and points the env at fake servers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(self.tmp.name)
        self.bin = d / "nymdial"
        self.bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_nymdial.py"}" "$@"\n')
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.log = d / "runs.jsonl"
        self.routes = {}
        self.env = mock.patch.dict(os.environ, {"FAKE_NYMDIAL_LOG": str(self.log), "FAKE_NYMDIAL_MODE": "ok"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def route(self, name, server, port=80):
        self.routes[f"{name}:{port}"] = f"127.0.0.1:{server.port}"
        os.environ["FAKE_NYMDIAL_ROUTES"] = json.dumps(self.routes)

    def runs(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def client(self, args=(), setup_timeout=20, retries=0, trace=None):
        t = NymdialTransport(nd.Nymdial(binary=str(self.bin), args=args, setup_timeout=setup_timeout, trace=trace))
        return MixnetClient(transport=t, timeout=5, retries=retries, trace=trace)


class Lookups(FakeNymdialCase):
    def test_each_request_is_its_own_nymdial_process_with_the_name_unresolved(self):
        self.route("api-a.test", FakeEsplora(height=901234))
        c = self.client()
        base, h = esplora.tip_height(c, ["http://api-a.test/api"])
        self.assertEqual((base, h), ("http://api-a.test/api", 901234))
        _, f = esplora.fees(c, ["http://api-a.test/api"])
        self.assertEqual(f["fastestFee"], 5)
        runs = self.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(len({r["pid"] for r in runs}), 2)
        for r in runs:
            # the hostname goes to nymdial as a name: DNS happens over the mixnet, not here
            self.assertEqual(r["argv"][-4:], ["connect", "api-a.test", "80", "--events"])

    def test_response_carries_the_exit(self):
        self.route("api-a.test", FakeEsplora())
        r = self.client().get("http://api-a.test/api/blocks/tip/height")
        self.assertIsNotNone(r.exit)
        self.assertEqual(r.exit.country, "FR")
        self.assertTrue(r.exit.label.startswith("node "))
        self.assertTrue(r.exit.ips[0].startswith("192.0.2."))

    def test_exit_options_are_passed_before_the_subcommand(self):
        self.route("api-a.test", FakeEsplora())
        esplora.tip_height(self.client(args=["--country", "DE", "--exit", "2205"]), ["http://api-a.test/api"])
        argv = self.runs()[0]["argv"]
        self.assertEqual(argv[:5], ["--country", "DE", "--exit", "2205", "connect"])

    def test_nymdial_log_goes_to_the_tracer_not_the_events(self):
        self.route("api-a.test", FakeEsplora())
        lines = []
        esplora.tip_height(self.client(trace=lines.append), ["http://api-a.test/api"])
        self.assertTrue(any("up after" in line for line in lines))
        self.assertFalse(any('"event"' in line for line in lines))

    def test_child_is_gone_after_the_request(self):
        self.route("api-a.test", FakeEsplora())
        esplora.tip_height(self.client(), ["http://api-a.test/api"])
        pid = self.runs()[0]["pid"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("nymdial child still running after the request")


class Broadcast(FakeNymdialCase):
    def test_fresh_mixnet_client_per_endpoint_and_per_broadcast(self):
        good, bad = FakeEsplora(accept_txid=SEG["txid"]), FakeEsplora()
        self.route("good.test", good)
        self.route("bad.test", bad)
        c = self.client()
        for _ in range(2):
            _, results = esplora.broadcast(c, ["http://good.test/api", "http://bad.test/api"], SEG["hex"])
            by = {r.endpoint: r for r in results}
            self.assertTrue(by["http://good.test/api"].accepted)
            self.assertFalse(by["http://bad.test/api"].accepted)
            self.assertIn("missingorspent", by["http://bad.test/api"].message)
            for r in results:
                self.assertIsNotNone(r.exit)
        runs = self.runs()
        self.assertEqual(len(runs), 4)  # 2 endpoints x 2 broadcasts, no client reused
        self.assertEqual(len({r["pid"] for r in runs}), 4)
        self.assertEqual(good.posts[0][1], SEG["hex"])

    def test_delay_is_drawn_per_endpoint_and_waited_before_posting(self):
        good = FakeEsplora(accept_txid=SEG["txid"])
        self.route("good.test", good)
        self.route("also.test", good)
        slept = []

        def sleep(s):
            slept.append((s, len(good.posts)))

        _, results = esplora.broadcast(
            self.client(), ["http://good.test/api", "http://also.test/api"], SEG["hex"],
            parallel=False, delay=(30.0, 120.0), sleep=sleep, rng=random.Random(7),
        )
        self.assertEqual(len(slept), 2)
        for (s, posts_before), r in zip(slept, results):
            self.assertTrue(30.0 <= s <= 120.0)
            self.assertEqual(r.delay, s)
        self.assertEqual(slept[0][1], 0)  # nothing posted before the first wait ended
        self.assertNotEqual(slept[0][0], slept[1][0])  # independent draws

    def test_no_delay_by_default(self):
        self.route("good.test", FakeEsplora(accept_txid=SEG["txid"]))
        slept = []
        _, results = esplora.broadcast(self.client(), ["http://good.test/api"], SEG["hex"], sleep=slept.append)
        self.assertEqual(slept, [])
        self.assertEqual(results[0].delay, 0.0)

    def test_invalid_tx_starts_no_nymdial(self):
        from mixpool.tx import TxError

        with self.assertRaises(TxError):
            esplora.broadcast(self.client(), ["http://x.test/api"], "deadbeef")
        self.assertEqual(self.runs(), [])


class Failures(FakeNymdialCase):
    def test_missing_binary_never_goes_direct(self):
        server = FakeEsplora()
        with mock.patch.dict(os.environ, {"PATH": self.tmp.name + "/empty", "MIXPOOL_NYMDIAL": ""}):
            with self.assertRaises(nd.NymdialMissing):
                MixnetClient(transport=NymdialTransport(), retries=3).get(f"http://127.0.0.1:{server.port}/api/blocks/tip/height")
            with self.assertRaises(nd.NymdialMissing):
                nd.Nymdial(binary="/nonexistent/nymdial")
        self.assertEqual(server.posts, [])

    def test_setup_error_retries_with_a_new_process(self):
        os.environ["FAKE_NYMDIAL_MODE"] = "fail-first"
        self.route("api-a.test", FakeEsplora(height=7))
        r = self.client(retries=1).get("http://api-a.test/api/blocks/tip/height")
        self.assertEqual((r.status, r.text, r.attempts), (200, "7", 2))
        self.assertEqual(len(self.runs()), 2)

    def test_every_exit_failing_is_an_error_that_names_the_step(self):
        os.environ["FAKE_NYMDIAL_MODE"] = "error"
        with self.assertRaises(MixnetError) as cm:
            self.client(retries=1).get("http://api-a.test/api/blocks/tip/height")
        self.assertIn("no working exit", str(cm.exception))
        self.assertEqual(len(self.runs()), 2)
        with self.assertRaises(nd.NymdialError) as cm:
            nd.Nymdial(binary=str(self.bin)).connect("api-a.test", 80)
        self.assertEqual(cm.exception.step, "exit")

    def test_setup_timeout_stops_the_child(self):
        os.environ["FAKE_NYMDIAL_MODE"] = "hang"
        t0 = time.monotonic()
        with self.assertRaises(nd.NymdialError) as cm:
            nd.Nymdial(binary=str(self.bin), setup_timeout=1).connect("api-a.test", 80)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(cm.exception.step, "timeout")
        pid = self.runs()[0]["pid"]
        with self.assertRaises(ProcessLookupError):
            for _ in range(100):
                os.kill(pid, 0)
                time.sleep(0.05)

    def test_exit_without_a_word(self):
        os.environ["FAKE_NYMDIAL_MODE"] = "silent-exit"
        with self.assertRaises(nd.NymdialError) as cm:
            nd.Nymdial(binary=str(self.bin)).connect("api-a.test", 80)
        self.assertIn("exited", str(cm.exception))

    def test_unknown_name_is_a_dns_error(self):
        with self.assertRaises(nd.NymdialError) as cm:
            nd.Nymdial(binary=str(self.bin)).connect("nowhere.test", 443)
        self.assertEqual(cm.exception.step, "dns")


class Cli(FakeNymdialCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_default_transport_is_nymdial(self):
        self.route("api-a.test", FakeEsplora(height=42))
        code, out, _ = self.run_cli("tip", "--nymdial", str(self.bin), "--endpoint", "http://api-a.test/api", "--country", "CH")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("42"))
        self.assertEqual(self.runs()[0]["argv"][:3], ["--country", "CH", "connect"])

    def test_proxy_needs_the_explicit_socks5_flag(self):
        code, _, err = self.run_cli("tip", "--nymdial", str(self.bin), "--proxy", "127.0.0.1:1080")
        self.assertEqual(code, 1)
        self.assertIn("--transport socks5", err)
        self.assertEqual(self.runs(), [])

    def test_broadcast_json_shows_exit_and_delay(self):
        self.route("good.test", FakeEsplora(accept_txid=SEG["txid"]))
        with mock.patch.object(esplora, "_sleep") as sl:
            code, out, err = self.run_cli("broadcast", SEG["hex"], "--nymdial", str(self.bin), "--endpoint", "http://good.test/api", "--delay", "1-2", "--json")
        self.assertEqual(code, 0)
        res = json.loads(out)["results"][0]
        self.assertTrue(res["accepted"])
        self.assertTrue(1.0 <= res["delay"] <= 2.0)
        self.assertEqual(res["exit"]["country"], "FR")
        sl.assert_called_once()
        self.assertIn("fresh mixnet client", err)

    def test_bad_delay(self):
        for bad in ("x", "10-5", "-3"):
            code, _, err = self.run_cli("broadcast", SEG["hex"], "--nymdial", str(self.bin), "--delay", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("--delay", err)
        self.assertEqual(self.runs(), [])

    def test_exits_lists_ipr_exits(self):
        code, out, _ = self.run_cli("exits", "--nymdial", str(self.bin))
        self.assertEqual(code, 0)
        self.assertIn("2205 FR", out)
        self.assertIn("192.0.2.6", out)


class Parsing(unittest.TestCase):
    def test_parse_delay(self):
        self.assertEqual(esplora.parse_delay("30-300"), (30.0, 300.0))
        self.assertEqual(esplora.parse_delay("45"), (45.0, 45.0))
        self.assertEqual(esplora.parse_delay("0.5-1.5"), (0.5, 1.5))
        self.assertIsNone(esplora.parse_delay(None))
        self.assertIsNone(esplora.parse_delay("0"))
        for bad in ("a-b", "5-1", "-1", "1-100000"):
            with self.assertRaises(ValueError):
                esplora.parse_delay(bad)

    def test_exit_info_from_event(self):
        ev = {"event": "connected", "exit": {"node_id": 2205, "country": "FR", "ips": ["192.0.2.5"], "label": "node 2205 (FR)", "identity": "Gw", "version": "1.43.0"}}
        x = nd.ExitInfo.from_event(ev)
        self.assertEqual((x.node_id, x.country, x.ips, x.label), (2205, "FR", ["192.0.2.5"], "node 2205 (FR)"))


if __name__ == "__main__":
    unittest.main()
