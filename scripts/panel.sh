#!/usr/bin/env bash
# Idempotent control script for the latex-panel window, driven by the Claude skill.
#
#   panel.sh ensure   - launch the panel if it is not already running (default)
#   panel.sh write     - read markdown from stdin, render it in the panel (implies ensure)
#   panel.sh status   - report whether the panel is running
#   panel.sh stop     - close the panel
#
# The panel is a GTK process; it must be running for anything to be visible.
# This script is the only thing the skill calls.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDERER="$REPO/latexpanel/renderer.py"
WATCH_FILE="/tmp/claude_response.md"
PAT="latexpanel/renderer.py"

running() { pgrep -f "$PAT" >/dev/null 2>&1; }

ensure() {
  if running; then
    echo "panel already running (pid $(pgrep -f "$PAT" | head -1))"
    return 0
  fi
  # Detach fully so the panel outlives this shell / tool call.
  nohup python3 "$RENDERER" >/tmp/latex-panel.log 2>&1 &
  disown || true
  for _ in $(seq 1 20); do
    running && { echo "panel started"; return 0; }
    sleep 0.2
  done
  echo "panel failed to start; see /tmp/latex-panel.log" >&2
  return 1
}

case "${1:-ensure}" in
  ensure)
    ensure
    ;;
  write)
    ensure
    cat > "$WATCH_FILE"
    echo "wrote $(wc -c < "$WATCH_FILE") bytes to $WATCH_FILE"
    ;;
  status)
    if running; then echo "running (pid $(pgrep -f "$PAT" | head -1))"; else echo "not running"; fi
    ;;
  stop)
    pkill -f "$PAT" && echo "panel stopped" || echo "panel was not running"
    ;;
  *)
    echo "usage: panel.sh {ensure|write|status|stop}" >&2
    exit 2
    ;;
esac
