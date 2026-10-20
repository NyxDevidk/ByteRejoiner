"""Roblox status detection and the stopped-confirmation state machine.

Key rule: a probe only gets to say "not running" if it was calibrated, i.e. it demonstrably
can see processes of other apps. Without root, Android 7+ often hides them from Termux, so an
uncalibrated "not found" would look identical to a crash. In that case the answer is UNKNOWN.
A positive sighting is always believed.
"""
import re
from collections import namedtuple

STOPPED, UNKNOWN = "STOPPED", "UNKNOWN"
RUNNING, FOREGROUND, BACKGROUND = "RUNNING", "FOREGROUND", "BACKGROUND"
RUNNING_STATES = (RUNNING, FOREGROUND, BACKGROUND)

Obs = namedtuple("Obs", "running foreground")   # each True / False / None (= could not tell)
NO_INFO = Obs(None, None)

# Processes that always exist on a healthy Android system; if a probe can't see them, it can't see Roblox either.
_SYSTEM_PROCS = ("system_server", "zygote64", "zygote", "surfaceflinger")


def _digits(s):
    return bool(re.search(r"\d", s))


class PidofProbe:
    name = "pidof"

    def calibrate(self, shell):
        if not shell.has("pidof"):
            return False, "pidof not available"
        for proc in _SYSTEM_PROCS:
            r = shell.run(["pidof", proc])
            if r.rc == 0 and _digits(r.out):
                return True, f"can see {proc}"
        return False, "cannot see system processes (process list is hidden from this shell)"

    def check(self, shell, pkg):
        r = shell.run(["pidof", pkg])
        if r.missing or r.timed_out or r.rc not in (0, 1):
            return NO_INFO
        return Obs(r.rc == 0 and _digits(r.out), None)


class PsProbe:
    name = "ps"

    def _list(self, shell):
        for args in (["ps", "-A", "-o", "PID,NAME"], ["ps", "-A"], ["ps"]):
            r = shell.run(args)
            if r.rc == 0 and r.out:
                return [ln.split()[-1] for ln in r.out.splitlines() if ln.split()]
        return None

    def calibrate(self, shell):
        if not shell.has("ps"):
            return False, "ps not available"
        names = self._list(shell)
        if names is None:
            return False, "ps failed"
        if any(n in _SYSTEM_PROCS for n in names):
            return True, "can see system processes"
        return False, f"ps lists only {len(names)} processes (others hidden)"

    def check(self, shell, pkg):
        names = self._list(shell)
        if names is None:
            return NO_INFO
        return Obs(any(n == pkg or n.startswith(pkg + ":") for n in names), None)


class DumpsysProbe:
    """Needs the DUMP permission: works with root or an adb-shell prefix, normally denied to Termux."""
    name = "dumpsys"

    def _dump(self, shell, *args):
        r = shell.run(["dumpsys", *args], timeout=15)
        if r.missing or r.timed_out or r.rc != 0:
            return None
        if "Permission Denial" in r.out or "Permission Denial" in r.err:
            return None
        return r.out

    def calibrate(self, shell):
        if not shell.has("dumpsys"):
            return False, "dumpsys not available"
        out = self._dump(shell, "activity", "processes")
        if out is None or len(out) < 200:
            return False, "dumpsys denied (needs adb-shell or root privileges)"
        return True, "dumpsys readable"

    def check(self, shell, pkg):
        out = self._dump(shell, "activity", "processes")
        if out is None:
            return NO_INFO
        running = re.search(r"ProcessRecord\{\S+ \d+:" + re.escape(pkg) + r"(?::\S+)?/", out) is not None
        if not running:
            return Obs(False, None)
        return Obs(True, self._foreground(shell, pkg))

    def _foreground(self, shell, pkg):
        out = self._dump(shell, "activity", "activities")
        if out:
            m = re.search(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[^\n]*?ActivityRecord\{\S+ \S+ ([\w.]+)/", out)
            if m:
                return m.group(1) == pkg
        out = self._dump(shell, "window")
        if out:
            m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/", out)
            if m:
                return m.group(1) == pkg
        return None


class Monitor:
    def __init__(self, shell, pkg, probes=None):
        self.shell, self.pkg = shell, pkg
        self.probes = probes if probes is not None else [PidofProbe(), PsProbe(), DumpsysProbe()]
        self.trusted = []          # probes allowed to report "not running"
        self.calibration = {}      # name -> (ok, note) for diagnostics

    def calibrate(self):
        self.trusted = []
        for p in self.probes:
            ok, note = p.calibrate(self.shell)
            self.calibration[p.name] = (ok, note)
            if ok:
                self.trusted.append(p)
        return self.can_detect_stop

    @property
    def can_detect_stop(self):
        return bool(self.trusted)

    def observe(self):
        """One poll -> STOPPED / FOREGROUND / BACKGROUND / RUNNING / UNKNOWN."""
        negative = False
        running_seen = False
        fg = None
        for p in self.probes:
            try:
                obs = p.check(self.shell, self.pkg)
            except Exception:  # a broken probe must never kill the monitor
                continue
            if obs.running:
                running_seen = True
                if obs.foreground is not None:
                    fg = obs.foreground
                break
            if obs.running is False and p in self.trusted:
                negative = True
        if running_seen:
            if fg is None:
                for p in self.trusted:
                    if isinstance(p, DumpsysProbe):
                        try:
                            fg = p.check(self.shell, self.pkg).foreground
                        except Exception:
                            pass
            return RUNNING if fg is None else (FOREGROUND if fg else BACKGROUND)
        return STOPPED if negative else UNKNOWN


class StateTracker:
    """Turns a stream of statuses into a single 'closed' event.

    - UNKNOWN is no information: it neither confirms nor resets a stopped streak.
    - Needs `confirm_polls` consecutive STOPPED readings, so one flaky poll cannot trigger a launch.
    - Stays disarmed until Roblox has been seen running, unless recover_on_start is set.
    - Background/foreground changes are never events.
    """

    def __init__(self, confirm_polls=3, recover_on_start=True):
        self.confirm = confirm_polls
        self.armed = recover_on_start
        self.stopped_streak = 0
        self.unknown_streak = 0
        self.last = UNKNOWN

    def update(self, status):
        self.last = status
        if status == UNKNOWN:
            self.unknown_streak += 1
            return None
        self.unknown_streak = 0
        if status in RUNNING_STATES:
            self.armed = True
            self.stopped_streak = 0
            return None
        # STOPPED
        if not self.armed:
            return None
        self.stopped_streak += 1
        if self.stopped_streak >= self.confirm:
            self.stopped_streak = 0
            return "closed"
        return None

    def reset(self, armed=True):
        self.stopped_streak = 0
        self.unknown_streak = 0
        self.armed = armed
