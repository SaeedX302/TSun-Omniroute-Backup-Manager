#!/usr/bin/env python3
"""
OmniRoute Backup Manager — Web Server & REST API (`app.py`)
Serves the Frontend Dashboard and exposes REST endpoints to:
  - Inspect `/home/saeed/.omniroute`, disk usage, cron status, and SQLite run history
  - Trigger an immediate backup (`omniroute-dd-mm-yy.tar.gz` -> `gdrive:omnirote` -> local delete)
  - Browse and manage remote files inside Google Drive `/omnirote` via `rclone`
  - Configure paths, remote name, folder (`omnirote`), and schedule (`00:01` / 12:01 AM)
  - Install/sync the Linux crontab (`1 0 * * *`) + optional in-process 12:01 AM scheduler thread
"""

import argparse
import datetime
import http.server
import json
import logging
import os
import pathlib
import threading
import time
import urllib.parse
from http import HTTPStatus
from typing import Any, Dict

import backup_engine

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATE_INDEX = PROJECT_ROOT / "templates" / "index.html"

# Whitelisted project files exposed read-only through GET /api/source?name=<key>
SOURCE_FILES: Dict[str, pathlib.Path] = {
    "backup_engine.py": PROJECT_ROOT / "backup_engine.py",
    "app.py": PROJECT_ROOT / "app.py",
    "config.json": PROJECT_ROOT / "config.json",
    "install.sh": PROJECT_ROOT / "install.sh",
    "README.md": PROJECT_ROOT / "README.md",
    "systemd/omniroute-dashboard.service": PROJECT_ROOT / "systemd" / "omniroute-dashboard.service",
}

logger = logging.getLogger("omniroute_web")

_backup_lock = threading.Lock()


class DailySchedulerThread(threading.Thread):
    """
    Lightweight fallback scheduler that checks once every 30 seconds whether the clock
    has reached the configured daily backup minute (default 00:01 / 12:01 AM) and runs
    the backup if a cron job has not already executed it today.
    """

    def __init__(self, enabled: bool = False) -> None:
        super().__init__(daemon=True)
        self.enabled = enabled
        self._last_run_date: str = ""

    def run(self) -> None:
        if not self.enabled:
            return
        logger.info("In-process daily backup scheduler started.")
        while True:
            try:
                cfg = backup_engine.load_config()
                now = datetime.datetime.now()
                target_h = int(cfg.get("cron_hour", 0))
                target_m = int(cfg.get("cron_minute", 1))
                today_key = now.strftime("%Y-%m-%d")

                if now.hour == target_h and now.minute == target_m and self._last_run_date != today_key:
                    # Check if a backup already ran today (e.g., via system cron)
                    history = backup_engine.list_history(limit=1)
                    already_ran_today = False
                    if history:
                        last_started = str(history[0].get("started_at", ""))
                        if last_started.startswith(today_key) and history[0].get("status") in ("running", "success"):
                            already_ran_today = True
                    if not already_ran_today and _backup_lock.acquire(blocking=False):
                        try:
                            self._last_run_date = today_key
                            backup_engine.execute_backup(trigger_type="scheduler-00:01")
                        finally:
                            _backup_lock.release()
            except Exception as exc:
                logger.error("Scheduler check error: %s", exc)
            time.sleep(25)


class OmniRouteRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/status":
            payload = backup_engine.get_system_overview()
            payload["server_pid"] = os.getpid()
            payload["server_port"] = self.server.server_address[1]
            payload["project_root"] = str(PROJECT_ROOT)
            self._send_json({"ok": True, "data": payload})
            return

        if route == "/api/history":
            self._send_json({"ok": True, "history": backup_engine.list_history(limit=50)})
            return

        if route == "/api/remote/files":
            self._send_json(backup_engine.list_remote_backups())
            return

        if route == "/api/config":
            self._send_json({"ok": True, "config": backup_engine.load_config()})
            return

        if route == "/api/logs":
            log_text = ""
            if backup_engine.LOG_FILE.exists():
                lines = backup_engine.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                log_text = "\n".join(lines[-200:])
            self._send_json({"ok": True, "log": log_text})
            return

        if route == "/api/source":
            query = urllib.parse.parse_qs(parsed.query)
            name = (query.get("name", [""])[0] or "").strip()
            target = SOURCE_FILES.get(name)
            if target is None or not target.is_file():
                self._send_json(
                    {"ok": False, "error": f"Unknown source file: {name}", "files": list(SOURCE_FILES)},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json({"ok": True, "name": name, "content": content, "files": list(SOURCE_FILES)})
            return

        if route in ("/", "/index.html"):
            if TEMPLATE_INDEX.is_file():
                body = TEMPLATE_INDEX.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            logger.warning("Dashboard template missing: %s", TEMPLATE_INDEX)
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Invalid JSON body: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        if route == "/api/backup/run":
            if not _backup_lock.acquire(blocking=False):
                self._send_json(
                    {"ok": False, "error": "A backup job is already running."},
                    status=HTTPStatus.CONFLICT,
                )
                return
            try:
                result = backup_engine.execute_backup(trigger_type=body.get("trigger", "manual-ui"))
                ok = result.get("status") == "success"
                self._send_json({"ok": ok, "run": result})
            finally:
                _backup_lock.release()
            return

        if route == "/api/config":
            updated = backup_engine.save_config(body)
            self._send_json({"ok": True, "config": updated, "cron_preview": backup_engine.get_cron_line(updated)})
            return

        if route == "/api/cron/sync":
            res = backup_engine.install_cron_job()
            self._send_json(res)
            return

        if route == "/api/rclone/test":
            res = backup_engine.test_rclone_connection()
            self._send_json(res)
            return

        if route == "/api/remote/delete":
            fname = str(body.get("filename", "")).strip()
            res = backup_engine.delete_remote_backup(fname)
            self._send_json(res)
            return

        self._send_json({"ok": False, "error": f"Unknown endpoint: {route}"}, status=HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniRoute Backup Manager Web Dashboard")
    cfg = backup_engine.load_config()
    parser.add_argument("--host", default=cfg.get("dashboard_host", "0.0.0.0"), help="Bind host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(cfg.get("dashboard_port", 8080)), help="Bind port (default 8080)")
    parser.add_argument(
        "--with-scheduler",
        action="store_true",
        help="Also run an in-process 12:01 AM daily scheduler thread alongside system cron.",
    )
    args = parser.parse_args()

    backup_engine.init_db()
    scheduler = DailySchedulerThread(enabled=args.with_scheduler)
    scheduler.start()

    server = http.server.ThreadingHTTPServer((args.host, args.port), OmniRouteRequestHandler)
    logger.info("OmniRoute Backup Dashboard listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down web server...")
        server.server_close()


if __name__ == "__main__":
    main()
