# OmniRoute Automated Google Drive Backup Manager

Production-ready backup engine and **fully API-driven** web management dashboard.

Every day at **12:01 AM (`1 0 * * *`)**, the engine:
1. Compresses `/home/admin/.omniroute` into `/tmp/omniroute-dd-mm-yy.tar.gz` (for example, `omniroute-18-10-26.tar.gz`) and verifies gzip/tar integrity + SHA-256 checksum.
2. Ensures the `/omnirote` folder exists on your Google Drive (`rclone mkdir gdrive:omnirote`).
3. Uploads the dated archive to `gdrive:omnirote/omniroute-dd-mm-yy.tar.gz` using `rclone copyto`.
4. Verifies the remote file exists on Google Drive with the exact matching byte size (`rclone lsjson gdrive:omnirote/omniroute-dd-mm-yy.tar.gz`).
5. **Deletes `omniroute-dd-mm-yy.tar.gz` from the VPS** only after upload + size verification succeed (protecting your local backup if network connectivity ever drops — can be disabled with `"delete_local_after_upload": false`).

## Project Structure

```text
TSun-Omniroute-Backup-Manager/
├── app.py                              # stdlib HTTP server: REST API + serves the dashboard
├── backup_engine.py                    # Core compression, rclone upload, remote verification & cron CLI
├── config.json                         # Configuration (paths, rclone remote, /omnirote folder, schedule, dashboard bind)
├── install.sh                          # One-command installer for rclone, cron (1 0 * * *), and systemd
├── templates/
│   └── index.html                      # Interactive Web Frontend Dashboard (single-file, wired to the REST API)
├── Skills/
│   └── DESIGN.md                       # Design system notes for the dashboard
├── systemd/
│   └── omniroute-dashboard.service     # Systemd unit for 24/7 Web Dashboard + in-process scheduler fallback
└── logs/                               # Execution and cron logs
```

## How the Dashboard Talks to the Backend

`templates/index.html` contains **no simulated data** — every widget is bound to the live REST API in `app.py`:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` or `/index.html` | Serves `templates/index.html` |
| `GET` | `/api/status` | Hostname, config, today's archive, source stats, disk, rclone, cron state, history — wrapped as `{ok, data:[...]}` |
| `GET` | `/api/history` | Backup history rows (durations, sha256, verification state, real `step_log`) |
| `GET` | `/api/remote/files` | Fresh `rclone lsjson` listing of the Drive folder (graceful `{ok:false, error}` if rclone is missing) |
| `GET` | `/api/config` | Current configuration |
| `GET` | `/api/logs` | Last 200 lines of the execution log (dashboard tails the last 20) |
| `GET` | `/api/source?name=<key>` | Read a whitelisted source file for the Code tab (`app.py`, `backup_engine.py`, `config.json`, `install.sh`, `README.md`, `systemd/omniroute-dashboard.service`) |
| `POST` | `/api/backup/run` | Body `{"trigger":"manual-ui"}` → runs a **real backup synchronously**; returns `409` if one is already running |
| `POST` | `/api/config` | Save configuration (send the **full** config object — missing keys reset to defaults) |
| `POST` | `/api/cron/sync` | Write the generated crontab line (`… # omniroute-daily-backup`) |
| `POST` | `/api/rclone/test` | Verify rclone install + remote + Drive folder connectivity |
| `POST` | `/api/remote/delete` | Body `{"filename": ...}` → delete an archive from Google Drive |

Dashboard features powered by those endpoints:

- **Run Backup** — triggers a real job, replays the engine's `step_log` live in the terminal, derives per-step badges (Compression → Upload → Verify → Purge), and shows real MB/s + SHA-256.
- **Audit History** — real runs with trigger, duration, purge and Drive verification state (FAILED / RUNNING / VERIFIED / UPLOADED).
- **Remote tab / restore modal** — real Drive folder listing with a generated restore script (`rclone copyto` → `tar -xzf` into your `source_dir`).
- **Config** — save tunnel (`vps_user@vps_host`), paths, Drive folder, schedule and purge policy; crontab preview regenerates from saved values.
- **Cron** — shows installed vs expected line and installs it on demand.
- **Source viewer** — reads the real project files from `/api/source`.
- Auto-refreshes status/history/remote every 30 s (paused while a backup runs or the tab is hidden).

## Complete Step-by-Step Setup Guide

### Step 1 — Install `rclone`

```bash
sudo apt-get update && sudo apt-get install -y curl unzip python3
curl https://rclone.org/install.sh | sudo bash
rclone version
```

### Step 2 — Connect `rclone` to Google Drive (Headless VPS Method)

1. On your **terminal**, start configuration:
   ```bash
   rclone config
   ```
2. Follow these exact prompts:
   - Press `n` for **New remote**
   - Enter name: **`gdrive`** (must match `"rclone_remote": "gdrive"` in `config.json`)
   - Storage type: enter **`drive`** (or the number corresponding to **Google Drive**)
   - `client_id` / `client_secret`: press **Enter** to leave blank (or supply your own Google Cloud OAuth credentials for higher API quotas)
   - `scope`: enter **`1`** (Full access all files, or `drive.file` if you only want rclone to access files/folders it creates)
   - `service_account_file`: press **Enter** to leave blank
   - `Edit advanced config?`: press **`n`**
   - `Use web browser to automatically authenticate rclone with remote?`: press **`n`** (since you are on a headless VPS)
3. `rclone` will print a command like:
   ```bash
   rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlIn0"
   ```
4. Run that exact `rclone authorize ...` command in a terminal on your **local laptop/PC** (which has a browser). Sign into your Google account, click **Allow**, copy the verification token pasted in your laptop terminal, and paste it back into your VPS terminal.
5. Configure as a Shared Drive (Team Drive)? Press **`n`**, then **`y`** to confirm, and **`q`** to quit `rclone config`.

### Step 3 — Verify Google Drive & Create `/omnirote`

Test that `rclone` can reach your Google Drive and create the `/omnirote` folder:

```bash
rclone mkdir gdrive:omnirote
rclone lsd gdrive:
```

> **Tip:** If you meant `/omniroute` instead of `/omnirote`, change `"drive_folder"` in `config.json` (or edit it in the dashboard and hit **Save Configuration**).

### Step 4 — Deploy & Install the Project on the VPS

Place the project at `/home/admin/TSun-Omniroute-Backup-Manager` and run the installer:

```bash
mkdir -p /home/admin/TSun-Omniroute-Backup-Manager
cd /home/admin/TSun-Omniroute-Backup-Manager
# Extract TSun-Omniroute-Backup-Manager.zip here
unzip TSun-Omniroute-Backup-Manager.zip -d /home/admin/TSun-Omniroute-Backup-Manager
chmod +x install.sh backup_engine.py app.py
./install.sh
```

For local development (Windows/macOS/Linux) just start the server directly:

```bash
python3 app.py                 # binds dashboard_host:dashboard_port from config.json
python3 app.py --with-scheduler  # also run the in-process 12:01 AM fallback scheduler
# flags: --host <ip> --port <port> override config.json
```

### Step 5 — Verify the 12:01 AM Cron Job & Run a Live Test

Check that the daily `12:01 AM` cron job (`1 0 * * *`) is active (or install it from the dashboard's **Cron** tab):

```bash
crontab -l
# Expected output:
# 1 0 * * * /usr/bin/python3 /home/admin/TSun-Omniroute-Backup-Manager/backup_engine.py --run-now >> /home/admin/TSun-Omniroute-Backup-Manager/logs/cron.log 2>&1 # omniroute-daily-backup
```

Trigger an immediate test backup from the terminal (or click **Run Backup Now** in the dashboard):

```bash
python3 /home/admin/TSun-Omniroute-Backup-Manager/backup_engine.py --run-now --trigger manual-cli
```

Confirm the archive is inside `/omnirote` on Google Drive and deleted locally:

```bash
rclone ls gdrive:omnirote
ls -la /tmp/omniroute-*.tar.gz
```


Then open **`http://localhost:8080`** in your browser to trigger backups on demand, inspect `/omnirote` files on Google Drive, check SHA-256 checksums, edit schedule settings, tail logs, inspect source files, and sync the crontab — everything runs against the live API, no mock data.
