"""Actions the dashboard can trigger: quit processes, manage brew
services, and purge the disk cache.

Every action validates its input against a fresh process/service listing
before touching anything, and only ever signals processes owned by the
current user.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time

import memstats

SUBPROCESS_TIMEOUT_SECONDS = 60
SERVICE_ACTIONS = ("start", "stop", "restart")

# System-critical groups the UI must never offer to kill, even though some
# of their processes run under the user's uid.
PROTECTED_GROUPS = {"launchd", "kernel_task", "WindowServer", "loginwindow",
                    "Finder", "Dock", "SystemUIServer", "python3", "Python"}


class ActionError(RuntimeError):
    """An action was rejected or failed; message is safe to show the user."""


def _find_brew() -> str | None:
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("brew")


def _owned_pids_for_group(group_name: str) -> list[int]:
    own_uid = os.getuid()
    own_pid = os.getpid()
    return [
        proc["pid"]
        for proc in memstats.list_processes()
        if proc["group"] == group_name
        and proc["uid"] == own_uid
        and proc["pid"] != own_pid
    ]


def kill_group(group_name: str, force: bool = False) -> dict:
    """Send SIGTERM (or SIGKILL) to every user-owned process in a group."""
    if not group_name or not isinstance(group_name, str):
        raise ActionError("missing process group name")
    if group_name in PROTECTED_GROUPS:
        raise ActionError(f"'{group_name}' is protected and cannot be quit")

    pids = _owned_pids_for_group(group_name)
    if not pids:
        raise ActionError(
            f"no processes owned by you found for '{group_name}'"
        )

    sig = signal.SIGKILL if force else signal.SIGTERM
    killed, failed = [], []
    for pid in pids:
        try:
            os.kill(pid, sig)
            killed.append(pid)
        except ProcessLookupError:
            continue  # already exited
        except PermissionError:
            failed.append(pid)
    return {"group": group_name, "signalled": killed, "failed": failed,
            "signal": sig.name}


def kill_pid(pid: int, force: bool = False) -> dict:
    """Signal one specific process — must be user-owned and not protected."""
    if not isinstance(pid, int) or pid <= 1:
        raise ActionError("invalid pid")
    own_uid = os.getuid()
    match = next(
        (p for p in memstats.list_processes()
         if p["pid"] == pid and p["uid"] == own_uid), None)
    if match is None:
        raise ActionError(f"pid {pid} not found or not owned by you")
    if match["group"] in PROTECTED_GROUPS or pid == os.getpid():
        raise ActionError(f"process '{match['group']}' is protected")
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already exited — treat as success
    except PermissionError as exc:
        raise ActionError(f"no permission to signal pid {pid}") from exc
    return {"pid": pid, "group": match["group"], "signal": sig.name}


def relaunch_group(group_name: str) -> dict:
    """Quit an app's processes, then reopen it via `open -a`."""
    result = kill_group(group_name, force=False)
    time.sleep(1.5)  # give processes a moment to exit cleanly
    try:
        reopen = subprocess.run(
            ["/usr/bin/open", "-a", group_name],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"quit succeeded but reopen failed: {exc}") from exc
    if reopen.returncode != 0:
        raise ActionError(
            f"quit '{group_name}', but it is not a reopenable app: "
            f"{reopen.stderr.strip() or 'open -a failed'}"
        )
    return {**result, "relaunched": True}


def list_services() -> list[dict]:
    """Brew services with their run state; empty list if brew is absent."""
    brew = _find_brew()
    if brew is None:
        return []
    try:
        result = subprocess.run(
            [brew, "services", "list"],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"brew services list failed: {exc}") from exc

    services = []
    for line in result.stdout.splitlines()[1:]:  # skip header row
        parts = line.split()
        if len(parts) >= 2:
            services.append({"name": parts[0], "status": parts[1]})
    return services


def control_service(name: str, action: str) -> dict:
    """Start/stop/restart a brew service. Name must exist in brew's list."""
    if action not in SERVICE_ACTIONS:
        raise ActionError(f"action must be one of {SERVICE_ACTIONS}")
    known = {service["name"] for service in list_services()}
    if name not in known:
        raise ActionError(f"unknown brew service '{name}'")

    brew = _find_brew()
    try:
        result = subprocess.run(
            [brew, "services", action, name],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"brew services {action} failed: {exc}") from exc
    if result.returncode != 0:
        raise ActionError(result.stderr.strip() or f"brew exited "
                          f"{result.returncode}")
    return {"service": name, "action": action}


def purge_disk_cache() -> dict:
    """Run `purge`. On modern macOS this usually requires sudo; surface a
    clear message instead of failing silently."""
    try:
        result = subprocess.run(
            ["/usr/sbin/purge"],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"purge failed to run: {exc}") from exc
    if result.returncode != 0:
        raise ActionError(
            "purge needs elevated rights — run `sudo purge` in a terminal"
        )
    return {"purged": True}
