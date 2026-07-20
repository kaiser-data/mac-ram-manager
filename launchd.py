"""List and control login/startup items (launchd user agents).

Covers plists in ~/Library/LaunchAgents and /Library/LaunchAgents — the
things that autostart at login. Control is restricted to the user's gui
domain and never touches com.apple.* labels.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

SUBPROCESS_TIMEOUT_SECONDS = 30
AGENT_DIRS = (
    (Path.home() / "Library" / "LaunchAgents", "user"),
    (Path("/Library/LaunchAgents"), "system"),
)
AGENT_ACTIONS = ("stop", "start", "disable", "enable")


class LaunchdError(RuntimeError):
    """A launchd operation was rejected or failed."""


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchdError(f"{' '.join(argv[:2])} failed: {exc}") from exc


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _running_pids() -> dict[str, int]:
    """label → pid for agents currently running in the user session."""
    result = _run(["/bin/launchctl", "list"])
    pids: dict[str, int] = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            pids[parts[2]] = int(parts[0])
    return pids


def _disabled_labels() -> set[str]:
    result = _run(["/bin/launchctl", "print-disabled", _gui_domain()])
    return set(re.findall(r'"([^"]+)"\s*=>\s*(?:true|disabled)', result.stdout))


def _plist_label(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            label = plistlib.load(handle).get("Label")
        if isinstance(label, str) and label:
            return label
    except (OSError, plistlib.InvalidFileException):
        pass
    return path.stem


def _agent_paths() -> dict[str, dict]:
    agents: dict[str, dict] = {}
    for directory, source in AGENT_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.plist")):
            label = _plist_label(path)
            agents.setdefault(label, {"label": label, "path": str(path),
                                      "source": source})
    return agents


def list_agents() -> list[dict]:
    """Startup items with live state: running pid, disabled flag, source."""
    running = _running_pids()
    disabled = _disabled_labels()
    agents = []
    for label, info in _agent_paths().items():
        agents.append({
            **info,
            "pid": running.get(label),
            "running": label in running,
            "disabled": label in disabled,
            "protected": label.startswith("com.apple."),
        })
    agents.sort(key=lambda a: (not a["running"], a["label"].lower()))
    return agents


def control_agent(label: str, action: str) -> dict:
    """stop/start a running agent; disable/enable its autostart at login."""
    if action not in AGENT_ACTIONS:
        raise LaunchdError(f"action must be one of {AGENT_ACTIONS}")
    if label.startswith("com.apple."):
        raise LaunchdError("Apple system agents cannot be managed here")

    known = _agent_paths()
    if label not in known:
        raise LaunchdError(f"unknown launch agent '{label}'")

    domain = _gui_domain()
    target = f"{domain}/{label}"
    commands = {
        "stop": ["/bin/launchctl", "bootout", target],
        "start": ["/bin/launchctl", "bootstrap", domain, known[label]["path"]],
        "disable": ["/bin/launchctl", "disable", target],
        "enable": ["/bin/launchctl", "enable", target],
    }
    result = _run(commands[action])
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise LaunchdError(f"launchctl {action} failed: {detail}")
    return {"label": label, "action": action}
