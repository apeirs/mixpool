import json
import pathlib
import unittest

from mixpool import tx

VECTORS = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text())


class TxParse(unittest.TestCase):
    def test_known_vectors(self):
        for v in VECTORS:
            with self.subTest(v["name"]):
                info = tx.parse(v["hex"])
                self.assertEqual(info.txid, v["txid"])
                self.assertEqual(info.size, v["size"])
                self.assertEqual(info.vsize, (v["weight"] + 3) // 4)
                self.assertEqual(info.n_inputs, v["n_inputs"])
                self.assertEqual(info.n_outputs, v["n_outputs"])
                self.assertEqual(info.output_sats, v["output_sats"])

    def test_segwit_flag_and_wtxid(self):
        seg = next(v for v in VECTORS if v["name"] == "seg")
        info = tx.parse(seg["hex"])
        self.assertTrue(info.segwit)
        self.assertNotEqual(info.txid, info.wtxid)
        legacy = tx.parse(next(v for v in VECTORS if v["name"] == "legacy")["hex"])
        self.assertFalse(legacy.segwit)
        self.assertEqual(legacy.txid, legacy.wtxid)

    def test_whitespace_is_ignored(self):
        h = VECTORS[0]["hex"]
        spaced = "\n".join(h[i : i + 64] for i in range(0, len(h), 64)) + "\n"
        self.assertEqual(tx.parse(spaced).txid, VECTORS[0]["txid"])

    def test_rejects_garbage(self):
        h = VECTORS[0]["hex"]
        for bad in ["", "zz", "abc", h[:-2], h + "00", "01000000" + "00" * 2]:
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(tx.TxError):
                    tx.parse(bad)


if __name__ == "__main__":
    unittest.main()
