#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NGROK_BIN="${NGROK_BIN:-$(command -v ngrok || true)}"
NGROK_CONFIG="${NGROK_CONFIG_PATH:-$HOME/Library/Application Support/ngrok/ngrok.yml}"
TEMPLATE="$PROJECT_ROOT/deploy/com.example.ai-daytrader-ngrok.plist"
TARGET="$HOME/Library/LaunchAgents/com.example.ai-daytrader-ngrok.plist"
LABEL="com.example.ai-daytrader-ngrok"

if [[ -z "$NGROK_BIN" ]]; then
  print -u2 "ngrok is not installed. Install it with: brew install ngrok/ngrok/ngrok"
  exit 1
fi

if [[ ! -f "$NGROK_CONFIG" ]]; then
  print -u2 "ngrok authentication is not configured."
  print -u2 "Run: ngrok config add-authtoken <TOKEN_FROM_NGROK_DASHBOARD>"
  exit 1
fi

"$NGROK_BIN" config check --config "$NGROK_CONFIG"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
  -e "s|/ABSOLUTE/PATH/TO/ngrok|$NGROK_BIN|g" \
  -e "s|/Users/YOU/Library/Application Support/ngrok/ngrok.yml|$NGROK_CONFIG|g" \
  -e "s|/ABSOLUTE/PATH/ai-daytrader-sim|$PROJECT_ROOT|g" \
  "$TEMPLATE" > "$TARGET"

plutil -lint "$TARGET"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
launchctl kickstart -k "gui/$UID/$LABEL"

print "Installed and started $LABEL"
print "Status: launchctl print gui/$UID/$LABEL"
print "Logs:   tail -f /tmp/ai-daytrader-ngrok.out.log"
