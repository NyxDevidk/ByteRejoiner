"""Capability report. Every line is measured on the device it runs on, never assumed."""
import platform
import re
import sys
import time

import ugphone
from monitor import Monitor, RUNNING_STATES

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"


def run(shell, cfg, launch_test=False):
    """Return (rows, summary). rows = [(status, label, detail)]."""
    rows = []
    add = lambda s, label, detail="": rows.append((s, label, detail))

    add(INFO, "Python", sys.version.split()[0])
    if not ugphone.is_android():
        add(WARN, "Platform", f"{platform.system()} - not Android. Android checks below will fail; run this inside Termux on UGPhone.")
    else:
        add(OK, "Platform", "Android")
    add(OK if ugphone.in_termux() else WARN, "Termux environment", "detected" if ugphone.in_termux() else "PREFIX does not look like Termux")

    info = ugphone.device_info(shell)
    if info["android"]:
        add(INFO, "Device", f"Android {info['android']} (SDK {info['sdk']}), {info['manufacturer']} {info['model']}, {info['abi']}")
    else:
        add(WARN, "Device", "getprop returned nothing")

    for cmd, need in (("am", True), ("pm", True), ("cmd", False), ("termux-open-url", False),
                      ("pidof", False), ("ps", False), ("dumpsys", False), ("termux-wake-lock", False)):
        found = shell.has(cmd)
        add(OK if found else (FAIL if need else WARN), f"command: {cmd}", "available" if found else "missing" + (" (required)" if need else ""))
    if cfg["shell_prefix"]:
        add(INFO, "shell_prefix", cfg["shell_prefix"])
    else:
        su = shell.has("su")
        add(INFO, "root (su)", "binary present (not used unless you set shell_prefix)" if su else "not present (fine; no root needed)")

    pkg = ugphone.find_roblox_package(shell, cfg["roblox_package"])
    if pkg:
        add(OK, "Roblox package", pkg)
    else:
        pm = shell.run(["pm", "list", "packages"], timeout=20)
        hint = ("pm could not list packages" if pm.rc != 0
                else "pm works but no roblox package is visible: not installed, or Android package-visibility hides it. Set roblox_package manually")
        add(FAIL, "Roblox package", hint)
        pkg = cfg["roblox_package"] or ugphone.ROBLOX_PACKAGE_DEFAULT

    mon = Monitor(shell, pkg)
    can_stop = mon.calibrate()
    for name, (ok, note) in mon.calibration.items():
        add(OK if ok else WARN, f"probe: {name}", note)
    status = mon.observe()
    add(INFO, "Roblox right now", status)
    pid = mon.pid()
    if pid:
        lc = shell.run(["logcat", "-d", "-t", "20", "--pid", pid], timeout=10)
        ok_lc = lc.rc == 0 and bool(lc.out)
        add(OK if ok_lc else WARN, "logcat of Roblox (disconnect detection)",
            "readable" if ok_lc else "not readable: disconnects while Roblox stays open won't be detected")
    else:
        add(INFO, "logcat of Roblox (disconnect detection)", "open Roblox and rerun to check")
    fs = shell.has("am") and shell.prefix != []
    add(OK if fs else WARN, "force-stop before relaunch",
        "privileged shell set (shell_prefix)" if fs else "no shell_prefix: force-stop will likely be denied")
    net = ugphone.network_ok()
    add(OK if net else WARN, "network (www.roblox.com:443)", "reachable" if net else "unreachable")
    add(INFO, "wake lock", "termux-wake-lock available" if shell.has("termux-wake-lock") else "unavailable (install termux-api app + pkg)")

    dd = shell.run(["dumpsys", "deviceidle", "whitelist"])
    if dd.rc == 0 and dd.out and "Permission Denial" not in dd.out + dd.err:
        listed = "com.termux" in dd.out
        add(OK if listed else WARN, "battery optimization", "Termux is exempt" if listed else "Termux is NOT exempt from Doze; disable battery optimization for Termux")
    else:
        add(INFO, "battery optimization", "cannot be read from this shell; check manually (README)")

    if launch_test:
        rows.extend(_launch_test(shell, pkg, mon))

    summary = _summary(rows, can_stop)
    return rows, summary


def _launch_test(shell, pkg, mon):
    out = []
    r = shell.run(["cmd", "package", "resolve-activity", "--brief", pkg], timeout=15)
    comp = ""
    if r.rc == 0:
        lines = [ln.strip() for ln in r.out.splitlines() if "/" in ln]
        comp = lines[-1] if lines else ""
    if comp:
        res = shell.run(["am", "start", "-n", comp], timeout=20)
    else:
        res = shell.run(["monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], timeout=20)
    sent = res.rc == 0 and not re.search(r"Error|Exception|Permission Denial", res.out + res.err)
    out.append((OK if sent else FAIL, "launch test: start Roblox", "accepted by Android" if sent else (res.err or res.out or f"rc={res.rc}")[:200]))
    if sent:
        end = time.monotonic() + 30
        st = mon.observe()
        while st not in RUNNING_STATES and time.monotonic() < end:
            time.sleep(2)
            st = mon.observe()
        out.append((OK if st in RUNNING_STATES else WARN, "launch test: detected running", st))
    return out


def _summary(rows, can_stop):
    by = {label: s for s, label, _ in rows}
    lines = []
    if by.get("command: am") != OK or by.get("Roblox package") != OK:
        lines.append("CANNOT RELAUNCH reliably: need `am` and a visible Roblox package.")
    else:
        lines.append("Launch: supported (confirm with `diagnose --launch-test`).")
    if can_stop:
        lines.append("Crash detection: SUPPORTED. A closed Roblox will be detected and relaunched.")
    else:
        lines.append("Crash detection: NOT AVAILABLE from this shell. Process list is hidden or dumpsys is denied.")
        lines.append("  Fallbacks: shell_prefix via adb/su (README), or blind_relaunch_minutes (periodic relaunch).")
    return lines


def render(rows, summary):
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
    for s, label, detail in rows:
        print(f"{mark[s]} {label}: {detail}")
    print()
    for ln in summary:
        print(ln)
