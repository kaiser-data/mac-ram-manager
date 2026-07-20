"""RAM Manager — local dashboard server.

Serves the dashboard on http://127.0.0.1:8765 (localhost only), samples
memory stats in the background, and exposes action endpoints protected by
a per-run CSRF token.

Run:  python3 server.py [--port 8765]
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import actions
import launchd
import memstats

DEFAULT_PORT = 8765
BIND_HOST = "127.0.0.1"
SAMPLE_INTERVAL_SECONDS = 10
HISTORY_MAX_SAMPLES = 360  # one hour at 10s
MAX_BODY_BYTES = 4096

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_TYPES = {".css": "text/css", ".js": "application/javascript",
                ".html": "text/html", ".svg": "image/svg+xml"}

CSRF_TOKEN = secrets.token_hex(16)

history: deque[dict] = deque(maxlen=HISTORY_MAX_SAMPLES)
history_lock = threading.Lock()


def sample_once() -> None:
    memory = memstats.get_memory()
    swap = memstats.get_swap()
    pressure = memstats.get_pressure()
    with history_lock:
        history.append({
            "t": int(time.time()),
            "usedBytes": memory["usedBytes"],
            "swapUsedBytes": swap["usedBytes"],
            "pressureLevel": pressure["level"],
        })


def sampler_loop() -> None:
    while True:
        try:
            sample_once()
        except memstats.CollectorError:
            pass  # transient tool failure; next tick retries
        time.sleep(SAMPLE_INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    server_version = "RAMManager/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data) -> None:
        self._send_json({"ok": True, "data": data, "error": None})

    def _fail(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "data": None, "error": message}, status)

    def _read_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_BYTES)
        if length <= 0:
            return {}
        parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _check_token(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-RAM-Token", ""), CSRF_TOKEN
        )

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            if self.path in ("/", "/index.html"):
                self._serve_index()
            elif self.path.startswith("/static/"):
                self._serve_static()
            elif self.path == "/api/stats":
                self._serve_stats()
            else:
                self._fail("not found", 404)
        except Exception as exc:  # top-level guard: never crash the server
            self._fail(f"internal error: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_token():
            self._fail("bad or missing CSRF token", 403)
            return
        try:
            body = self._read_body()
            if self.path == "/api/kill-group":
                self._ok(actions.kill_group(
                    body.get("name", ""), bool(body.get("force", False))))
            elif self.path == "/api/relaunch-group":
                self._ok(actions.relaunch_group(body.get("name", "")))
            elif self.path == "/api/launchd":
                self._ok(launchd.control_agent(
                    body.get("label", ""), body.get("action", "")))
            elif self.path == "/api/service":
                self._ok(actions.control_service(
                    body.get("name", ""), body.get("action", "")))
            elif self.path == "/api/purge":
                self._ok(actions.purge_disk_cache())
            else:
                self._fail("not found", 404)
        except (actions.ActionError, launchd.LaunchdError) as exc:
            self._fail(str(exc), 400)
        except json.JSONDecodeError:
            self._fail("request body must be JSON", 400)
        except Exception as exc:
            self._fail(f"internal error: {exc}", 500)

    # -- route bodies -----------------------------------------------------
    def _serve_index(self) -> None:
        html = (STATIC_DIR / "index.html").read_text()
        body = html.replace("__RAM_TOKEN__", CSRF_TOKEN).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self) -> None:
        requested = (STATIC_DIR / self.path.removeprefix("/static/")).resolve()
        if not requested.is_relative_to(STATIC_DIR) or not requested.is_file():
            self._fail("not found", 404)
            return
        body = requested.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            STATIC_TYPES.get(requested.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stats(self) -> None:
        stats = memstats.get_stats()
        try:
            services = actions.list_services()
        except actions.ActionError:
            services = []
        try:
            agents = launchd.list_agents()
        except launchd.LaunchdError:
            agents = []
        with history_lock:
            stats["history"] = list(history)
        stats["services"] = services
        stats["agents"] = agents
        self._ok(stats)

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep the terminal quiet; errors surface via JSON


def main() -> None:
    parser = argparse.ArgumentParser(description="RAM Manager dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    sample_once()
    threading.Thread(target=sampler_loop, daemon=True).start()

    server = ThreadingHTTPServer((BIND_HOST, args.port), Handler)
    print(f"RAM Manager running → http://{BIND_HOST}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
