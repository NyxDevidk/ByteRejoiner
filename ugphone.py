"""Android / UGPhone integration: command execution and environment facts.

Nothing here is UGPhone-specific API. UGPhone is treated as a normal Android image, and every
capability is probed at runtime instead of assumed.
"""
import os
import shlex
import shutil
import subprocess
from collections import namedtuple

Result = namedtuple("Result", "rc out err missing timed_out")

ROBLOX_PACKAGE_DEFAULT = "com.roblox.client"


class Shell:
    """Runs commands directly, or through a user-chosen prefix such as 'su -c' or 'adb shell'."""

    def __init__(self, prefix=""):
        self.prefix = shlex.split(prefix) if prefix else []

    def run(self, args, timeout=10):
        cmd = self.prefix + [shlex.join(args)] if self.prefix else list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, errors="replace")
        except FileNotFoundError:
            return Result(127, "", f"{cmd[0]}: not found", True, False)
        except PermissionError as exc:
            return Result(126, "", str(exc), False, False)
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout", False, True)
        except OSError as exc:
            return Result(125, "", str(exc), False, False)
        return Result(p.returncode, p.stdout.strip(), p.stderr.strip(), False, False)

    def has(self, name):
        """Is a command usable through this shell? Direct mode uses PATH; prefixed mode asks the remote shell."""
        if not self.prefix:
            return shutil.which(name) is not None
        r = self.run(["command", "-v", name])
        if r.missing or r.rc != 0:
            r = self.run(["which", name])
        return r.rc == 0 and bool(r.out)


def is_android():
    return os.path.exists("/system/build.prop") or "ANDROID_ROOT" in os.environ


def in_termux():
    return "com.termux" in os.environ.get("PREFIX", "") or "TERMUX_VERSION" in os.environ


def getprop(shell, key):
    r = shell.run(["getprop", key])
    return r.out if r.rc == 0 else ""


def device_info(shell):
    return {
        "android": getprop(shell, "ro.build.version.release"),
        "sdk": getprop(shell, "ro.build.version.sdk"),
        "model": getprop(shell, "ro.product.model"),
        "manufacturer": getprop(shell, "ro.product.manufacturer"),
        "abi": getprop(shell, "ro.product.cpu.abi"),
    }


def find_roblox_package(shell, preferred=""):
    """Return the Roblox package installed on the device, or '' when none is visible.

    Uses `pm list packages`; Android 11+ package-visibility rules may hide other apps from
    Termux, in which case this returns '' and the user must set roblox_package manually.
    """
    r = shell.run(["pm", "list", "packages"], timeout=20)
    if r.rc != 0:
        return ""
    pkgs = [ln.split(":", 1)[1].strip() for ln in r.out.splitlines() if ln.startswith("package:")]
    if preferred and preferred in pkgs:
        return preferred
    if ROBLOX_PACKAGE_DEFAULT in pkgs:
        return ROBLOX_PACKAGE_DEFAULT
    cands = sorted(p for p in pkgs if "roblox" in p.lower())
    return cands[0] if cands else ""


def network_ok(host="www.roblox.com", port=443, timeout=4.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wake_lock(shell):
    """Ask Termux to hold a CPU wake lock. Returns True when the command ran successfully."""
    if not shell.has("termux-wake-lock"):
        return False
    return shell.run(["termux-wake-lock"]).rc == 0
