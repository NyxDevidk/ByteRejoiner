import json
import unittest

from tests import fakes  # noqa: F401  (sets sys.path)
import config
import webhook
from webhook import Notifier, NullNotifier, build_payload, is_valid_url, make_notifier, mask

URL = "https://discord.com/api/webhooks/123456789012345678/AbC-dEf_123"


class UrlTests(unittest.TestCase):
    def test_valid(self):
        for u in (URL, "https://discordapp.com/api/webhooks/1/x-y_z", "https://ptb.discord.com/api/webhooks/1/abc/"):
            self.assertTrue(is_valid_url(u), u)

    def test_invalid(self):
        for u in ("", "http://discord.com/api/webhooks/1/x", "https://evil.com/api/webhooks/1/x",
                  "https://discord.com.evil.com/api/webhooks/1/x", "https://discord.com/api/webhooks/abc/x",
                  "https://discord.com/api/webhooks/1/"):
            self.assertFalse(is_valid_url(u), u)

    def test_mask_hides_token(self):
        m = mask(URL)
        self.assertNotIn("AbC-dEf_123", m)
        self.assertIn("5678", m)

    def test_config_validates_and_keeps_secret_out_of_defaults(self):
        self.assertEqual(config.validate({"webhook_url": URL})["webhook_url"], URL)
        with self.assertRaises(config.ConfigError):
            config.validate({"webhook_url": "https://example.com/hook"})
        self.assertEqual(config.DEFAULTS["webhook_url"], "")


class PayloadTests(unittest.TestCase):
    def test_shape_and_limits(self):
        p = build_payload("Bot", "error", "T" * 500, "D" * 5000, {"A": "x" * 2000, "B": ""})
        e = p["embeds"][0]
        self.assertLessEqual(len(e["title"]), 250)
        self.assertLessEqual(len(e["description"]), 1800)
        self.assertEqual(e["color"], webhook.COLORS["error"])
        self.assertLessEqual(len(e["fields"][0]["value"]), 900)
        self.assertEqual(e["fields"][1]["value"], "-")           # Discord rejects empty field values
        self.assertEqual(p["allowed_mentions"], {"parse": []})   # never pings @everyone
        json.dumps(p)


class NotifierTests(unittest.TestCase):
    def make(self, post):
        return Notifier(URL, "Bot", post=post, sleep=lambda s: None)

    def test_async_send_delivers(self):
        got = []
        n = self.make(lambda url, payload: (got.append((url, payload)) or (204, "")))
        n.send("info", "hello", "world", {"K": "V"})
        n.close()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], URL)
        self.assertEqual(got[0][1]["embeds"][0]["title"], "hello")

    def test_retries_once_on_rate_limit(self):
        calls = []
        def post(url, payload):
            calls.append(1)
            return (429, json.dumps({"retry_after": 0.1})) if len(calls) == 1 else (204, "")
        n = self.make(post)
        self.assertEqual(n.send_sync("info", "x"), (True, "delivered"))
        self.assertEqual(len(calls), 2)

    def test_network_error_never_raises_and_hides_url(self):
        def post(url, payload):
            raise OSError("boom " + url)
        n = self.make(post)
        ok, detail = n.send_sync("info", "x")
        self.assertFalse(ok)
        self.assertNotIn("AbC-dEf_123", detail)

    def test_http_errors_reported(self):
        self.assertEqual(self.make(lambda u, p: (404, "")).send_sync("info", "x"), (False, "webhook deleted or wrong URL"))
        self.assertEqual(self.make(lambda u, p: (500, "")).send_sync("info", "x"), (False, "HTTP 500"))

    def test_send_never_blocks_when_queue_full(self):
        import threading
        gate = threading.Event()
        n = self.make(lambda u, p: (gate.wait(5), (204, ""))[1])
        for i in range(200):
            n.send("info", str(i))          # must return immediately even though the worker is stuck
        gate.set()
        n.close()

    def test_null_notifier_and_factory(self):
        self.assertIsInstance(make_notifier({"webhook_url": ""}), NullNotifier)
        NullNotifier().send("info", "x")
        n = make_notifier({"webhook_url": URL, "webhook_username": ""})
        self.assertEqual(n.username, "Roblox Auto-Rejoin")
        n.close(timeout=0.1)


class SupervisorNotifyTests(unittest.TestCase):
    def test_incident_and_success_are_reported(self):
        from tests.test_retry import FakeMonitor, FakeLauncher, make, SENT
        from tests.fakes import FakeClock
        from monitor import STOPPED, FOREGROUND

        class Rec:
            def __init__(self):
                self.items = []
            def send(self, level, title, description="", fields=None):
                self.items.append((level, title))
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        rec = Rec()
        lau = FakeLauncher([SENT], monitor=mon, up_after=FOREGROUND)
        sup, _ = make(mon, lau, clock, limit=60)
        sup.notifier = rec
        sup.run()
        titles = [t for _, t in rec.items]
        self.assertEqual(titles[0], "Monitoring started")
        self.assertIn("Roblox needs a rejoin", titles)
        self.assertIn("Roblox relaunched", titles)
        self.assertEqual(titles[-1], "Monitoring stopped")


if __name__ == "__main__":
    unittest.main()
