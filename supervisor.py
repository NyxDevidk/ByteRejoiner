"""Orchestrates monitor + launcher: poll, confirm close, wait, launch, verify, back off."""
import time

import logger
from launcher import RetryPolicy
from monitor import (RUNNING_STATES, STOPPED, UNKNOWN, StateTracker)


class Supervisor:
    def __init__(self, cfg, monitor, launcher, stop, net_check, clock=time.monotonic, on_state=None, watcher=None, notifier=None):
        """stop: object with .is_set() and .wait(seconds) -> bool (threading.Event fits).
        watcher: LogcatWatcher-like (seed/poll) or None to disable disconnect detection.
        notifier: object with .send(level, title, description, fields) (webhook) or None."""
        self.cfg, self.monitor, self.launcher, self.watcher = cfg, monitor, launcher, watcher
        self.notifier = notifier
        self.last_pid = ""
        self.stop, self.net_check, self.clock = stop, net_check, clock
        self.on_state = on_state or (lambda *_: None)
        self.log = logger.get()
        self.tracker = StateTracker(cfg["confirm_polls"], cfg["recover_if_closed_on_start"])
        self.policy = RetryPolicy(cfg["max_retries"], cfg["retry_delay"], cfg["backoff_factor"], cfg["retry_delay_max"])
        self.mode = "MONITORING"
        self.roblox = UNKNOWN
        self.total_attempts = 0       # launches sent since monitoring began
        self.incidents = 0
        self._last_launch = None      # monotonic time of the last launch we sent
        self._last_blind = self.clock()
        self._warned_blind = False

    # -- helpers -----------------------------------------------------------
    def _notify(self, level, title, description="", **fields):
        if self.notifier:
            fields.setdefault("Experience", self.cfg["experience_id"])
            self.notifier.send(level, title, description, fields)

    def _publish(self):
        self.on_state(self.mode, self.roblox, self.total_attempts)

    def _sleep(self, seconds):
        """Interruptible sleep. Returns False if stop was requested."""
        return not self.stop.wait(max(0.0, seconds))

    def _enforce_cooldown(self):
        """Duplicate-launch guard: never send two launches closer than min_launch_interval."""
        if self._last_launch is None:
            return True
        wait = self.cfg["min_launch_interval"] - (self.clock() - self._last_launch)
        if wait > 0:
            self.log.debug("cooldown: waiting %.0fs before the next launch", wait)
            return self._sleep(wait)
        return True

    def _wait_for_network(self):
        warned = False
        while not self.net_check():
            if not warned:
                self.log.warning("Network unreachable; pausing recovery until it returns.")
                warned = True
            if not self._sleep(min(10.0, self.cfg["poll_interval"] * 2)):
                return False
        if warned:
            self.log.info("Network is back.")
        return True

    def _verify(self, old_pid=""):
        """After a launch, wait for Roblox to appear. Returns 'running', 'unverified' or 'absent'.
        With old_pid set (force-stop used), the old process does not count as a successful start."""
        deadline = self.clock() + self.cfg["verify_timeout"]
        saw_unknown = False
        while self.clock() < deadline and not self.stop.is_set():
            st = self.monitor.observe()
            self.roblox = st
            self._publish()
            if st in RUNNING_STATES and not (old_pid and self.monitor.pid() == old_pid):
                return "running"
            saw_unknown = saw_unknown or st == UNKNOWN
            if not self._sleep(min(2.0, self.cfg["poll_interval"])):
                break
        if self.monitor.can_detect_stop and not saw_unknown:
            return "absent"
        return "unverified"

    # -- recovery ----------------------------------------------------------
    def _rebaseline(self):
        """Remember the current pid and mark the existing log backlog as seen."""
        self.last_pid = self.monitor.pid()
        if self.watcher and self.last_pid:
            self.watcher.seed(self.last_pid)

    def _health_check(self):
        """Roblox is running: is it still in a healthy session? Returns a reason string to rejoin, else None.

        Catches the case the process check cannot: Roblox alive but dropped to its home screen,
        either restarted by Android (new pid) or after a disconnect/kick (code in logcat)."""
        pid = self.monitor.pid()
        if not pid:
            return None
        if not self.last_pid:
            self.last_pid = pid
            if self.watcher:
                self.watcher.seed(pid)
            return None
        if pid != self.last_pid:
            old, self.last_pid = self.last_pid, pid
            if self.watcher:
                self.watcher.seed(pid)
            if self.cfg["rejoin_on_pid_change"]:
                return f"Roblox restarted by itself (pid {old} -> {pid})"
            return None
        if self.watcher and self.cfg["rejoin_on_disconnect"]:
            hits = self.watcher.poll(pid)
            if hits:
                code, line = hits[-1]
                self.log.debug("disconnect line: %s", line[:200])
                return f"Roblox reported disconnect code {code} (process still running)"
        return None

    def recover(self, reason, delay=None, force_stop=False):
        """One incident: launch with retries. Returns True when Roblox is up (or launch accepted but unverifiable).
        force_stop: Roblox is still alive, so stop it first (needs root/adb; ignored when disabled in config)."""
        force_stop = force_stop and self.cfg["force_stop_before_launch"]
        old_pid = self.monitor.pid() if force_stop else ""
        exp = self.cfg["experience_id"]
        if not exp:
            self.log.error("No experience selected; cannot relaunch. Choose one from the menu first.")
            return False
        self.incidents += 1
        self.mode = "RECOVERING"
        self._publish()
        self.log.info("Incident #%d: %s", self.incidents, reason)
        self._notify("warn", "Roblox needs a rejoin", reason, Incident=self.incidents)
        attempts = 0
        wait = self.cfg["reopen_delay"] if delay is None else delay
        while not self.stop.is_set():
            if wait > 0:
                self.log.info("Waiting %.0f seconds before launching.", wait)
                if not self._sleep(wait):
                    break
            if not self._wait_for_network() or not self._enforce_cooldown():
                break
            attempts += 1
            self.total_attempts += 1
            self.log.info("Launching experience %s (attempt %d%s).", exp, attempts,
                          "" if not self.cfg["max_retries"] else f"/{self.cfg['max_retries']}")
            res = self.launcher.launch(exp, force_stop=force_stop)
            self._last_launch = self.clock()
            if force_stop and getattr(self.launcher, "force_stop_ok", None) is False:
                self.log.warning("am force-stop failed (needs root/adb); relaunching without it.")
                force_stop = False
            if res.sent:
                self.log.debug("Launch accepted via %s", res.method)
                outcome = self._verify(old_pid if force_stop else "")
                if outcome == "running":
                    self.log.info("Roblox launch detected. (Process is up; entering the game server is not verifiable.)")
                    self._notify("success", "Roblox relaunched", "Process is up. Joining the server is not verifiable.",
                                 Attempts=attempts)
                    self._finish()
                    return True
                if outcome == "unverified":
                    self.log.warning("Launch accepted by Android, but this shell cannot confirm Roblox started.")
                    self._notify("warn", "Launch sent, not confirmed", "Android accepted the launch; cannot verify from this shell.")
                    self._finish(armed=False)
                    return True
                self.log.warning("Launch accepted but Roblox did not appear within %.0fs.", self.cfg["verify_timeout"])
            else:
                self.log.error("Launch failed: %s", res.detail)
                self._notify("error", "Launch failed", res.detail[:500], Attempt=attempts)
            if self.policy.exhausted(attempts):
                self.log.error("Giving up after %d attempts. Pausing %.0fs, then trying a fresh incident.",
                               attempts, self.cfg["give_up_cooldown"])
                self.mode = "GAVE_UP"
                self._publish()
                self._notify("error", "Giving up for now", f"{attempts} attempts failed. Pausing "
                             f"{self.cfg['give_up_cooldown']:.0f}s before trying again.")
                self._sleep(self.cfg["give_up_cooldown"])
                self._finish(armed=True)
                return False
            wait = self.policy.delay(attempts)
            self.log.info("Retrying with backoff.")
        return False

    def _finish(self, armed=True):
        self.tracker.reset(armed=armed)
        self.mode = "MONITORING"
        self._last_blind = self.clock()
        self._rebaseline()
        self._publish()

    # -- main loop ---------------------------------------------------------
    def run(self):
        cfg = self.cfg
        self.log.info("Monitoring started.")
        self._notify("info", "Monitoring started", Detection="full" if self.monitor.can_detect_stop else "limited")
        if not self.monitor.can_detect_stop:
            self.log.warning("No trustworthy process probe on this shell: a closed Roblox cannot be detected.")
            for name, (ok, note) in self.monitor.calibration.items():
                self.log.warning("  %s: %s", name, note)
            if cfg["blind_relaunch_minutes"]:
                self.log.warning("Fallback active: relaunching every %d min.", cfg["blind_relaunch_minutes"])
            else:
                self.log.warning("Set blind_relaunch_minutes or use a shell_prefix (see README). Positive sightings still tracked.")
        if cfg["launch_on_start"]:
            self.recover("launch_on_start is enabled", delay=0)
        while not self.stop.is_set():
            st = self.monitor.observe()
            prev, self.roblox = self.roblox, st
            if st != prev:
                self._log_change(prev, st)
                self._publish()
            if self.tracker.update(st) == "closed":
                self.log.warning("Roblox process is no longer detected.")
                self.recover("Roblox closed unexpectedly")
            elif st in RUNNING_STATES and (reason := self._health_check()):
                self.log.warning(reason)
                self.recover(reason, force_stop=True)
            elif (not self.monitor.can_detect_stop and cfg["blind_relaunch_minutes"]
                  and self.clock() - self._last_blind >= cfg["blind_relaunch_minutes"] * 60):
                self.recover("periodic blind relaunch (no process detection available)", delay=0)
            if self.tracker.unknown_streak == 10:
                self.log.warning("Roblox status unknown for 10 polls in a row; detection may be failing.")
            if not self._sleep(cfg["poll_interval"]):
                break
        self.mode = "STOPPED"
        self._publish()
        self.log.info("Monitoring stopped.")
        self._notify("info", "Monitoring stopped", Rejoins=self.total_attempts)

    def _log_change(self, prev, st):
        if st == STOPPED:
            self.log.info("Roblox status: STOPPED (confirming...)")
        else:
            self.log.info("Roblox status: %s", st)
