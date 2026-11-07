import os
import pathlib
import tempfile
import unittest
from unittest import mock

from mixpool import nymclient

CONFIG = """[core.client]
id = 'mixpool'

[storage_paths]
# Path to the database containing bandwidth credentials
credentials_database = '/x/socks5-clients/mixpool/data/credentials_database.db'

# Path to the persistent store
reply_surb_database = '/x/socks5-clients/mixpool/data/persistent_reply_store.sqlite'

[core.socks5]
provider_mix_address = 'A.B@C'
bind_address = '127.0.0.1:1080'
send_anonymously = true
"""


class PatchConfig(unittest.TestCase):
    def test_adds_missing_field_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HOME": d}):
            p = nymclient.config_path("mixpool")
            p.parent.mkdir(parents=True)
            p.write_text(CONFIG)
            self.assertTrue(nymclient.patch_config("mixpool"))
            text = p.read_text()
            self.assertIn("credential_requests_database = '/x/socks5-clients/mixpool/data/credential_requests.db'", text)
            self.assertFalse(nymclient.patch_config("mixpool"))  # idempotent
            cfg = nymclient.read_config("mixpool")
            self.assertEqual(cfg["core"]["socks5"]["bind_address"], "127.0.0.1:1080")
            self.assertTrue(cfg["storage_paths"]["credential_requests_database"].endswith("credential_requests.db"))


class NymHome(unittest.TestCase):
    # nym-socks5-client ignores $NYM_HOME and always writes under ~/.nym, so
    # mixpool must look there whatever NYM_HOME says.
    def test_ignores_nym_home(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {"HOME": d, "NYM_HOME": os.path.join(d, "elsewhere")}
        ):
            want = pathlib.Path(d) / ".nym" / "socks5-clients" / "mixpool" / "config" / "config.toml"
            self.assertEqual(nymclient.config_path("mixpool"), want)


if __name__ == "__main__":
    unittest.main()
