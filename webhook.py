"""Discord webhook notifications. Standard library only.

Sends happen on a background thread so a slow or failing Discord never delays monitoring.
The webhook URL is a secret (anyone holding it can post to the channel): it is only read from
config.json, and logs show a masked form.
"""
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import logger

URL_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+/?$")
COLORS = {"info": 0x3498DB, "success": 0x2ECC71, "warn": 0xE67E22, "error": 0xE74C3C}
_USER_AGENT = "RobloxAutoRejoin/1.0"   # Discord rejects the default Python user agent with 403


def is_valid_url(url):
    return bool(URL_RE.match(url or ""))


def mask(url):
    m = re.search(r"/webhooks/(\d+)/", url or "")
    return f"discord webhook ...{m.group(1)[-4:]}" if m else "webhook"


def build_payload(username, level, title, description="", fields=None):
    embed = {
        "title": title[:250],
        "color": COLORS.get(level, COLORS["info"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        embed["description"] = description[:1800]
    if fields:
        embed["fields"] = [{"name": str(k)[:250], "value": str(v)[:900] or "-", "inline": True}
                           for k, v in list(fields.items())[:10]]
    return {"username": username[:80], "embeds": [embed], "allowed_mentions": {"parse": []}}


def _http_post(url, payload, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            return exc.code, ""


class NullNotifier:
    enabled = False

    def send(self, *args, **kwargs):
        pass

    def send_sync(self, *args, **kwargs):
        return False, "webhook not configured"

    def close(self, timeout=5):
        pass


class Notifier:
    enabled = True

    def __init__(self, url, username="Roblox Auto-Rejoin", post=_http_post, sleep=time.sleep):
        self.url, self.username, self._post, self._sleep = url, username, post, sleep
        self.log = logger.get()
        self._q = queue.Queue(maxsize=50)
        self._worker = threading.Thread(target=self._run, name="webhook", daemon=True)
        self._worker.start()

    def send(self, level, title, description="", fields=None):
        """Queue a message. Never blocks, never raises; drops when the queue is full."""
        try:
            self._q.put_nowait(build_payload(self.username, level, title, description, fields))
        except queue.Full:
            self.log.debug("webhook queue full, dropped: %s", title)

    def send_sync(self, level, title, description="", fields=None):
        """Deliver now and report (ok, detail). Used by the test command."""
        return self._deliver(build_payload(self.username, level, title, description, fields))

    def close(self, timeout=5):
        """Flush queued messages (bounded wait) and stop the worker."""
        self._q.put(None)
        self._worker.join(timeout)

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            self._deliver(item)

    def _deliver(self, payload):
        for attempt in (1, 2):
            try:
                status, body = self._post(self.url, payload)
            except Exception as exc:   # never include str(exc): it could echo the URL
                msg = f"network error ({exc.__class__.__name__})"
                self.log.warning("Webhook not delivered to %s: %s", mask(self.url), msg)
                return False, msg
            if status in (200, 204):
                return True, "delivered"
            if status == 429 and attempt == 1:
                try:
                    wait = float(json.loads(body).get("retry_after", 1))
                except (ValueError, AttributeError):
                    wait = 1.0
                self._sleep(min(max(wait, 0.5), 30.0))
                continue
            msg = {401: "invalid webhook token", 404: "webhook deleted or wrong URL"}.get(status, f"HTTP {status}")
            self.log.warning("Webhook rejected by Discord (%s): %s", mask(self.url), msg)
            return False, msg
        return False, "rate limited"


def make_notifier(cfg):
    url = cfg.get("webhook_url", "")
    if not url:
        return NullNotifier()
    return Notifier(url, cfg.get("webhook_username") or "Roblox Auto-Rejoin")
