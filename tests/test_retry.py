import unittest

from tests.fakes import FakeClock, FakeStop
import config
from launcher import RetryPolicy, LaunchResult
from monitor import STOPPED, FOREGROUND, BACKGROUND, UNKNOWN
from supervisor import Supervisor

SENT = LaunchResult(True, "am", "")
FAIL = LaunchResult(False, "", "boom")


def cfg(**kw):
    return config.validate({"experience_id": "42", **kw})


class FakeMonitor:
    def __init__(self, status, can_detect=True):
        self.status, self.can_detect_stop, self.calibration = status, can_detect, {}

    def observe(self):
        return self.status

    def pid(self):
        return self.pid_value

    pid_value = ""


class FakeLauncher:
    """Returns scripted results; after a sent launch it flips the monitor to `up_after`."""

    def __init__(self, results, last=None, monitor=None, up_after=None, clock=None):
        self.results, self.last, self.calls = list(results), last or results[-1], []
        self.force_flags = []
        self.monitor, self.up_after, self.clock, self.times = monitor, up_after, clock, []

    def launch(self, pid, force_stop=False):
        self.calls.append(pid)
        self.force_flags.append(force_stop)
        if self.clock:
            self.times.append(self.clock.t)
        r = self.results.pop(0) if self.results else self.last
        if r.sent and self.monitor is not None and self.up_after:
            self.monitor.status = self.up_after
        return r


def make(monitor, launcher, clock, net=lambda: True, limit=2000, **kw):
    stop = FakeStop(clock, limit)
    return Supervisor(cfg(**kw), monitor, launcher, stop, net, clock=clock), stop


class PolicyTests(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        p = RetryPolicy(5, 10, 2, 50)
        self.assertEqual([p.delay(i) for i in (1, 2, 3, 4, 5)], [10, 20, 40, 50, 50])

    def test_exhaustion(self):
        p = RetryPolicy(3)
        self.assertFalse(p.exhausted(2))
        self.assertTrue(p.exhausted(3))
        self.assertFalse(RetryPolicy(0).exhausted(10 ** 6))


class RecoveryTests(unittest.TestCase):
    def test_success_first_try_single_launch(self):
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        lau = FakeLauncher([SENT], monitor=mon, up_after=FOREGROUND)
        sup, _ = make(mon, lau, clock, limit=60)
        sup.run()
        self.assertEqual(len(lau.calls), 1)          # one launch; no duplicates once Roblox is up
        self.assertEqual(sup.incidents, 1)

    def test_retries_then_succeeds_with_backoff(self):
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        lau = FakeLauncher([FAIL, FAIL, SENT], monitor=mon, up_after=FOREGROUND)
        sup, stop = make(mon, lau, clock, limit=80, retry_delay=10, min_launch_interval=0, reopen_delay=5)
        sup.run()
        self.assertEqual(len(lau.calls), 3)
        self.assertIn(10.0, stop.waits)
        self.assertIn(20.0, stop.waits)

    def test_max_retries_then_gives_up_and_pauses(self):
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        lau = FakeLauncher([FAIL], clock=clock)
        sup, stop = make(mon, lau, clock, limit=400, max_retries=3, give_up_cooldown=300, min_launch_interval=0)
        sup.run()
        self.assertIn(300.0, stop.waits)             # cooldown observed: no hot loop
        self.assertGreaterEqual(len(lau.calls), 3)
        # Between give-up pauses, an incident never exceeds max_retries launches.
        gaps = [b - a for a, b in zip(lau.times, lau.times[1:])]
        self.assertEqual(sum(1 for g in gaps if g >= 300), (len(lau.calls) - 1) // 3)

    def test_min_launch_interval_enforced(self):
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        lau = FakeLauncher([FAIL], clock=clock)
        sup, _ = make(mon, lau, clock, limit=300, max_retries=0, retry_delay=1, backoff_factor=1,
                      min_launch_interval=30, reopen_delay=0)
        sup.run()
        gaps = [b - a for a, b in zip(lau.times, lau.times[1:])]
        self.assertTrue(len(gaps) >= 2 and all(g >= 30 for g in gaps), gaps)

    def test_network_down_does_not_consume_attempts(self):
        clock, mon = FakeClock(), FakeMonitor(STOPPED)
        lau = FakeLauncher([SENT], monitor=mon, up_after=FOREGROUND)
        calls = {"n": 0}

        def net():
            calls["n"] += 1
            return calls["n"] > 5
        sup, _ = make(mon, lau, clock, net=net, limit=100)
        sup.run()
        self.assertEqual(len(lau.calls), 1)
        self.assertEqual(sup.total_attempts, 1)

    def test_background_does_not_trigger(self):
        clock = FakeClock()
        lau = FakeLauncher([SENT])
        sup, _ = make(FakeMonitor(BACKGROUND), lau, clock, limit=50)
        sup.run()
        self.assertEqual(lau.calls, [])

    def test_unknown_never_launches(self):
        clock = FakeClock()
        lau = FakeLauncher([SENT])
        sup, _ = make(FakeMonitor(UNKNOWN, can_detect=False), lau, clock, limit=50)
        sup.run()
        self.assertEqual(lau.calls, [])

    def test_blind_fallback_relaunches_periodically(self):
        clock = FakeClock()
        lau = FakeLauncher([SENT])
        sup, _ = make(FakeMonitor(UNKNOWN, can_detect=False), lau, clock, limit=400,
                      blind_relaunch_minutes=1, poll_interval=5, min_launch_interval=0, verify_timeout=5)
        sup.run()
        self.assertGreaterEqual(len(lau.calls), 2)

    def test_launch_on_start(self):
        clock, mon = FakeClock(), FakeMonitor(FOREGROUND)
        lau = FakeLauncher([SENT])
        sup, _ = make(mon, lau, clock, limit=30, launch_on_start=True)
        sup.run()
        self.assertEqual(len(lau.calls), 1)

    def test_no_experience_no_launch(self):
        clock = FakeClock()
        lau = FakeLauncher([SENT])
        sup, _ = make(FakeMonitor(STOPPED), lau, clock, limit=30)
        sup.cfg["experience_id"] = ""
        sup.run()
        self.assertEqual(lau.calls, [])


if __name__ == "__main__":
    unittest.main()
