"""Live tests through the real Nym mixnet. Opt-in: MIXPOOL_LIVE=1.

Default transport: nymdial (on PATH, or MIXPOOL_NYMDIAL=/path/to/nymdial):

    MIXPOOL_LIVE=1 python -m unittest tests.test_live -v

socks5 transport: start a client with `mixpool setup --binary /path/to/nym-socks5-client`, then also set
MIXPOOL_PROXY=127.0.0.1:1080.
"""

import json
import os
import pathlib
import unittest

from mixpool import esplora
from mixpool.mixhttp import MixnetClient

LIVE = os.environ.get("MIXPOOL_LIVE") == "1"
PROXY = os.environ.get("MIXPOOL_PROXY")  # set = socks5 transport, unset = nymdial
SEG = next(v for v in json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text()) if v["name"] == "seg")


@unittest.skipUnless(LIVE, "set MIXPOOL_LIVE=1 to run against the live mixnet")
class Live(unittest.TestCase):
    def setUp(self):
        self.c = MixnetClient(proxy=PROXY, timeout=60) if PROXY else MixnetClient(timeout=60, retries=3)

    @unittest.skipIf(PROXY, "nymdial only")
    def test_server_sees_the_exit_nymdial_used(self):
        _, ip, ex = esplora.exit_ip(self.c, with_exit=True)
        self.assertIn(ip, ex.ips)

    def test_fees(self):
        _, f = esplora.fees(self.c, esplora.NETWORKS["mainnet"])
        self.assertIn("fastestFee", f)

    def test_status_of_block_700000_tx(self):
        _, st = esplora.tx_status(self.c, esplora.NETWORKS["mainnet"], SEG["txid"])
        self.assertEqual(st["block_height"], 700000)

    def test_broadcast_already_confirmed_is_rejected_by_bitcoin_core(self):
        _, results = esplora.broadcast(self.c, esplora.NETWORKS["mainnet"], SEG["hex"])
        replies = [r for r in results if r.status]
        self.assertTrue(replies, results)
        for r in replies:
            self.assertFalse(r.accepted)
            self.assertIn("missingorspent", r.message)


if __name__ == "__main__":
    unittest.main()
