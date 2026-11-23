#!/bin/bash
# One-time setup on a Mac that stays on. Paste into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/ttiillol/sniper-radar/main/deploy/mac_setup.sh | bash
# Paper trading only. No keys, no passwords, no money.
set -e
DIR="$HOME/sniper-radar"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin
  # update the program files to the latest version (local data is left alone)
  git -C "$DIR" checkout -q origin/main -- scanner.py build_page.py desk tests deploy requirements.txt
else
  git clone -q https://github.com/ttiillol/sniper-radar "$DIR"
fi
cd "$DIR"
/usr/bin/python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/python -m pytest -q tests                      # refuse to start if any rule test fails
PL="$HOME/Library/LaunchAgents/com.sniper-radar.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.sniper-radar</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/deploy/mac_run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/data/mac.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/mac.log</string>
</dict></plist>
PLIST
launchctl unload "$PL" 2>/dev/null || true
launchctl load "$PL"
echo "✅ sniper-radar is running and will restart automatically (stop: launchctl unload $PL)"
