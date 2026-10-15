#!/usr/bin/env bash
# Idempotent control script for the latex-panel window, driven by the Claude skill.
#
#   panel.sh ensure   - launch the panel if it is not already running (default)
#   panel.sh write     - read markdown from stdin, render it as a NEW card (implies ensure)
#   panel.sh current [--latest|--entry N]  - copy a card's markdown to edit.md (prints the path)
#   panel.sh edit [--latest|--shown|--entry N] [FILE] - replace a card IN PLACE from stdin/FILE/edit.md
#   panel.sh status   - report whether the panel is running
#   panel.sh stop     - close the panel
#   panel.sh manim [-q l|m|h] [-s Scene]  - OPTIONAL: Manim scene .py on stdin -> render ->
#                       prints a ':::video <path>' line to put in the markdown you `write`.
#                       Failed renders print the error and exit 1; nothing is shown.
#
#   Panel -> Claude (interactive blocks, pins, selection comments, the message bar):
#   panel.sh watch    - stream user actions, one line each; run it under the Monitor tool
#   panel.sh events   - print unread actions (catch-up when no watch was running)
#   panel.sh wait [--id ID] [--timeout S]  - block until the user acts (on block ID)
#   panel.sh changed OLD.pdf NEW.pdf       - which pages differ visually
#
#   Claude -> panel (live updates, no new history entry):
#   panel.sh reply ID|'#SEQ' TEXT...       - show a reply under a block / in the chat strip
#   panel.sh suggest SEQ [--note N] < new  - in-place rewrite for a `rewrite` event
#   panel.sh tick ID ITEM [todo|doing|done]- update a :::checklist item
#   panel.sh flagged [--all] / filed FILE  - flagged responses awaiting / done filing to notes
#
# The panel is a GTK process; it must be running for anything to be visible.
# This script is the only thing the skill calls.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDERER="$REPO/latexpanel/renderer.py"
EVENTS_PY="$REPO/latexpanel/events.py"
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

# Optional Manim support: render a scene to a cached mp4, emit the :::video directive.
MANIM_ENV="$HOME/.local/share/manim-panel/env"
MEDIA_DIR="$HOME/.local/share/latex-panel/media"

manim_render() {
  local q=m scene=""
  while getopts "q:s:" o; do case $o in q) q=$OPTARG;; s) scene=$OPTARG;; *) exit 2;; esac; done
  [[ $q =~ ^[lmhp]$ ]] || { echo "bad quality $q" >&2; exit 2; }
  [[ -x "$MANIM_ENV/bin/manim" ]] || { echo "manim env missing at $MANIM_ENV" >&2; exit 1; }
  mkdir -p "$MEDIA_DIR"
  local tmp; tmp=$(mktemp -d)
  cat > "$tmp/scene.py"
  [[ -n $scene ]] || scene=$(grep -oE '^class +[A-Za-z_0-9]+\(.*Scene[A-Za-z]*\)' "$tmp/scene.py" | tail -1 | sed -E 's/class +([A-Za-z_0-9]+).*/\1/' || true)
  [[ -n $scene ]] || { echo "no Scene subclass found in input" >&2; exit 2; }
  local h; h=$( { cat "$tmp/scene.py"; echo "$scene $q"; } | sha1sum | cut -c1-12)
  local out="$MEDIA_DIR/$h.mp4"
  if [[ ! -f $out ]]; then
    local rc=0
    ( cd "$tmp" && PYTHONNOUSERSITE=1 PATH="$MANIM_ENV/bin:$PATH" "$MANIM_ENV/bin/manim" "-q$q" \
        --media_dir "$tmp/media" --disable_caching "$tmp/scene.py" "$scene" ) > "$tmp/render.log" 2>&1 || rc=$?
    local mp4; mp4=$(find "$tmp/media/videos" -maxdepth 3 -name "$scene.mp4" 2>/dev/null | head -1 || true)
    if (( rc != 0 )) || [[ -z $mp4 ]]; then
      echo "manim render FAILED (exit $rc); log tail:" >&2; tail -25 "$tmp/render.log" >&2
      rm -rf "${tmp:?}"; exit 1
    fi
    cp "$mp4" "$out"
    cp "$tmp/scene.py" "$MEDIA_DIR/$h.py"
  fi
  rm -rf "${tmp:?}"
  echo ":::video $out"
}

case "${1:-ensure}" in
  manim)
    shift; manim_render "$@"
    ;;
  ensure)
    ensure
    ;;
  write)
    ensure
    tmp=$(mktemp); cat > "$tmp"
    python3 "$RENDERER" --check < "$tmp" || true   # lint: missing files, bad blocks, dup ids
    mv "$tmp" "$WATCH_FILE"
    echo "wrote $(wc -c < "$WATCH_FILE") bytes to $WATCH_FILE"
    ;;
  edit)
    ensure >/dev/null
    exec python3 -u "$EVENTS_PY" "$@"
    ;;
  watch|events|wait|changed|reply|suggest|tick|flagged|filed|current)
    exec python3 -u "$EVENTS_PY" "$@"
    ;;
  status)
    if running; then echo "running (pid $(pgrep -f "$PAT" | head -1))"; else echo "not running"; fi
    ;;
  stop)
    pkill -f "$PAT" && echo "panel stopped" || echo "panel was not running"
    ;;
  *)
    echo "usage: panel.sh {ensure|write|edit|current|status|stop|watch|events|wait|changed|reply|suggest|tick|flagged|filed|manim [-q l|m|h] [-s Scene]}" >&2
    exit 2
    ;;
esac
