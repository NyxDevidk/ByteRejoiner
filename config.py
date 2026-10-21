"""Settings persistence, validation, and Roblox URL/ID parsing. No secrets are ever stored."""
import json
import os
import re
import tempfile
from urllib.parse import urlparse, parse_qs

from webhook import URL_RE

DEFAULTS = {
    "experience_id": "",
    "saved_experiences": [],          # [{"name": str, "id": str}]
    "roblox_package": "",             # "" = auto-detect
    "shell_prefix": "",               # "", "su -c", or "adb -s localhost:5555 shell" (opt-in, see README)
    "poll_interval": 5.0,             # seconds between checks
    "confirm_polls": 3,               # consecutive "stopped" readings before it counts as closed
    "reopen_delay": 5.0,              # seconds to wait after confirmed close
    "max_retries": 5,                 # 0 = unlimited
    "retry_delay": 10.0,              # first delay between failed attempts
    "backoff_factor": 2.0,
    "retry_delay_max": 300.0,
    "min_launch_interval": 20.0,      # hard floor between any two launches (duplicate guard)
    "verify_timeout": 45.0,           # seconds to wait for Roblox to show up after a launch
    "give_up_cooldown": 300.0,        # pause after exhausting retries, then try a fresh incident
    "launch_on_start": False,
    "recover_if_closed_on_start": True,
    "rejoin_on_disconnect": True,     # relaunch when Roblox logs a disconnect/kick code (process still alive)
    "rejoin_on_pid_change": True,     # relaunch when Roblox restarted by itself (new pid)
    "force_stop_before_launch": True, # am force-stop first when Roblox is alive but out of the game
    "webhook_url": "",                # Discord webhook for status messages ("" = off). Keep it private.
    "webhook_username": "Roblox Auto-Rejoin",
    "blind_relaunch_minutes": 0,     # fallback when no probe works: relaunch every N min (0 = off)
    "log_level": "info",              # debug | info | warn | error
}

_LOG_LEVELS = ("debug", "info", "warn", "error")
# (min, max) for numeric fields; ints listed separately.
_RANGES = {
    "poll_interval": (1, 600), "reopen_delay": (0, 3600), "retry_delay": (1, 3600),
    "backoff_factor": (1, 10), "retry_delay_max": (1, 86400), "min_launch_interval": (0, 3600),
    "verify_timeout": (5, 600), "give_up_cooldown": (0, 86400), "blind_relaunch_minutes": (0, 1440),
}
_INT_RANGES = {"confirm_polls": (1, 20), "max_retries": (0, 1000)}
_BOOLS = ("launch_on_start", "recover_if_closed_on_start", "rejoin_on_disconnect",
          "rejoin_on_pid_change", "force_stop_before_launch")
_PKG_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$")

_WEBHOOK_RE = URL_RE
_ID_RE = re.compile(r"^\d{1,19}$")
_GAME_PATH_RE = re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/(\d{1,19})(?:/|$)", re.I)


class ConfigError(ValueError):
    pass


def parse_experience(text):
    """Return the place ID (digits) from a Roblox games URL, deep link, or bare ID. Raise ConfigError otherwise."""
    s = (text or "").strip()
    if not s:
        raise ConfigError("Empty input. Enter a Roblox game URL or a numeric ID.")
    if _ID_RE.match(s):
        if int(s) == 0:
            raise ConfigError("ID 0 is not a valid experience.")
        return s
    candidate = s if "://" in s else "https://" + s
    try:
        u = urlparse(candidate)
    except ValueError:
        raise ConfigError(f"Not a valid URL: {s!r}")
    host = (u.hostname or "").lower()
    if u.scheme in ("http", "https") and (host == "roblox.com" or host.endswith(".roblox.com")):
        m = _GAME_PATH_RE.search(u.path + "/")
        if m:
            return m.group(1)
        q = parse_qs(u.query)
        for key in ("placeId", "placeid"):
            if q.get(key) and _ID_RE.match(q[key][0]):
                return q[key][0]
        raise ConfigError("Roblox URL has no game ID. Expected https://www.roblox.com/games/<id>/<name>.")
    if u.scheme == "roblox":
        q = parse_qs(u.query)
        for key in ("placeId", "placeid"):
            if q.get(key) and _ID_RE.match(q[key][0]):
                return q[key][0]
        m = re.search(r"placeId=(\d+)", s)
        if m:
            return m.group(1)
        raise ConfigError("roblox:// link has no placeId.")
    raise ConfigError(f"{s!r} is not a Roblox games URL or numeric ID. "
                      "Example: https://www.roblox.com/games/1234567890/Example-Game")


def validate(cfg):
    """Return a cleaned copy of cfg; raise ConfigError naming the first bad field."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})

    if out["experience_id"] != "":
        out["experience_id"] = parse_experience(str(out["experience_id"]))
    for key, (lo, hi) in _RANGES.items():
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ConfigError(f"{key} must be a number between {lo} and {hi} (got {v!r}).")
        out[key] = float(v)
    for key, (lo, hi) in _INT_RANGES.items():
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ConfigError(f"{key} must be a whole number between {lo} and {hi} (got {v!r}).")
    for key in _BOOLS:
        if not isinstance(out[key], bool):
            raise ConfigError(f"{key} must be true or false.")
    if out["log_level"] not in _LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(_LOG_LEVELS)}.")
    pkg = out["roblox_package"]
    if not isinstance(pkg, str) or (pkg and not _PKG_RE.match(pkg)):
        raise ConfigError("roblox_package must be an Android package name like com.roblox.client, or empty.")
    if not isinstance(out["shell_prefix"], str):
        raise ConfigError("shell_prefix must be a string.")
    hook = out["webhook_url"]
    if not isinstance(hook, str) or (hook and not _WEBHOOK_RE.match(hook.strip())):
        raise ConfigError("webhook_url must be a Discord webhook URL "
                          "(https://discord.com/api/webhooks/<id>/<token>) or empty.")
    out["webhook_url"] = hook.strip()
    name = out["webhook_username"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ConfigError("webhook_username must be 1-80 characters.")
    saved = out["saved_experiences"]
    if not isinstance(saved, list):
        raise ConfigError("saved_experiences must be a list.")
    clean = []
    for item in saved:
        if not isinstance(item, dict) or "id" not in item:
            raise ConfigError("saved_experiences entries need an 'id'.")
        clean.append({"name": str(item.get("name") or item["id"])[:60], "id": parse_experience(str(item["id"]))})
    out["saved_experiences"] = clean
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULTS)

    def load(self):
        """Load config; create defaults if missing. A corrupt file is kept as .bad and replaced."""
        if not os.path.exists(self.path):
            self.data = validate({})
            self.save()
            return self.data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ConfigError("top level must be a JSON object")
            self.data = validate(raw)
        except (ValueError, OSError) as exc:  # JSONDecodeError and ConfigError are ValueErrors
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            self.data = validate({})
            self.save()
            raise ConfigError(f"config.json was invalid ({exc}); defaults restored, old file saved as config.json.bad")
        return self.data

    def save(self):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, **changes):
        """Validate and persist. On failure the previous settings stay in place."""
        new = validate({**self.data, **changes})
        self.data = new
        self.save()
        return new

    def save_experience(self, exp_id, name=""):
        saved = [e for e in self.data["saved_experiences"] if e["id"] != exp_id]
        saved.append({"name": name or exp_id, "id": exp_id})
        return self.update(saved_experiences=saved, experience_id=exp_id)
