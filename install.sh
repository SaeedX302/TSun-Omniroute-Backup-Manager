#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo " OmniRoute Automated Backup Manager — VPS Installer"
echo " Target VPS: saeed@ip-172-31-37-134"
echo "============================================================"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/backup_engine.py" "$PROJECT_DIR/app.py"

# 1. Check Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "[*] Installing Python 3..."
    sudo apt-get update && sudo apt-get install -y python3
fi

# 2. Check rclone
if ! command -v rclone >/dev/null 2>&1; then
    echo "[*] Installing rclone..."
    sudo apt-get update && sudo apt-get install -y curl unzip
    curl https://rclone.org/install.sh | sudo bash
else
    echo "[✓] rclone is already installed: $(rclone version | head -n 1)"
fi

# 3. Install 12:01 AM daily cron job (1 0 * * *)
echo "[*] Registering daily 12:01 AM cron job (1 0 * * *)..."
python3 "$PROJECT_DIR/backup_engine.py" --install-cron

# 4. Install and enable systemd service for the Web Frontend Dashboard
if command -v systemctl >/dev/null 2>&1; then
    echo "[*] Installing systemd service (omniroute-dashboard.service)..."
    SERVICE_TMP="/tmp/omniroute-dashboard.service"
    sed "s|/home/saeed/omniroute-backup-manager|$PROJECT_DIR|g; s|User=saeed|User=$(whoami)|g; s|Group=saeed|Group=$(id -gn)|g" \
        "$PROJECT_DIR/systemd/omniroute-dashboard.service" > "$SERVICE_TMP"
    sudo mv "$SERVICE_TMP" /etc/systemd/system/omniroute-dashboard.service
    sudo systemctl daemon-reload
    sudo systemctl enable omniroute-dashboard.service
    sudo systemctl restart omniroute-dashboard.service
    echo "[✓] Web Frontend running on port 8080 (systemd service active)."
fi

echo ""
echo "============================================================"
echo " Installation Complete!"
echo " - Daily Cron Job: 1 0 * * * (12:01 AM every day)"
echo " - Archive Format: omniroute-dd-mm-yy.tar.gz"
echo " - Google Drive Folder: gdrive:omnirote"
echo " - Web Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo "============================================================"
