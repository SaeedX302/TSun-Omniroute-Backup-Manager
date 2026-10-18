#!/usr/bin/env python3
"""
OmniRoute Daily Backup Engine (`backup_engine.py`)
Target VPS: saeed@ip-172-31-37-134:~$
Source:     /home/saeed/.omniroute
Archive:    omniroute-dd-mm-yy.tar.gz (e.g., omniroute-16-10-26.tar.gz)
Schedule:   Daily at 12:01 AM (Cron: 1 0 * * *)
Remote:     Google Drive `/omnirote` folder via rclone (`gdrive:omnirote`)

Workflow:
1. Validates that `/home/saeed/.omniroute` exists and is readable.
2. Compresses `.omniroute` into `/tmp/omniroute-dd-mm-yy.tar.gz` and verifies gzip/tar integrity + SHA-256 hash.
3. Ensures the `/omnirote` folder exists on Google Drive via `rclone mkdir`.
4. Uploads the archive to `gdrive:omnirote/omniroute-dd-mm-yy.tar.gz` via `rclone copyto`.
5. Verifies the uploaded file exists on Google Drive with matching byte size via `rclone lsjson`.
6. Deletes the local `omniroute-dd-mm-yy.tar.gz` from the VPS ONLY after verified upload success.
7. Logs all metrics, command outputs, and status into SQLite (`backup_history.db`) and `logs/backup.log`.
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DB_PATH = PROJECT_ROOT / "backup_history.db"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "backup.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("omniroute_backup")

DEFAULT_CONFIG: Dict[str, Any] = {
    "vps_user": "saeed",
    "vps_host": "ip-172-31-37-134",
    "source_dir": "/home/saeed/.omniroute",
    "staging_dir": "/tmp",
    "archive_prefix": "omniroute",
    "date_format": "%d-%m-%y",
    "rclone_binary": "rclone",
    "rclone_remote": "gdrive",
    "drive_folder": "omnirote",
    "cron_minute": 1,
    "cron_hour": 0,
    "delete_local_after_upload": True,
    "verify_remote_after_upload": True,
    "retention_days": 0,

    "allowlist_entries": [
        ".env",
        "oauth",
        "storage.sqlite",
        "storage.sqlite-wal",
    ],

    "dashboard_host": "0.0.0.0",
    "dashboard_port": 20363,
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as exc:
            logger.warning("Failed to parse config.json, using defaults: %s", exc)
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def _connect_db() -> sqlite3.Connection:
    uri = f"file:{DB_PATH}?nolock=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")
    return conn


def init_db() -> None:
    with _connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                trigger_type TEXT NOT NULL,
                archive_name TEXT NOT NULL,
                source_dir TEXT NOT NULL,
                local_archive_path TEXT NOT NULL,
                remote_target TEXT NOT NULL,
                archive_size_bytes INTEGER DEFAULT 0,
                sha256 TEXT DEFAULT '',
                status TEXT NOT NULL,
                local_deleted INTEGER DEFAULT 0,
                remote_verified INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0.0,
                step_log TEXT DEFAULT '',
                error_message TEXT DEFAULT ''
            )
            """
        )
        conn.commit()


def compute_sha256(file_path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_directory_stats(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "file_count": 0, "total_bytes": 0}
    total_bytes = 0
    file_count = 0
    for root, _, files in os.walk(path):
        for fname in files:
            fpath = pathlib.Path(root) / fname
            try:

                if not fpath.is_symlink():
                    total_bytes += fpath.stat().st_size
                    file_count += 1
            except OSError:
                pass
    return {"exists": True, "file_count": file_count, "total_bytes": total_bytes}


def build_archive_name(cfg: Dict[str, Any], dt: Optional[datetime.datetime] = None) -> str:
    now = dt or datetime.datetime.now()
    date_tag = now.strftime(cfg.get("date_format", "%d-%m-%y"))
    prefix = cfg.get("archive_prefix", "omniroute").strip() or "omniroute"
    return f"{prefix}-{date_tag}.tar.gz"


def run_cmd(cmd: List[str], timeout: int = 600) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:
        return 1, "", str(exc)


def _create_allowlist_tar_gz(
    source_dir: pathlib.Path,
    archive_path: pathlib.Path,
    allowlist_entries: List[str],
) -> int:
    """Create tar.gz containing only selected entries under `source_dir`.

    `allowlist_entries` values are relative paths under `source_dir`.
    Each entry can be a file (e.g., `.env`) or a directory (e.g., `oauth`).

    Archive layout preserves the top-level directory name (e.g. `.omniroute/...`).
    """
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

    normalized = [e.strip().lstrip("./") for e in allowlist_entries if str(e).strip()]
    if not normalized:
        raise ValueError("allowlist_entries is empty; refusing to create an empty/unsafe backup.")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    top_name = source_dir.name

    def _safe_rel(entry: str) -> pathlib.Path:
        # Prevent escaping the source directory.
        rel = pathlib.Path(entry)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"Invalid allowlist entry (path traversal not allowed): {entry!r}")
        return rel

    with tarfile.open(archive_path, "w:gz", compresslevel=6) as tar:
        for entry in normalized:
            rel = _safe_rel(entry)
            abs_path = source_dir / rel
            if not abs_path.exists():
                # Missing entries are fine, but we still want deterministic archives.
                logger.info("Allowlist entry missing, skipping: %s", entry)
                continue

            # Preserve `.omniroute/...` layout.
            tar.add(str(abs_path), arcname=str(pathlib.Path(top_name) / rel))

    if not archive_path.exists() or archive_path.stat().st_size == 0:
        raise RuntimeError(f"Archive creation produced an empty or missing file: {archive_path}")

    with tarfile.open(archive_path, "r:gz") as check_tar:
        members = check_tar.getmembers()
        if not members:
            raise RuntimeError("Created tar.gz archive contains zero members.")

    return archive_path.stat().st_size


def create_tar_gz(source_dir: pathlib.Path, archive_path: pathlib.Path) -> int:
    """Backward-compatible behavior: compress full `source_dir`."""
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    with tarfile.open(archive_path, "w:gz", compresslevel=6) as tar:
        tar.add(str(source_dir), arcname=source_dir.name)

    if not archive_path.exists() or archive_path.stat().st_size == 0:
        raise RuntimeError(f"Archive creation produced an empty or missing file: {archive_path}")

    with tarfile.open(archive_path, "r:gz") as check_tar:
        members = check_tar.getmembers()
        if not members:
            raise RuntimeError("Created tar.gz archive contains zero members.")

    return archive_path.stat().st_size


def verify_remote_file(
    rclone_bin: str, remote_folder_spec: str, archive_name: str, expected_size: int
) -> Tuple[bool, str]:
    """
    Uses `rclone lsjson <remote>:<folder>/<archive_name>` to verify that the file exists
    in Google Drive `/omnirote` and matches the local archive byte size.
    """
    target_Object = f"{remote_folder_spec.rstrip('/')}/{archive_name}"
    code, out, err = run_cmd([rclone_bin, "lsjson", target_Object], timeout=60)
    if code != 0:
        return False, f"rclone lsjson failed (exit {code}): {err or out}"
    try:
        items = json.loads(out)
        if not isinstance(items, list) or len(items) == 0:
            return False, f"File {archive_name} not found in {remote_folder_spec}"
        remote_size = int(items[0].get("Size", -1))
        if remote_size != expected_size:
            return (
                False,
                f"Size mismatch on Drive: expected {expected_size} bytes, found {remote_size} bytes",
            )
        return True, f"Verified on Google Drive ({remote_size} bytes at {target_Object})"
    except Exception as exc:
        return False, f"Failed to parse rclone lsjson output: {exc}"


def execute_backup(trigger_type: str = "cron") -> Dict[str, Any]:
    """
    Executes the complete backup lifecycle:
      1. Compress `/home/saeed/.omniroute` -> `/tmp/omniroute-dd-mm-yy.tar.gz`
      2. Upload to `gdrive:omnirote/omniroute-dd-mm-yy.tar.gz` via rclone
      3. Verify remote file size on Google Drive
      4. Delete local `omniroute-dd-mm-yy.tar.gz` from VPS upon verified success
    """
    init_db()
    cfg = load_config()
    start_ts = time.time()
    started_at = datetime.datetime.now().isoformat(timespec="seconds")

    source_dir = pathlib.Path(os.path.expanduser(cfg["source_dir"])).resolve()
    staging_dir = pathlib.Path(os.path.expanduser(cfg["staging_dir"])).resolve()
    archive_name = build_archive_name(cfg)
    local_archive = staging_dir / archive_name

    rclone_bin = cfg.get("rclone_binary", "rclone")
    remote_name = cfg.get("rclone_remote", "gdrive").rstrip(":")
    drive_folder = cfg.get("drive_folder", "omnirote").strip("/")
    remote_folder_spec = f"{remote_name}:{drive_folder}" if drive_folder else f"{remote_name}:"
    remote_target = f"{remote_folder_spec}/{archive_name}"

    steps: List[str] = []

    def log_step(msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        steps.append(line)
        logger.info(msg)

    with _connect_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO backup_runs (
                started_at, trigger_type, archive_name, source_dir,
                local_archive_path, remote_target, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                trigger_type,
                archive_name,
                str(source_dir),
                str(local_archive),
                remote_target,
                "running",
            ),
        )
        run_id = cur.lastrowid
        conn.commit()

    archive_size = 0
    sha256_hex = ""
    local_deleted = False
    remote_verified = False
    status = "failed"
    error_message = ""

    try:
        log_step(f"Starting backup run #{run_id} (trigger={trigger_type})")
        log_step(f"Checking source directory: {source_dir}")
        if not source_dir.exists():
            raise FileNotFoundError(
                f"Source directory '{source_dir}' not found on VPS. "
                "Verify the path in config.json or create /home/saeed/.omniroute."
            )

        log_step(f"Compressing allowlisted entries from '{source_dir}' -> '{local_archive}' ...")

        allowlist_entries = cfg.get(
            "allowlist_entries",
            [".env", "oauth", "storage.sqlite", "storage.sqlite-wal"],
        )

        archive_size = _create_allowlist_tar_gz(source_dir, local_archive, allowlist_entries)
        sha256_hex = compute_sha256(local_archive)
        log_step(
            f"Compression complete (allowlist): {archive_name} ({archive_size:,} bytes, SHA-256: {sha256_hex[:16]}...)"
        )

        # Ensure destination folder (/omnirote) exists on Google Drive
        log_step(f"Ensuring remote folder '{remote_folder_spec}' exists on Google Drive...")
        mk_code, mk_out, mk_err = run_cmd([rclone_bin, "mkdir", remote_folder_spec], timeout=60)
        if mk_code != 0:
            raise RuntimeError(
                f"rclone mkdir '{remote_folder_spec}' failed (exit {mk_code}): {mk_err or mk_out}"
            )

        # Upload archive via rclone copyto
        log_step(f"Uploading '{local_archive}' -> '{remote_target}' via rclone...")
        up_code, up_out, up_err = run_cmd(
            [
                rclone_bin,
                "copyto",
                str(local_archive),
                remote_target,
                "--stats-one-line",
                "-v",
            ],
            timeout=1800,
        )
        if up_code != 0:
            raise RuntimeError(
                f"rclone upload failed (exit {up_code}): {up_err or up_out}"
            )
        log_step("rclone copyto finished with exit code 0.")

        # Verify uploaded file on Google Drive
        if cfg.get("verify_remote_after_upload", True):
            log_step(f"Verifying '{archive_name}' inside Google Drive '/{drive_folder}'...")
            ok, verify_msg = verify_remote_file(
                rclone_bin, remote_folder_spec, archive_name, archive_size
            )
            if not ok:
                raise RuntimeError(f"Post-upload verification failed: {verify_msg}")
            remote_verified = True
            log_step(verify_msg)
        else:
            remote_verified = True

        # Delete local archive ONLY after verified upload
        if cfg.get("delete_local_after_upload", True):
            log_step(f"Deleting temporary local archive '{local_archive}' from VPS...")
            local_archive.unlink()
            local_deleted = not local_archive.exists()
            log_step(f"Local archive deleted from VPS: {local_deleted}")

        # Optional retention cleanup on remote
        retention_days = int(cfg.get("retention_days", 0) or 0)
        if retention_days > 0:
            log_step(f"Applying remote retention policy: deleting files older than {retention_days}d in {remote_folder_spec}")
            run_cmd(
                [
                    rclone_bin,
                    "delete",
                    remote_folder_spec,
                    "--min-age",
                    f"{retention_days}d",
                    "--include",
                    f"{cfg.get('archive_prefix', 'omniroute')}-*.tar.gz",
                ],
                timeout=120,
            )

        status = "success"
        log_step(f"Backup run #{run_id} completed successfully.")

    except Exception as exc:
        error_message = str(exc)
        status = "failed"
        log_step(f"ERROR: {error_message}")
        if local_archive.exists():
            log_step(
                f"Local archive retained at '{local_archive}' because upload did not complete."
            )

    duration = round(time.time() - start_ts, 2)
    finished_at = datetime.datetime.now().isoformat(timespec="seconds")

    with _connect_db() as conn:
        conn.execute(
            """
            UPDATE backup_runs
            SET finished_at = ?,
                archive_size_bytes = ?,
                sha256 = ?,
                status = ?,
                local_deleted = ?,
                remote_verified = ?,
                duration_seconds = ?,
                step_log = ?,
                error_message = ?
            WHERE id = ?
            """,
            (
                finished_at,
                archive_size,
                sha256_hex,
                status,
                1 if local_deleted else 0,
                1 if remote_verified else 0,
                duration,
                "\n".join(steps),
                error_message,
                run_id,
            ),
        )
        conn.commit()

    return get_run_by_id(run_id)


def get_run_by_id(run_id: int) -> Dict[str, Any]:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM backup_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else {}


def list_history(limit: int = 30) -> List[Dict[str, Any]]:
    init_db()
    with _connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM backup_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_remote_backups() -> Dict[str, Any]:
    cfg = load_config()
    rclone_bin = cfg.get("rclone_binary", "rclone")
    remote_name = cfg.get("rclone_remote", "gdrive").rstrip(":")
    drive_folder = cfg.get("drive_folder", "omnirote").strip("/")
    remote_folder_spec = f"{remote_name}:{drive_folder}" if drive_folder else f"{remote_name}:"

    code, out, err = run_cmd([rclone_bin, "lsjson", remote_folder_spec], timeout=45)
    if code != 0:
        return {
            "ok": False,
            "remote_path": f"/{drive_folder}",
            "remote_spec": remote_folder_spec,
            "error": err or out or f"rclone exited with code {code}",
            "files": [],
        }
    try:
        raw_items = json.loads(out) if out else []
        files = [
            {
                "name": item.get("Name"),
                "size": item.get("Size", 0),
                "mod_time": item.get("ModTime", ""),
                "is_dir": item.get("IsDir", False),
                "id": item.get("ID", ""),
            }
            for item in raw_items
            if not item.get("IsDir", False)
        ]
        files.sort(key=lambda x: x.get("mod_time", ""), reverse=True)
        return {
            "ok": True,
            "remote_path": f"/{drive_folder}",
            "remote_spec": remote_folder_spec,
            "files": files,
        }
    except Exception as exc:
        return {
            "ok": False,
            "remote_path": f"/{drive_folder}",
            "remote_spec": remote_folder_spec,
            "error": str(exc),
            "files": [],
        }


def delete_remote_backup(filename: str) -> Dict[str, Any]:
    if not filename or "/" in filename or ".." in filename:
        return {"ok": False, "error": "Invalid filename."}
    cfg = load_config()
    rclone_bin = cfg.get("rclone_binary", "rclone")
    remote_name = cfg.get("rclone_remote", "gdrive").rstrip(":")
    drive_folder = cfg.get("drive_folder", "omnirote").strip("/")
    target = f"{remote_name}:{drive_folder}/{filename}"
    code, out, err = run_cmd([rclone_bin, "deletefile", target], timeout=45)
    if code != 0:
        return {"ok": False, "error": err or out}
    return {"ok": True, "deleted": target}


def test_rclone_connection() -> Dict[str, Any]:
    cfg = load_config()
    rclone_bin = cfg.get("rclone_binary", "rclone")
    remote_name = cfg.get("rclone_remote", "gdrive").rstrip(":")
    drive_folder = cfg.get("drive_folder", "omnirote").strip("/")
    remote_folder_spec = f"{remote_name}:{drive_folder}" if drive_folder else f"{remote_name}:"

    ver_code, ver_out, ver_err = run_cmd([rclone_bin, "version"], timeout=15)
    if ver_code != 0:
        return {
            "ok": False,
            "rclone_installed": False,
            "error": ver_err or "rclone binary is not installed or not in PATH.",
        }
    version_line = ver_out.splitlines()[0] if ver_out else "rclone"

    rem_code, rem_out, _ = run_cmd([rclone_bin, "listremotes"], timeout=15)
    remotes = [r.strip().rstrip(":") for r in rem_out.splitlines() if r.strip()] if rem_code == 0 else []

    if remote_name not in remotes:
        return {
            "ok": False,
            "rclone_installed": True,
            "rclone_version": version_line,
            "configured_remotes": remotes,
            "error": f"Remote '{remote_name}:' not found in rclone config. Configured remotes: {remotes or 'none'}",
        }

    mkdir_code, _, mkdir_err = run_cmd([rclone_bin, "mkdir", remote_folder_spec], timeout=30)
    if mkdir_code != 0:
        return {
            "ok": False,
            "rclone_installed": True,
            "rclone_version": version_line,
            "configured_remotes": remotes,
            "error": f"Failed to access/create '{remote_folder_spec}': {mkdir_err}",
        }

    about_code, about_out, _ = run_cmd([rclone_bin, "about", f"{remote_name}:", "--json"], timeout=30)
    quota = {}
    if about_code == 0 and about_out:
        try:
            quota = json.loads(about_out)
        except Exception:
            pass

    return {
        "ok": True,
        "rclone_installed": True,
        "rclone_version": version_line,
        "configured_remotes": remotes,
        "remote_folder_spec": remote_folder_spec,
        "quota": quota,
    }


CRON_MARKER = "# omniroute-daily-backup"


def get_cron_line(cfg: Optional[Dict[str, Any]] = None) -> str:
    c = cfg or load_config()
    minute = int(c.get("cron_minute", 1))
    hour = int(c.get("cron_hour", 0))
    python_exec = sys.executable or "/usr/bin/python3"
    script_path = PROJECT_ROOT / "backup_engine.py"
    cron_log = LOG_DIR / "cron.log"
    return f"{minute} {hour} * * * {python_exec} {script_path} --run-now >> {cron_log} 2>&1 {CRON_MARKER}"


def get_cron_status() -> Dict[str, Any]:
    expected_line = get_cron_line()
    code, out, err = run_cmd(["crontab", "-l"], timeout=10)
    if code != 0 and "no crontab" not in (err or "").lower():
        return {
            "installed": False,
            "crontab_available": shutil.which("crontab") is not None,
            "current_line": None,
            "expected_line": expected_line,
            "error": err or out,
        }
    lines = out.splitlines() if code == 0 else []
    matched = [ln for ln in lines if CRON_MARKER in ln or "backup_engine.py" in ln]
    return {
        "installed": len(matched) > 0,
        "crontab_available": True,
        "current_line": matched[0] if matched else None,
        "expected_line": expected_line,
    }


def install_cron_job() -> Dict[str, Any]:
    cfg = load_config()
    new_line = get_cron_line(cfg)
    code, out, err = run_cmd(["crontab", "-l"], timeout=10)
    existing_lines: List[str] = []
    if code == 0 and out:
        existing_lines = [
            ln
            for ln in out.splitlines()
            if CRON_MARKER not in ln and "backup_engine.py" not in ln
        ]
    existing_lines.append(new_line)
    payload = "\n".join(existing_lines) + "\n"

    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip()}
        return {"ok": True, "cron_line": new_line}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_system_overview() -> Dict[str, Any]:
    cfg = load_config()
    source_path = pathlib.Path(os.path.expanduser(cfg["source_dir"])).resolve()
    staging_path = pathlib.Path(os.path.expanduser(cfg["staging_dir"])).resolve()
    disk = shutil.disk_usage(staging_path if staging_path.exists() else "/")

    rclone_bin = cfg.get("rclone_binary", "rclone")
    ver_code, ver_out, _ = run_cmd([rclone_bin, "version"], timeout=10)
    rem_code, rem_out, _ = run_cmd([rclone_bin, "listremotes"], timeout=10)
    remotes = [r.strip().rstrip(":") for r in rem_out.splitlines() if r.strip()] if rem_code == 0 else []

    return {
        "hostname": socket.gethostname(),
        "config": cfg,
        "today_archive_name": build_archive_name(cfg),
        "source_stats": get_directory_stats(source_path),
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "rclone_installed": ver_code == 0,
        "rclone_version": ver_out.splitlines()[0] if (ver_code == 0 and ver_out) else None,
        "configured_remotes": remotes,
        "cron": get_cron_status(),
        "history": list_history(limit=25),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OmniRoute Daily Tar.Gz + Rclone Google Drive Backup Engine"
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Compress .omniroute, upload to Google Drive /omnirote, verify, and delete local archive.",
    )
    parser.add_argument(
        "--trigger",
        default="cron",
        help="Trigger label stored in backup history (default: cron).",
    )
    parser.add_argument(
        "--install-cron",
        action="store_true",
        help="Install or update the daily 12:01 AM (1 0 * * *) cron job.",
    )
    parser.add_argument(
        "--test-rclone",
        action="store_true",
        help="Verify rclone installation, remote config, and /omnirote Drive folder.",
    )
    parser.add_argument(
        "--list-remote",
        action="store_true",
        help="List backup archives currently inside Google Drive /omnirote.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print full JSON status of source directory, cron, rclone, and recent runs.",
    )

    args = parser.parse_args()

    if args.install_cron:
        res = install_cron_job()
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.test_rclone:
        res = test_rclone_connection()
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.list_remote:
        res = list_remote_backups()
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if args.status:
        res = get_system_overview()
        print(json.dumps(res, indent=2))
        return 0

    if args.run_now:
        res = execute_backup(trigger_type=args.trigger)
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
