#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Airzone Pi Installer
# ═══════════════════════════════════════════════════════════════════════════════
# Run on a fresh Raspberry Pi OS (Bookworm):
#
#   1. Copy the airzone/ folder to the Pi:
#      scp -r ~/ClaudeCodeProjects/airzone/ pi@<pi-ip>:~/airzone/
#
#   2. SSH into the Pi:
#      ssh pi@<pi-ip>
#
#   3. Edit credentials:
#      nano ~/airzone/.env
#
#   4. Run installer:
#      cd ~/airzone/pi && bash install.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -e

INSTALL_DIR="$HOME/airzone"
VENV_DIR="$INSTALL_DIR/venv"
PI_DIR="$INSTALL_DIR/pi"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║    Airzone Pi Installer              ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ───────────────────────────────────────────────────
echo "▶ Installing system packages..."
sudo apt update -qq
sudo apt install -y python3-venv python3-pip sqlite3

# ── 2. Python virtual environment ────────────────────────────────────────────
echo "▶ Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "▶ Installing Python packages..."
pip install --upgrade pip -q
pip install requests flask gunicorn -q

# ── 3. Data directory ────────────────────────────────────────────────────────
echo "▶ Creating data directory..."
mkdir -p "$PI_DIR/data"

# ── 4. Credentials env file ─────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cat > "$INSTALL_DIR/.env" << 'ENVEOF'
# Airzone Cloud credentials
# Fill these in, then restart the daemon:
#   sudo systemctl restart airzone-daemon
AIRZONE_EMAIL=
AIRZONE_PASSWORD=
ENVEOF
    echo ""
    echo "  ⚠  Created $INSTALL_DIR/.env"
    echo "     Edit it with your Airzone credentials:"
    echo "     nano $INSTALL_DIR/.env"
    echo ""
fi

# ── 5. systemd services ─────────────────────────────────────────────────────
echo "▶ Installing systemd services..."

# Update service files with actual username if not 'pi'
CURRENT_USER=$(whoami)
if [ "$CURRENT_USER" != "pi" ]; then
    echo "  (Adjusting service files for user: $CURRENT_USER)"
    sed -i "s|User=pi|User=$CURRENT_USER|g" "$PI_DIR/airzone-daemon.service"
    sed -i "s|User=pi|User=$CURRENT_USER|g" "$PI_DIR/airzone-dashboard.service"
    sed -i "s|/home/pi/|/home/$CURRENT_USER/|g" "$PI_DIR/airzone-daemon.service"
    sed -i "s|/home/pi/|/home/$CURRENT_USER/|g" "$PI_DIR/airzone-dashboard.service"
fi

sudo cp "$PI_DIR/airzone-daemon.service" /etc/systemd/system/
sudo cp "$PI_DIR/airzone-dashboard.service" /etc/systemd/system/
sudo systemctl daemon-reload

# ── 6. Check if credentials are set ─────────────────────────────────────────
source "$INSTALL_DIR/.env" 2>/dev/null || true
if [ -z "$AIRZONE_EMAIL" ] || [ -z "$AIRZONE_PASSWORD" ]; then
    echo ""
    echo "  ⚠  Credentials not yet set in $INSTALL_DIR/.env"
    echo "     Services will be enabled but NOT started."
    echo "     After editing .env, start them with:"
    echo "       sudo systemctl start airzone-daemon airzone-dashboard"
    echo ""
    sudo systemctl enable airzone-daemon airzone-dashboard
else
    echo "▶ Starting services..."
    sudo systemctl enable --now airzone-daemon airzone-dashboard
fi

# ── Done ─────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo "╔══════════════════════════════════════╗"
echo "║    Installation complete!            ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Dashboard:  http://${PI_IP:-<pi-ip>}:5000"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status airzone-daemon"
echo "    sudo systemctl status airzone-dashboard"
echo "    journalctl -u airzone-daemon -f"
echo "    sqlite3 $PI_DIR/data/airzone_history.db"
echo ""
echo "  Config:  $PI_DIR/airzone_pi_config.json"
echo "  Logs:    $PI_DIR/data/airzone_daemon.log"
echo "  DB:      $PI_DIR/data/airzone_history.db"
echo ""
