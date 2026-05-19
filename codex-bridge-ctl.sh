#!/usr/bin/env bash
# codex-bridge-ctl.sh — Control the Codex-Ollama protocol bridge
# Usage: codex-bridge-ctl.sh {start|stop|restart|status|logs}
set -euo pipefail

PLIST="com.x.codex-bridge"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST}.plist"
LOG_DIR="$HOME/ai-worklogs"

case "${1:-}" in
  start)
    echo -n "Starting codex-bridge... "
    launchctl load -w "$PLIST_PATH" 2>/dev/null && echo "OK" || echo "FAILED"
    ;;
  stop)
    echo -n "Stopping codex-bridge... "
    launchctl bootout "gui/$(id -u)/${PLIST}" 2>/dev/null && echo "OK" || echo "not running"
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if launchctl list | grep -q "$PLIST"; then
      echo "codex-bridge: RUNNING (pid=$(launchctl list | awk "/$PLIST/{print \$1}"))"
      curl -sf http://127.0.0.1:11434/__health 2>/dev/null && echo "Health: OK" || echo "Health: unreachable"
    else
      echo "codex-bridge: NOT RUNNING"
    fi
    ;;
  logs)
    if [ -f "$LOG_DIR/codex-bridge.out.log" ]; then
      tail -f "$LOG_DIR/codex-bridge.out.log" "$LOG_DIR/codex-bridge.err.log"
    else
      echo "No logs yet."
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
