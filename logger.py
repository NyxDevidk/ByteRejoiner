"""Terminal + rotating file logging. Standard library only."""
import logging
import logging.handlers
import os
import sys

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
_NAME = "rejoin"


class _TermFormatter(logging.Formatter):
    def format(self, record):
        tag = {"WARNING": "WARN"}.get(record.levelname, record.levelname)
        stamp = self.formatTime(record, "%H:%M:%S")
        return f"{stamp} [{tag}] {record.getMessage()}"


def setup(home, verbosity="info"):
    """Configure the shared logger. Safe to call repeatedly (e.g. after a settings change)."""
    log = logging.getLogger(_NAME)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    level = _LEVELS.get(verbosity, logging.INFO)

    term = logging.StreamHandler(sys.stdout)
    term.setLevel(level)
    term.setFormatter(_TermFormatter())
    log.addHandler(term)

    try:
        path = os.path.join(home, "rejoin.log")
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)
    except OSError as exc:
        log.warning("file logging disabled: %s", exc)
    return log


def get():
    return logging.getLogger(_NAME)
