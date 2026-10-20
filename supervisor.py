"""Orchestrates monitor + launcher: poll, confirm close, wait, launch, verify, back off."""
import time

import logger
from launcher import RetryPolicy
from monitor import (RUNNING_STATES, STOPPED, UNKNOWN, StateTracker)


class Supervisor:
    def __init__(self, cfg, monitor, launcher, stop, net_check, clock=time.monotonic, on_state=None):
        """stop: object with .is_set() and .wait(seconds) -> bool (threading.Event fits)."""
        self.cfg, self.monitor, self.launcher = cfg, monitor, launcher
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

    def _verify(self):
        """After a launch, wait for Roblox to appear. Returns 'running', 'unverified' or 'absent'."""
        deadline = self.clock() + self.cfg["verify_timeout"]
        saw_unknown = False
        while self.clock() < deadline and not self.stop.is_set():
            st = self.monitor.observe()
            self.roblox = st
            self._publish()
            if st in RUNNING_STATES:
                return "running"
            saw_unknown = saw_unknown or st == UNKNOWN
            if not self._sleep(min(2.0, self.cfg["poll_interval"])):
                break
        if self.monitor.can_detect_stop and not saw_unknown:
            return "absent"
        return "unverified"

    # -- recovery ----------------------------------------------------------
    def recover(self, reason, delay=None):
        """One incident: launch with retries. Returns True when Roblox is up (or launch accepted but unverifiable)."""
        exp = self.cfg["experience_id"]
        if not exp:
            self.log.error("No experience selected; cannot relaunch. Choose one from the menu first.")
            return False
        self.incidents += 1
        self.mode = "RECOVERING"
        self._publish()
        self.log.info("Incident #%d: %s", self.incidents, reason)
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
            res = self.launcher.launch(exp)
            self._last_launch = self.clock()
            if res.sent:
                self.log.debug("Launch accepted via %s", res.method)
                outcome = self._verify()
                if outcome == "running":
                    self.log.info("Roblox launch detected. (Process is up; entering the game server is not verifiable.)")
                    self._finish()
                    return True
                if outcome == "unverified":
                    self.log.warning("Launch accepted by Android, but this shell cannot confirm Roblox started.")
                    self._finish(armed=False)
                    return True
                self.log.warning("Launch accepted but Roblox did not appear within %.0fs.", self.cfg["verify_timeout"])
            else:
                self.log.error("Launch failed: %s", res.detail)
            if self.policy.exhausted(attempts):
                self.log.error("Giving up after %d attempts. Pausing %.0fs, then trying a fresh incident.",
                               attempts, self.cfg["give_up_cooldown"])
                self.mode = "GAVE_UP"
                self._publish()
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
        self._publish()

    # -- main loop ---------------------------------------------------------
    def run(self):
        cfg = self.cfg
        self.log.info("Monitoring started.")
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

    def _log_change(self, prev, st):
        if st == STOPPED:
            self.log.info("Roblox status: STOPPED (confirming...)")
        else:
            self.log.info("Roblox status: %s", st)
