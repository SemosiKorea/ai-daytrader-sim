#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

if [[ -z "$UV_BIN" ]]; then
  print -u2 "uv is not installed or is not on PATH"
  exit 1
fi

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  print -u2 "Missing $PROJECT_ROOT/.env"
  exit 1
fi

mkdir -p "$LAUNCH_AGENTS"

for service in sim feed; do
  label="com.example.ai-daytrader-$service"
  template="$PROJECT_ROOT/deploy/$label.plist"
  target="$LAUNCH_AGENTS/$label.plist"

  sed \
    -e "s|/ABSOLUTE/PATH/ai-daytrader-sim|$PROJECT_ROOT|g" \
    -e "s|/ABSOLUTE/PATH/TO/uv|$UV_BIN|g" \
    "$template" > "$target"

  plutil -lint "$target"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  installed=false
  bootstrap_error=""
  for attempt in 1 2 3 4 5; do
    if bootstrap_error="$(launchctl bootstrap "gui/$UID" "$target" 2>&1)"; then
      installed=true
      break
    fi
    sleep 0.5
  done
  if [[ "$installed" != true ]]; then
    print -u2 "$bootstrap_error"
    print -u2 "Failed to install $label after five attempts"
    exit 1
  fi
  launchctl kickstart -k "gui/$UID/$label"
  print "Installed and started $label"
done

print "API log:  tail -f /tmp/ai-daytrader-sim.out.log"
print "Feed log: tail -f /tmp/ai-daytrader-feed.out.log"
