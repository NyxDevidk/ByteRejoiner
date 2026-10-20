import unittest

from tests.fakes import FakeClock, FakeShell, FakeStop, ok, rc
import config
from launcher import Launcher, LaunchResult
from monitor import FOREGROUND, LogcatWatcher, parse_disconnect_code
from supervisor import Supervisor

SENT = LaunchResult(True, "am", "")


class ParseTests(unittest.TestCase):
    def test_real_roblox_line(self):
        line = "10-21 07:50:01.123 I/Roblox  (15641): [FLog::Network] Sending disconnect with reason: 278"
        self.assertEqual(parse_disconnect_code(line), 278)

    def test_error_code_dialog_text(self):
        self.assertEqual(parse_disconnect_code("Error Code: 277"), 277)

    def test_ignores_handshake_and_noise(self):
        for line in ("Sending disconnect with reason: 0", "disconnect with reason: 1",
                     "onGameLoaded: placeId = 123974602339071", "Error Code: 9999", ""):
            self.assertIsNone(parse_disconnect_code(line), line)


class WatcherTests(unittest.TestCase):
    def make(self, dumps):
        it = iter(dumps)
        sh = FakeShell(lambda a: ok(next(it)) if a[0] == "logcat" else None, commands={"logcat"})
        return LogcatWatcher(sh), sh

    def test_seed_hides_backlog_new_line_triggers_once(self):
        old = "10-21 07:00:00.000 I/Roblox (1): disconnect with reason: 278"
        new = "10-21 07:50:00.000 I/Roblox (1): disconnect with reason: 277"
        w, sh = self.make([old, old + "\n" + new, old + "\n" + new])
        w.seed("1")
        hits = w.poll("1")
        self.assertEqual([c for c, _ in hits], [277])      # old backlog ignored
        self.assertEqual(w.poll("1"), [])                  # same line not reported twice

    def test_unreadable_logcat_is_silent(self):
        sh = FakeShell(lambda a: rc(1, "", "denied"), commands={"logcat"})
        w = LogcatWatcher(sh)
        w.seed("1")
        self.assertEqual(w.poll("1"), [])

    def test_uses_pid_filter(self):
        w, sh = self.make([""])
        w.seed("4321")
        self.assertIn("--pid", sh.calls[0])
        self.assertIn("4321", sh.calls[0])


class ForceStopTests(unittest.TestCase):
    def test_force_stop_runs_before_launch(self):
        sh = FakeShell(lambda a: ok("Starting: Intent"), commands={"am"})
        Launcher(sh, "com.roblox.client", sleep=lambda s: None).launch("42", force_stop=True)
        self.assertEqual(sh.calls[0], ["am", "force-stop", "com.roblox.client"])
        self.assertEqual(sh.calls[1][:2], ["am", "start"])

    def test_no_force_stop_by_default(self):
        sh = FakeShell(lambda a: ok("Starting: Intent"), commands={"am"})
        Launcher(sh, "com.roblox.client", sleep=lambda s: None).launch("42")
        self.assertEqual(sh.calls[0][:2], ["am", "start"])

    def test_force_stop_failure_recorded_launch_continues(self):
        def h(a):
            return rc(1, "", "Permission Denial") if a[1] == "force-stop" else ok("Starting: Intent")
        sh = FakeShell(h, commands={"am"})
        lau = Launcher(sh, "com.roblox.client", sleep=lambda s: None)
        self.assertTrue(lau.launch("42", force_stop=True).sent)
        self.assertFalse(lau.force_stop_ok)


# -- supervisor integration -------------------------------------------------

class Mon:
    can_detect_stop = True
    calibration = {}

    def __init__(self, pid="100"):
        self.pid_value, self.on_observe, self.n = pid, None, 0

    def observe(self):
        self.n += 1
        if self.on_observe:
            self.on_observe(self.n)
        return FOREGROUND

    def pid(self):
        return self.pid_value


class Lau:
    force_stop_ok = True

    def __init__(self, mon):
        self.mon, self.calls, self.flags = mon, [], []

    def launch(self, pid, force_stop=False):
        self.calls.append(pid)
        self.flags.append(force_stop)
        self.mon.pid_value = str(int(self.mon.pid_value or 0) + 100)   # a fresh process
        return SENT


class Watcher:
    def __init__(self, script):
        self.script, self.seeded, self.polls = script, [], 0

    def seed(self, pid):
        self.seeded.append(pid)

    def poll(self, pid):
        self.polls += 1
        return self.script(self.polls, pid)


def make(mon, lau, watcher=None, limit=60, **kw):
    clock = FakeClock()
    cfg = config.validate({"experience_id": "42", "min_launch_interval": 0, **kw})
    return Supervisor(cfg, mon, lau, FakeStop(clock, limit), lambda: True, clock=clock, watcher=watcher)


class HealthIntegrationTests(unittest.TestCase):
    def test_disconnect_code_triggers_rejoin_with_force_stop(self):
        mon = Mon()
        lau = Lau(mon)
        w = Watcher(lambda n, pid: [(278, "disconnect with reason: 278")] if n == 3 and pid == "100" else [])
        sup = make(mon, lau, w)
        sup.run()
        self.assertEqual(lau.calls, ["42"])
        self.assertEqual(lau.flags, [True])
        self.assertIn("200", w.seeded)                  # re-seeded against the new process

    def test_pid_change_triggers_rejoin(self):
        mon = Mon("100")
        mon.on_observe = lambda n: setattr(mon, "pid_value", "555") if n == 4 else None
        lau = Lau(mon)
        sup = make(mon, lau, Watcher(lambda n, pid: []))
        sup.run()
        self.assertEqual(len(lau.calls), 1)
        self.assertTrue(lau.flags[0])

    def test_stable_pid_no_launch(self):
        mon, w = Mon(), Watcher(lambda n, pid: [])
        lau = Lau(mon)
        make(mon, lau, w).run()
        self.assertEqual(lau.calls, [])

    def test_disabled_by_config(self):
        mon = Mon("100")
        mon.on_observe = lambda n: setattr(mon, "pid_value", "555") if n == 4 else None
        lau = Lau(mon)
        w = Watcher(lambda n, pid: [(278, "x")])
        make(mon, lau, w, rejoin_on_pid_change=False, rejoin_on_disconnect=False).run()
        self.assertEqual(lau.calls, [])

    def test_force_stop_disabled_in_config(self):
        mon = Mon()
        lau = Lau(mon)
        w = Watcher(lambda n, pid: [(278, "x")] if n == 2 and pid == "100" else [])
        make(mon, lau, w, force_stop_before_launch=False).run()
        self.assertEqual(lau.flags, [False])

    def test_no_pid_visible_never_triggers(self):
        mon = Mon("")
        lau = Lau(mon)
        make(mon, lau, Watcher(lambda n, pid: [(278, "x")])).run()
        self.assertEqual(lau.calls, [])


if __name__ == "__main__":
    unittest.main()
