import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ugphone import Result  # noqa: E402


class FakeShell:
    """Scriptable shell. handler: callable(args) -> Result | None (None = command not found)."""

    def __init__(self, handler=None, commands=()):
        self.handler, self.commands, self.calls = handler, set(commands), []
        self.prefix = []

    def run(self, args, timeout=10):
        self.calls.append(list(args))
        r = self.handler(list(args)) if self.handler else None
        return r if r is not None else Result(127, "", "not found", True, False)

    def has(self, name):
        return name in self.commands


def ok(out=""):
    return Result(0, out, "", False, False)


def rc(code, out="", err=""):
    return Result(code, out, err, False, False)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeStop:
    """Event stand-in: wait() advances the fake clock and stops after `limit` waits."""

    def __init__(self, clock, limit=1000):
        self.clock, self.limit, self.n, self._set = clock, limit, 0, False
        self.waits = []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, s):
        self.waits.append(s)
        self.clock.t += s
        self.n += 1
        if self.n >= self.limit:
            self._set = True
        return self._set
