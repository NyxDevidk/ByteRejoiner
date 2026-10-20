import unittest

from tests.fakes import FakeShell, ok, rc
import monitor
from monitor import Monitor, StateTracker, STOPPED, UNKNOWN, FOREGROUND, BACKGROUND, RUNNING

PKG = "com.roblox.client"


def shell_visible(roblox_up=True):
    def h(a):
        if a[0] == "pidof":
            if a[1] == PKG:
                return ok("4321") if roblox_up else rc(1)
            if a[1] == "system_server":
                return ok("900")
            return rc(1)
    return FakeShell(h, commands={"pidof", "ps"})


def shell_hidden():
    # Android 7+ hidepid: other apps' (even system) processes are invisible, pidof just exits 1.
    return FakeShell(lambda a: rc(1) if a[0] == "pidof" else None, commands={"pidof"})


class ObserveTests(unittest.TestCase):
    def test_running_and_stopped_when_visible(self):
        m = Monitor(shell_visible(True), PKG)
        self.assertTrue(m.calibrate())
        self.assertEqual(m.observe(), RUNNING)
        m = Monitor(shell_visible(False), PKG)
        m.calibrate()
        self.assertEqual(m.observe(), STOPPED)

    def test_hidden_process_list_is_unknown_not_stopped(self):
        m = Monitor(shell_hidden(), PKG)
        self.assertFalse(m.calibrate())
        self.assertEqual(m.observe(), UNKNOWN)

    def test_positive_sighting_trusted_even_uncalibrated(self):
        sh = FakeShell(lambda a: ok("55") if a[:2] == ["pidof", PKG] else rc(1), commands={"pidof"})
        m = Monitor(sh, PKG)
        m.calibrate()
        self.assertEqual(m.observe(), RUNNING)

    def test_command_errors_are_unknown(self):
        m = Monitor(FakeShell(None), PKG)
        m.calibrate()
        self.assertEqual(m.observe(), UNKNOWN)

    def test_dumpsys_foreground_background(self):
        procs = "x" * 300 + "\n  *APP* UID 10234 ProcessRecord{ab12 4321:%s/u0a234}\n" % PKG

        def make(resumed):
            def h(a):
                if a[:3] == ["dumpsys", "activity", "processes"]:
                    return ok(procs)
                if a[:3] == ["dumpsys", "activity", "activities"]:
                    return ok("  mResumedActivity: ActivityRecord{1 u0 %s/.Main t5}" % resumed)
            return FakeShell(h, commands={"dumpsys"})
        m = Monitor(make(PKG), PKG, probes=[monitor.DumpsysProbe()])
        m.calibrate()
        self.assertEqual(m.observe(), FOREGROUND)
        m = Monitor(make("com.android.launcher"), PKG, probes=[monitor.DumpsysProbe()])
        m.calibrate()
        self.assertEqual(m.observe(), BACKGROUND)

    def test_dumpsys_denied_not_trusted(self):
        sh = FakeShell(lambda a: rc(255, "", "Permission Denial") if a[0] == "dumpsys" else None, commands={"dumpsys"})
        m = Monitor(sh, PKG, probes=[monitor.DumpsysProbe()])
        self.assertFalse(m.calibrate())


class TrackerTests(unittest.TestCase):
    def test_needs_consecutive_stops(self):
        t = StateTracker(3, True)
        self.assertIsNone(t.update(STOPPED))
        self.assertIsNone(t.update(STOPPED))
        self.assertEqual(t.update(STOPPED), "closed")

    def test_running_resets_streak(self):
        t = StateTracker(3, True)
        t.update(STOPPED)
        t.update(STOPPED)
        t.update(FOREGROUND)
        self.assertIsNone(t.update(STOPPED))

    def test_unknown_is_neutral(self):
        t = StateTracker(3, True)
        t.update(STOPPED)
        t.update(UNKNOWN)
        t.update(UNKNOWN)
        self.assertIsNone(t.update(STOPPED))      # streak is 2: unknown neither reset nor advanced it
        self.assertEqual(t.update(STOPPED), "closed")

    def test_background_never_triggers(self):
        t = StateTracker(1, True)
        for _ in range(20):
            self.assertIsNone(t.update(BACKGROUND))

    def test_disarmed_until_seen_running(self):
        t = StateTracker(2, recover_on_start=False)
        for _ in range(5):
            self.assertIsNone(t.update(STOPPED))
        t.update(RUNNING)
        t.update(STOPPED)
        self.assertEqual(t.update(STOPPED), "closed")


if __name__ == "__main__":
    unittest.main()
