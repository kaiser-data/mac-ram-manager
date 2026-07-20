"""Read-only collectors for macOS memory statistics.

Sources: vm_stat (page counters), sysctl (totals, swap, pressure level),
and ps (per-process RSS). All functions return plain dicts/lists so the
server layer can serialize them directly.
"""
from __future__ import annotations

import os
import re
import subprocess

PAGE_SIZE_FALLBACK = 16384
PS_RSS_UNIT = 1024  # ps reports RSS in KiB
SUBPROCESS_TIMEOUT_SECONDS = 15

PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}

_APP_BUNDLE_RE = re.compile(r"/([^/]+)\.app/")
_HELPER_SUFFIX_RE = re.compile(r"\s+(Helper|Web Content|Networking|GPU|Renderer).*$")


class CollectorError(RuntimeError):
    """A system command failed or produced unparseable output."""


def _run(argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError(f"{argv[0]} failed to run: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise CollectorError(f"{argv[0]} failed: {detail}")
    return result.stdout


def _sysctl(name: str) -> str:
    return _run(["/usr/sbin/sysctl", "-n", name]).strip()


def _parse_vm_stat(raw: str) -> tuple[int, dict[str, int]]:
    page_size = PAGE_SIZE_FALLBACK
    size_match = re.search(r"page size of (\d+) bytes", raw)
    if size_match:
        page_size = int(size_match.group(1))

    counters: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            counters[key.strip()] = int(digits)
    if not counters:
        raise CollectorError("vm_stat output had no counters")
    return page_size, counters


def get_memory() -> dict:
    """Overall memory picture, mirroring Activity Monitor's model."""
    page_size, pages = _parse_vm_stat(_run(["/usr/bin/vm_stat"]))
    total_bytes = int(_sysctl("hw.memsize"))

    def as_bytes(key: str) -> int:
        return pages.get(key, 0) * page_size

    anonymous = as_bytes("Anonymous pages")
    purgeable = as_bytes("Pages purgeable")
    wired = as_bytes("Pages wired down")
    compressed = as_bytes("Pages occupied by compressor")
    file_backed = as_bytes("File-backed pages")

    app_memory = max(anonymous - purgeable, 0)
    used = app_memory + wired + compressed

    return {
        "totalBytes": total_bytes,
        "usedBytes": min(used, total_bytes),
        "appBytes": app_memory,
        "wiredBytes": wired,
        "compressedBytes": compressed,
        "cachedBytes": file_backed + purgeable,
        "freeBytes": as_bytes("Pages free") + as_bytes("Pages speculative"),
        "swapIns": pages.get("Swapins", 0),
        "swapOuts": pages.get("Swapouts", 0),
    }


def get_swap() -> dict:
    raw = _sysctl("vm.swapusage")
    match = re.search(
        r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M", raw
    )
    if not match:
        raise CollectorError(f"could not parse vm.swapusage: {raw!r}")

    def to_bytes(mib: str) -> int:
        return int(float(mib) * 1024 * 1024)

    return {
        "totalBytes": to_bytes(match.group(1)),
        "usedBytes": to_bytes(match.group(2)),
        "freeBytes": to_bytes(match.group(3)),
    }


def get_pressure() -> dict:
    try:
        level = int(_sysctl("kern.memorystatus_vm_pressure_level"))
    except (CollectorError, ValueError):
        level = 1
    return {"level": level, "label": PRESSURE_LEVELS.get(level, "normal")}


def _group_name(command_path: str) -> str:
    bundle = _APP_BUNDLE_RE.search(command_path)
    name = bundle.group(1) if bundle else os.path.basename(command_path)
    name = _HELPER_SUFFIX_RE.sub("", name).strip()
    return name or command_path


def list_processes() -> list[dict]:
    """Every process visible to ps, one entry per process."""
    raw = _run(["/bin/ps", "-axo", "pid=,rss=,uid=,comm="])
    processes = []
    for line in raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_text, rss_text, uid_text, command = parts
        if not (pid_text.isdigit() and rss_text.isdigit() and uid_text.isdigit()):
            continue
        processes.append(
            {
                "pid": int(pid_text),
                "rssBytes": int(rss_text) * PS_RSS_UNIT,
                "uid": int(uid_text),
                "command": command,
                "group": _group_name(command),
            }
        )
    return processes


def top_groups(limit: int = 15) -> list[dict]:
    """Processes aggregated by app/group, sorted by total RSS."""
    own_uid = os.getuid()
    groups: dict[str, dict] = {}
    for proc in list_processes():
        entry = groups.setdefault(
            proc["group"],
            {"name": proc["group"], "rssBytes": 0, "processCount": 0,
             "killable": False, "pids": []},
        )
        entry["rssBytes"] += proc["rssBytes"]
        entry["processCount"] += 1
        if proc["uid"] == own_uid:
            entry["killable"] = True
            entry["pids"].append(proc["pid"])
    ranked = sorted(groups.values(), key=lambda g: g["rssBytes"], reverse=True)
    return ranked[:limit]


def get_stats(top_limit: int = 15) -> dict:
    return {
        "memory": get_memory(),
        "swap": get_swap(),
        "pressure": get_pressure(),
        "topGroups": top_groups(top_limit),
    }
