#!/bin/bash
PLIST_NAME="com.towercast.daemon.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

launchctl unload "$TARGET_PLIST" 2>/dev/null || true
rm -f "$TARGET_PLIST"

echo "🛑 TowerCast background LaunchAgent has been unloaded and removed."
