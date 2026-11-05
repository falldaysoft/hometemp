#!/bin/bash
# Install the collector on a Mac that logs in automatically (default: idlemac.local).
# Usage: collector/tools/install-remote.sh [host]
# Builds the app into collector/build first (signed with a profile that includes the host), so what's installed
# is always the current source. Expects an upload token at ~/Library/Application Support/HomeTemp/token on the host.
set -euo pipefail
HOST="${1:-idlemac.local}"
cd "$(dirname "$0")/.."
APP=build/Build/Products/Debug-maccatalyst/HomeTempCollector.app
xcodegen generate --quiet
xcodebuild -quiet -scheme HomeTempCollector -destination 'platform=macOS,variant=Mac Catalyst' \
  -configuration Debug -derivedDataPath build build

ssh "$HOST" 'mkdir -p ~/Applications ~/Library/LaunchAgents "$HOME/Library/Application Support/HomeTemp"
  launchctl bootout gui/$(id -u)/ca.fallday.hometemp.collector 2>/dev/null || true
  pkill -x HomeTempCollector 2>/dev/null || true
  rm -rf ~/Applications/HomeTempCollector.app'
rsync -a "$APP" "$HOST":Applications/
ssh "$HOST" 'sed "s#APP_PATH#$HOME/Applications/HomeTempCollector.app#" > ~/Library/LaunchAgents/ca.fallday.hometemp.collector.plist' < launchd/ca.fallday.hometemp.collector.plist
ssh "$HOST" 'launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ca.fallday.hometemp.collector.plist && sleep 3 && pgrep -x HomeTempCollector && echo running'
