#!/usr/bin/env bash
# Optional remote view of the latex-panel (see latexpanel/remote.py). Called as `panel.sh remote ...`.
#
#   remote on [--bind lan|tailscale|all|IP] [--port N]   start serving (panel keeps its history)
#   remote off                                           stop serving
#   remote status | url                                  is it serving, and where
#
# The panel polls for the flag file, so switching on/off never restarts it.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${LATEX_PANEL_DATA:-$HOME/.local/share/latex-panel}"
FLAG="$DATA/remote.on"
STATE="$DATA/remote.json"

field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2]) or "")' "$STATE" "$1" 2>/dev/null || true; }

case "${1:-status}" in
  on)
    shift
    bind=lan port=8765
    while (( $# )); do
      case $1 in
        --bind) bind=$2; shift 2;;
        --port) port=$2; shift 2;;
        *) echo "remote on: unknown option $1" >&2; exit 2;;
      esac
    done
    [[ $port =~ ^[0-9]+$ ]] || { echo "bad port $port" >&2; exit 2; }
    bash "$REPO/scripts/panel.sh" ensure >/dev/null
    mkdir -p "$DATA"; rm -f "$STATE"
    printf '{"bind": "%s", "port": %s}\n' "$bind" "$port" > "$FLAG"
    for _ in $(seq 1 30); do
      [[ -f $STATE ]] && break
      sleep 0.2
    done
    err=$(field error); url=$(field url)
    if [[ -n $err ]]; then echo "remote failed to start: $err" >&2; rm -f "$FLAG"; exit 1; fi
    if [[ -z $url ]]; then
      rm -f "$FLAG"
      echo "the running panel didn't pick up the remote flag - it probably predates remote support." >&2
      echo "restart it (panel.sh stop && panel.sh ensure; this clears its card history), then retry." >&2
      exit 1
    fi
    echo "remote panel serving - open on the tablet:"
    echo "  $url"
    ;;
  off)
    rm -f "$FLAG"
    for _ in $(seq 1 15); do [[ -f $STATE ]] || break; sleep 0.2; done
    echo "remote panel off"
    ;;
  status)
    if [[ -f $FLAG && -f $STATE && -z $(field error) ]]; then
      echo "serving at $(field url) ($(field clients) browser(s) connected)"
    elif [[ -f $FLAG ]]; then
      echo "switched on but not serving $(field error) (is the panel running? panel.sh ensure)"
    else
      echo "off"
    fi
    ;;
  url)
    [[ -f $STATE ]] && [[ -n $(field url) ]] || { echo "remote is off (panel.sh remote on)" >&2; exit 1; }
    field url
    ;;
  *)
    echo "usage: panel.sh remote {on [--bind lan|tailscale|all|IP] [--port N]|off|status|url}" >&2
    exit 2
    ;;
esac
