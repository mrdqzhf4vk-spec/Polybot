#!/bin/bash
# setup_server.sh — Run this ONCE on a fresh VPS to install and start the bot.
#
# Usage:
#   TELEGRAM_BOT_TOKEN="xxx" TELEGRAM_CHAT_ID="yyy" REPO_URL="https://github.com/mrdqzhf4vk-spec/polybot.git" bash setup_server.sh

set -e

REPO_URL="${REPO_URL:-}"
REPO_BRANCH="${REPO_BRANCH:-main}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
INSTALL_DIR="/opt/polybot"
SERVICE="polybot"

if [[ -z "$REPO_URL" || -z "$BOT_TOKEN" || -z "$CHAT_ID" ]]; then
    echo "ERROR: set REPO_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID before running."
    exit 1
fi

echo "=== Polybot VPS Setup ==="

apt-get update -qq
apt-get install -y -qq python3 python3-pip git curl

# Install httpx — works on Ubuntu 22.04+ and older
pip3 install httpx --break-system-packages 2>/dev/null || pip3 install httpx

if [[ -d "$INSTALL_DIR" ]]; then
    echo "Updating existing install at $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull origin "$REPO_BRANCH"
else
    echo "Cloning repo to $INSTALL_DIR ..."
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cat > "$INSTALL_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_CHAT_ID=$CHAT_ID
EOF
chmod 600 "$INSTALL_DIR/.env"
echo "Credentials saved to $INSTALL_DIR/.env"

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Polybot P8 Telegram Signal Bot
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=/usr/bin/python3 $INSTALL_DIR/telegram_signals.py --live
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/${SERVICE}-updater.service" <<EOF
[Unit]
Description=Polybot auto-update from git

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=/bin/bash -c 'git -C $INSTALL_DIR pull origin $REPO_BRANCH && systemctl restart $SERVICE'
EOF

cat > "/etc/systemd/system/${SERVICE}-updater.timer" <<EOF
[Unit]
Description=Run Polybot updater every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=${SERVICE}-updater.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl start  "$SERVICE"
systemctl enable "${SERVICE}-updater.timer"
systemctl start  "${SERVICE}-updater.timer"

echo ""
echo "=== Done ==="
echo "Bot status:  systemctl status $SERVICE"
echo "Live logs:   journalctl -u $SERVICE -f"
echo "Auto-update: every 5 min from git"
echo ""
echo "Any fix pushed to the repo -> live on server within 5 minutes."
