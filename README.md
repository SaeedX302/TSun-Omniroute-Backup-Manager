# OmniRoute Automated Google Drive Backup Manager (Python + Web Frontend)

Production-ready Python backup engine and web management dashboard tailored for **`saeed@ip-172-31-37-134:~$`**.

Every day at **12:01 AM (`1 0 * * *`)**, the engine:
1. Compresses `/home/saeed/.omniroute` into `/tmp/omniroute-dd-mm-yy.tar.gz` (for example, `omniroute-16-10-26.tar.gz`) and verifies gzip/tar integrity + SHA-256 checksum.
2. Ensures the `/omnirote` folder exists on your Google Drive (`rclone mkdir gdrive:omnirote`).
3. Uploads the dated archive to `gdrive:omnirote/omniroute-dd-mm-yy.tar.gz` using `rclone copyto`.
4. Verifies the remote file exists on Google Drive with the exact matching byte size (`rclone lsjson gdrive:omnirote/omniroute-dd-mm-yy.tar.gz`).
5. **Deletes `omniroute-dd-mm-yy.tar.gz` from the VPS** only after upload + size verification succeed (protecting your local backup if network connectivity ever drops).

## Project Structure

```text
omniroute-backup-manager/
├── app.py                              # Python HTTP & REST API server for the Web Frontend
├── backup_engine.py                    # Core compression, rclone upload, remote verification & cron CLI
├── config.json                         # Configuration (paths, rclone remote, /omnirote folder, 00:01 schedule)
├── install.sh                          # One-command installer for rclone, cron (1 0 * * *), and systemd
├── static/
│   └── index.html                      # Interactive Web Frontend Dashboard
├── systemd/
│   └── omniroute-dashboard.service     # Systemd unit file for 24/7 Web Dashboard + fallback scheduler
└── logs/                               # Execution and cron logs
```

## Complete Step-by-Step Setup Guide on `saeed@ip-172-31-37-134`

### Step 1 — Install `rclone` on your VPS

SSH into your VPS (`saeed@ip-172-31-37-134`) and install `rclone`:

```bash
sudo apt-get update && sudo apt-get install -y curl unzip python3
curl https://rclone.org/install.sh | sudo bash
rclone version
```

### Step 2 — Connect `rclone` to Google Drive (Headless VPS Method)

Because `saeed@ip-172-31-37-134` is a headless server without a local browser, authorize Google Drive using either an SSH tunnel or a local laptop with `rclone authorize`:

1. On your **VPS**, start configuration:
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

> **Tip:** If you meant `/omniroute` instead of `/omnirote`, you can change `"drive_folder": "omnirote"` to `"omniroute"` at any time inside the Web Dashboard or `config.json`.

### Step 4 — Deploy & Install the Project on the VPS

Place the project at `/home/saeed/omniroute-backup-manager` and run the installer:

```bash
mkdir -p /home/saeed/omniroute-backup-manager
cd /home/saeed/omniroute-backup-manager
# Extract omniroute-backup-manager.zip here
unzip omniroute-backup-manager.zip -d /home/saeed/omniroute-backup-manager
chmod +x install.sh backup_engine.py app.py
./install.sh
```

### Step 5 — Verify the 12:01 AM Cron Job & Run a Live Test

Check that the daily `12:01 AM` cron job (`1 0 * * *`) is active:

```bash
crontab -l
# Expected output:
# 1 0 * * * /usr/bin/python3 /home/saeed/omniroute-backup-manager/backup_engine.py --run-now >> /home/saeed/omniroute-backup-manager/logs/cron.log 2>&1 # omniroute-daily-backup
```

Trigger an immediate test backup from the terminal (or click **Run Backup Now** in the Web Frontend):

```bash
python3 /home/saeed/omniroute-backup-manager/backup_engine.py --run-now --trigger manual-cli
```

Confirm the archive is inside `/omnirote` on Google Drive and deleted locally:

```bash
rclone ls gdrive:omnirote
ls -la /tmp/omniroute-*.tar.gz
```

### Step 6 — Access the Web Frontend Dashboard

The installer registers `omniroute-dashboard.service` on port `8080`. To access it securely from your laptop without exposing port 8080 publicly on AWS Security Groups, open an SSH tunnel from your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 saeed@<YOUR-VPS-PUBLIC-IP>
```

Then open **`http://localhost:8080`** in your browser to trigger backups on demand, inspect `/omnirote` files on Google Drive, check SHA-256 checksums, edit schedule settings, and view real-time logs.
