#!/bin/bash
set -e

PLIST_NAME="com.towercast.daemon.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PLIST="$SCRIPT_DIR/$PLIST_NAME"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$PLIST_NAME"

mkdir -p "$TARGET_DIR"
mkdir -p "$SCRIPT_DIR/../data"

# Unload previous instance if loaded
launchctl unload "$TARGET_PLIST" 2>/dev/null || true

# Copy plist
cp "$SOURCE_PLIST" "$TARGET_PLIST"

# Load LaunchAgent
launchctl load "$TARGET_PLIST"

echo "✅ TowerCast daemon installed and started as a macOS LaunchAgent!"
echo "   It will automatically run at login in the background."
echo "   Logs: $SCRIPT_DIR/../data/towercast.log"
echo "   To stop: ./uninstall.sh"
