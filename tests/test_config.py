import json
import os
import tempfile
import unittest

from tests import fakes  # noqa: F401  (sets sys.path)
import config
from config import ConfigError, parse_experience


class ParseTests(unittest.TestCase):
    def test_valid(self):
        for text in ("1234567890", " 1234567890 ",
                     "https://www.roblox.com/games/1234567890/Example-Game",
                     "https://www.roblox.com/games/1234567890",
                     "https://roblox.com/de/games/1234567890/x?privateServerLinkCode=1",
                     "www.roblox.com/games/1234567890/Example",
                     "roblox://placeId=1234567890",
                     "roblox://experiences/start?placeId=1234567890"):
            self.assertEqual(parse_experience(text), "1234567890", text)

    def test_invalid(self):
        for text in ("", "   ", "abc", "0", "-5", "12.5", "https://example.com/games/123/x",
                     "https://evilroblox.com/games/123/x", "https://www.roblox.com/users/123/profile",
                     "https://www.roblox.com/games/abc", "javascript:alert(1)", "1" * 25):
            with self.assertRaises(ConfigError, msg=text):
                parse_experience(text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_defaults(self):
        s = config.Store(self.path)
        s.load()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(s.data["max_retries"], 5)

    def test_persists(self):
        s = config.Store(self.path)
        s.load()
        s.save_experience("111", "Game")
        s.update(poll_interval=7)
        s2 = config.Store(self.path)
        s2.load()
        self.assertEqual(s2.data["experience_id"], "111")
        self.assertEqual(s2.data["poll_interval"], 7.0)
        self.assertEqual(s2.data["saved_experiences"], [{"name": "Game", "id": "111"}])

    def test_rejects_bad_values_and_keeps_old(self):
        s = config.Store(self.path)
        s.load()
        for bad in ({"poll_interval": -1}, {"max_retries": "x"}, {"log_level": "loud"},
                    {"launch_on_start": "yes"}, {"experience_id": "nope"}, {"roblox_package": "bad pkg"}):
            with self.assertRaises(ConfigError, msg=bad):
                s.update(**bad)
        self.assertEqual(s.data["poll_interval"], 5.0)

    def test_corrupt_file_recovered(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        s = config.Store(self.path)
        with self.assertRaises(ConfigError):
            s.load()
        self.assertTrue(os.path.exists(self.path + ".bad"))
        self.assertEqual(s.data["max_retries"], 5)
        with open(self.path) as fh:
            json.load(fh)

    def test_no_secret_fields(self):
        for k in config.DEFAULTS:
            self.assertFalse(any(w in k for w in ("password", "cookie", "token", "secret")))


if __name__ == "__main__":
    unittest.main()
