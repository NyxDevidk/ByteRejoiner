#!/usr/bin/env python3
"""Roblox Auto-Rejoin for UGPhone (Termux). Usage: python main.py [menu|run|diagnose|set-game ID_OR_URL]"""
import argparse
import os
import signal
import sys
import threading

import config
import diagnostics
import logger
import ugphone
from launcher import Launcher
from monitor import Monitor
from supervisor import Supervisor

HOME = os.environ.get("ROBLOX_REJOIN_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HOME, "config.json")
LOCK_PATH = os.path.join(HOME, "rejoin.lock")


# -- single-instance lock ---------------------------------------------------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def acquire_lock():
    """Refuse to run two monitors at once (they would double-launch). Stale locks are reclaimed."""
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(LOCK_PATH) as fh:
                    pid = int(fh.read().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                print(f"Another monitor is already running (pid {pid}). Stop it first.")
                return False
            try:
                os.unlink(LOCK_PATH)
            except OSError:
                return False
    return False


def release_lock():
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


# -- monitoring ---------------------------------------------------------------
def monitor_forever(store, stop):
    cfg = store.data
    log = logger.get()
    shell = ugphone.Shell(cfg["shell_prefix"])
    pkg = ugphone.find_roblox_package(shell, cfg["roblox_package"])
    if not pkg:
        log.error("Roblox package not found. Run diagnose; set roblox_package in config.json if needed.")
        return 2
    if not cfg["experience_id"]:
        log.error("No experience selected. Use the menu (or `set-game`) first.")
        return 2
    if not acquire_lock():
        return 3
    try:
        if ugphone.wake_lock(shell):
            log.info("Termux wake lock acquired.")
        else:
            log.warning("No termux-wake-lock; Android may suspend Termux when idle (see README).")
        mon = Monitor(shell, pkg)
        mon.calibrate()

        def show(mode, roblox, attempts):
            print(f"\n=== Roblox Auto-Rejoin | UGPhone ===\n"
                  f"Status: {mode}\nSelected experience: {cfg['experience_id']}\n"
                  f"Roblox status: {roblox}\nReconnect attempts: {attempts}\n")

        sup = Supervisor(cfg, mon, Launcher(shell, pkg), stop, ugphone.network_ok, on_state=show)
        sup.run()
        return 0
    finally:
        release_lock()


def run_until_interrupt(store):
    stop = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (ValueError, OSError, AttributeError):
        pass
    try:
        print("Monitoring... press Ctrl+C to stop.")
        return monitor_forever(store, stop)
    finally:
        signal.signal(signal.SIGINT, old)


# -- menu -------------------------------------------------------------------------
SETTINGS = [
    ("poll_interval", "Polling interval (s)"), ("confirm_polls", "Stopped readings to confirm a close"),
    ("reopen_delay", "Delay before reopening (s)"), ("max_retries", "Max retries (0 = unlimited)"),
    ("retry_delay", "Delay between retries (s)"), ("backoff_factor", "Backoff factor"),
    ("retry_delay_max", "Max retry delay (s)"), ("min_launch_interval", "Min seconds between launches"),
    ("verify_timeout", "Launch verify timeout (s)"), ("give_up_cooldown", "Pause after giving up (s)"),
    ("launch_on_start", "Launch on startup (true/false)"),
    ("recover_if_closed_on_start", "Recover if already closed at start (true/false)"),
    ("blind_relaunch_minutes", "Blind relaunch every N min (0 = off)"),
    ("log_level", "Log level (debug/info/warn/error)"),
    ("roblox_package", "Roblox package ('' = auto)"), ("shell_prefix", "Shell prefix ('' = none)"),
]


def _coerce(key, text):
    text = text.strip()
    default = config.DEFAULTS[key]
    if isinstance(default, bool):
        if text.lower() in ("true", "t", "yes", "y", "1"):
            return True
        if text.lower() in ("false", "f", "no", "n", "0"):
            return False
        raise config.ConfigError("Enter true or false.")
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError:
            raise config.ConfigError("Enter a whole number.")
    if isinstance(default, float):
        try:
            return float(text)
        except ValueError:
            raise config.ConfigError("Enter a number.")
    return text


def settings_menu(store):
    while True:
        print("\n-- Settings --")
        for i, (k, label) in enumerate(SETTINGS, 1):
            print(f"{i:2}. {label}: {store.data[k]!r}")
        c = input("Number to change (Enter = back): ").strip()
        if not c:
            return
        if not c.isdigit() or not 1 <= int(c) <= len(SETTINGS):
            print("Pick a number from the list.")
            continue
        key = SETTINGS[int(c) - 1][0]
        try:
            store.update(**{key: _coerce(key, input(f"New value for {key}: "))})
            logger.setup(HOME, store.data["log_level"])
            print("Saved.")
        except config.ConfigError as exc:
            print(f"Rejected: {exc}")


def pick_saved(store):
    saved = store.data["saved_experiences"]
    if not saved:
        print("No saved experiences yet. Add one with option 1 or 2.")
        return
    for i, e in enumerate(saved, 1):
        mark = "*" if e["id"] == store.data["experience_id"] else " "
        print(f"{mark}{i}. {e['name']} ({e['id']})")
    c = input("Select number (Enter = cancel): ").strip()
    if c.isdigit() and 1 <= int(c) <= len(saved):
        store.update(experience_id=saved[int(c) - 1]["id"])
        print(f"Selected {store.data['experience_id']}.")
    elif c:
        print("Not a valid choice.")


def add_experience(store, text):
    try:
        exp = config.parse_experience(text)
    except config.ConfigError as exc:
        print(f"Invalid: {exc}")
        return
    name = input("Name for this experience (Enter = use ID): ").strip()
    store.save_experience(exp, name)
    print(f"Selected experience {exp}.")


def menu(store):
    while True:
        cur = store.data["experience_id"] or "(none)"
        print(f"\n=== Roblox Auto-Rejoin | UGPhone ===\nSelected experience: {cur}")
        print("1. Enter experience URL\n2. Enter experience ID\n3. Select saved experience\n"
              "4. View selected experience\n5. Settings\n6. Start monitoring\n7. Run diagnostics\n0. Quit")
        try:
            c = input("> ").strip()
        except EOFError:
            return
        if c == "1":
            add_experience(store, input("URL: "))
        elif c == "2":
            add_experience(store, input("ID: "))
        elif c == "3":
            pick_saved(store)
        elif c == "4":
            print(f"Experience ID: {cur}\nLink: https://www.roblox.com/games/{store.data['experience_id']}" if store.data["experience_id"] else "None selected.")
        elif c == "5":
            settings_menu(store)
        elif c == "6":
            run_until_interrupt(store)
        elif c == "7":
            do_diagnose(store, launch_test=input("Also start Roblox as a launch test? [y/N] ").strip().lower() == "y")
        elif c == "0":
            return
        else:
            print("Choose 0-7.")


def do_diagnose(store, launch_test=False):
    shell = ugphone.Shell(store.data["shell_prefix"])
    rows, summary = diagnostics.run(shell, store.data, launch_test=launch_test)
    diagnostics.render(rows, summary)
    return 0 if not any(s == diagnostics.FAIL for s, _, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Roblox auto-rejoin for UGPhone/Termux")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("menu", help="interactive menu (default)")
    sub.add_parser("run", help="start monitoring with saved settings (unattended)")
    d = sub.add_parser("diagnose", help="check environment capabilities")
    d.add_argument("--launch-test", action="store_true", help="also start Roblox once to verify launching")
    g = sub.add_parser("set-game", help="select experience by URL or ID")
    g.add_argument("target")
    args = ap.parse_args(argv)

    store = config.Store(CONFIG_PATH)
    try:
        store.load()
    except config.ConfigError as exc:
        print(f"[WARN] {exc}")
    logger.setup(HOME, store.data["log_level"])

    if args.cmd == "run":
        return run_until_interrupt(store)
    if args.cmd == "diagnose":
        return do_diagnose(store, args.launch_test)
    if args.cmd == "set-game":
        try:
            store.save_experience(config.parse_experience(args.target))
        except config.ConfigError as exc:
            print(f"Invalid: {exc}")
            return 1
        print(f"Selected experience {store.data['experience_id']}.")
        return 0
    try:
        menu(store)
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
