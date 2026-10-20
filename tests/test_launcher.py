import unittest

from tests.fakes import FakeShell, ok, rc
from launcher import Launcher, deep_links


class LauncherTests(unittest.TestCase):
    def test_deep_links_use_place_id(self):
        self.assertTrue(all("1234567890" in u and u.startswith("roblox://") for u in deep_links("1234567890")))

    def test_first_method_success(self):
        sh = FakeShell(lambda a: ok("Starting: Intent {...}"), commands={"am"})
        r = Launcher(sh, "com.roblox.client").launch("42")
        self.assertTrue(r.sent)
        cmd = sh.calls[0]
        self.assertEqual(cmd[:2], ["am", "start"])
        self.assertIn("-p", cmd)
        self.assertIn("com.roblox.client", cmd)
        self.assertIn("roblox://experiences/start?placeId=42", cmd)

    def test_am_error_text_with_rc0_is_failure_then_falls_back(self):
        seq = iter([ok("Error: Activity not started, unable to resolve Intent"), ok("Starting: Intent")])
        sh = FakeShell(lambda a: next(seq), commands={"am"})
        r = Launcher(sh, "com.roblox.client").launch("42")
        self.assertTrue(r.sent)
        self.assertEqual(len(sh.calls), 2)

    def test_all_fail(self):
        sh = FakeShell(lambda a: rc(255, "", "Permission Denial"), commands={"am", "termux-open-url"})
        r = Launcher(sh, "com.roblox.client").launch("42")
        self.assertFalse(r.sent)
        self.assertIn("Permission Denial", r.detail)

    def test_missing_am(self):
        r = Launcher(FakeShell(None), "com.roblox.client").launch("42")
        self.assertFalse(r.sent)


if __name__ == "__main__":
    unittest.main()
