#!/bin/bash
# status.sh — Quick health check for the running bot.
# Run from the server: bash deploy/status.sh

SERVICE="polybot"

echo "=== Bot Service ==="
systemctl is-active --quiet $SERVICE && echo "✓ RUNNING" || echo "✗ STOPPED"
systemctl status $SERVICE --no-pager -l | tail -5

echo ""
echo "=== Last 20 log lines ==="
journalctl -u $SERVICE -n 20 --no-pager

echo ""
echo "=== Auto-update timer ==="
systemctl status ${SERVICE}-updater.timer --no-pager | grep -E "Active|Trigger"

echo ""
echo "=== Current git version ==="
git -C /opt/polybot log --oneline -3
