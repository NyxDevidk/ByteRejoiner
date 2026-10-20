"""Opening the selected experience, plus retry/backoff policy."""
import re
from collections import namedtuple

LaunchResult = namedtuple("LaunchResult", "sent method detail")

_AM_ERR = re.compile(r"(Error:|Exception|does not exist|unable to resolve|Permission Denial|Warning: Activity not started)", re.I)


def deep_links(place_id):
    """Roblox deep links, most specific first. place_id is the number in the /games/<id>/ URL."""
    return [
        f"roblox://experiences/start?placeId={place_id}",
        f"roblox://placeId={place_id}",
    ]


class Launcher:
    def __init__(self, shell, pkg):
        self.shell, self.pkg = shell, pkg

    def _am_view(self, url):
        # -p pins the intent to the Roblox app so a browser can't grab it.
        return self.shell.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", self.pkg], timeout=20)

    @staticmethod
    def _am_ok(r):
        return (not r.missing and not r.timed_out and r.rc == 0 and not _AM_ERR.search(r.out + "\n" + r.err))

    def launch(self, place_id):
        """Try each launch method until one is accepted by Android.

        'sent' means Android accepted the intent, NOT that Roblox is running or that the game loaded.
        The caller must verify with the monitor.
        """
        errors = []
        for url in deep_links(place_id):
            r = self._am_view(url)
            if self._am_ok(r):
                return LaunchResult(True, f"am start {url}", (r.out or "").splitlines()[-1] if r.out else "")
            text = (r.err or r.out or "").strip()
            errors.append(f"am {url}: {text.splitlines()[0] if text else 'rc=' + str(r.rc)}")
        if self.shell.has("termux-open-url"):
            url = deep_links(place_id)[0]
            r = self.shell.run(["termux-open-url", url], timeout=15)
            if not r.missing and not r.timed_out and r.rc == 0:
                return LaunchResult(True, f"termux-open-url {url}", "")
            errors.append(f"termux-open-url: rc={r.rc}")
        return LaunchResult(False, "", "; ".join(errors) or "no launch method available")


class RetryPolicy:
    """Backoff for failed attempts within one incident. attempt is 1-based."""

    def __init__(self, max_retries=5, base=10.0, factor=2.0, cap=300.0):
        self.max_retries, self.base, self.factor, self.cap = max_retries, base, factor, cap

    def delay(self, attempt):
        return min(self.base * (self.factor ** max(0, attempt - 1)), self.cap)

    def exhausted(self, attempts_made):
        return self.max_retries != 0 and attempts_made >= self.max_retries
