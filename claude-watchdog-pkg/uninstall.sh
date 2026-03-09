#!/usr/bin/env bash
# Claude Watchdog — Uninstall Script
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
PLIST="$HOME/Library/LaunchAgents/com.claude.watchdog.plist"

echo "=== Claude Watchdog Uninstaller ==="
echo ""

# Stop running watchdog
if pgrep -f "claude-watchdog.*--web" &>/dev/null; then
    echo "Stopping running watchdog..."
    pkill -f "claude-watchdog.*--web" || true
fi

# Unload launchd
if [ -f "$PLIST" ]; then
    echo "Unloading launchd service..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm "$PLIST"
fi

# Remove binaries
for cmd in claude-watchdog claude-session ollama-cli; do
    if [ -f "$INSTALL_DIR/$cmd" ]; then
        echo "Removing $INSTALL_DIR/$cmd"
        rm "$INSTALL_DIR/$cmd"
    fi
done

echo ""
echo "Binaries removed. Data preserved at ~/.claude/watchdog/"
echo "To remove data too:  rm -rf ~/.claude/watchdog/"
echo "To remove hooks:     edit ~/.claude/settings.json"
