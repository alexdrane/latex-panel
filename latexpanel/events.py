#!/usr/bin/env python3
"""Panel -> Claude event channel.

The panel appends one JSON object per user action (approve, pick, pin, comment...)
to EVENTS. A Claude session reads them back with this CLI, normally by arming the
Monitor tool on `panel.sh watch` so every click arrives as a notification.

    events.py watch                 stream new events, one readable line each (for Monitor)
    events.py events [--all]        print unread events and mark them read
    events.py wait [--id ID] [--timeout S]   block until an event (for block ID) arrives
    events.py changed OLD.pdf NEW.pdf        pages whose rendering differs

Claude -> panel (live, no new history entry):
    events.py reply TARGET TEXT...          TARGET = block id or #seq of the event answered
    events.py suggest SEQ [--note N] < new  in-place rewrite proposal for a `rewrite` event
    events.py tick ID ITEM [todo|doing|done]   update a :::checklist item (1-based or text)
    events.py current [--latest|--entry N]  copy a card's markdown to edit.md for in-place editing
    events.py edit [--latest|--shown|--entry N] [FILE]   replace that card in place (stdin, FILE or edit.md)
    events.py flagged [--all]               flagged/filed-to-notes responses not yet filed
    events.py filed FILE...                 mark flagged files as consolidated into notes
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

DATA_DIR   = os.environ.get("LATEX_PANEL_DATA", os.path.expanduser("~/.local/share/latex-panel"))
EVENTS     = os.path.join(DATA_DIR, "events.jsonl")
CURSOR     = os.path.join(DATA_DIR, "events.cursor")
WATCH_PID  = os.path.join(DATA_DIR, "watcher.pid")
TO_PANEL   = os.path.join(DATA_DIR, "to_panel.jsonl")
STATE      = os.path.join(DATA_DIR, "state.json")
EDIT_FILE  = os.path.join(DATA_DIR, "edit.md")
EDIT_UID   = os.path.join(DATA_DIR, "edit.uid")
FLAGGED    = os.path.join(DATA_DIR, "flagged")
FILED      = os.path.join(FLAGGED, "filed")


# ── writing (panel side) ─────────────────────────────────────────────────────

def _last_seq() -> int:
    try:
        with open(EVENTS, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                return int(json.loads(line)["seq"])
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return 0


def append(ev: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    ev = {"seq": _last_seq() + 1, **ev}
    with open(EVENTS, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def to_panel(msg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TO_PANEL, "a") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def seen_cursor() -> int:
    return _cursor()


def watcher_alive() -> bool:
    try:
        with open(WATCH_PID) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


# ── reading (Claude side) ────────────────────────────────────────────────────

def _read_all() -> list[dict]:
    out = []
    try:
        with open(EVENTS) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return out


def _cursor() -> int:
    try:
        with open(CURSOR) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _set_cursor(seq: int) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if seq > _cursor():
        with open(CURSOR, "w") as f:
            f.write(str(seq))


def unread() -> list[dict]:
    c = _cursor()
    return [e for e in _read_all() if e.get("seq", 0) > c]


def _q(s, n: int = 400) -> str:
    s = " ".join(str(s or "").split())
    return json.dumps(s if len(s) <= n else s[: n - 1] + "…", ensure_ascii=False)


def fmt(e: dict) -> str:
    t = e.get("type", "?")
    bid = f"[{e['id']}] " if e.get("id") else ""
    title = f"{_q(e['title'], 80)} " if e.get("title") else ""
    note = f" — note: {_q(e['note'])}" if e.get("note") else ""
    where = f"entry {e.get('entry', '?')}/{e.get('entries', '?')}"
    if e.get("stale"):
        where += f", OLDER response ({_q(e.get('entry_title'), 60)})"
    head = f"panel #{e.get('seq')} ({where}) {t}: "

    if t in ("approve", "diff"):
        word = {"approve": "APPROVED", "changes": "CHANGES REQUESTED",
                "reject": "REJECTED"}.get(e.get("decision"), str(e.get("decision")).upper())
        body = f"{bid}{title}→ {word}{note}"
    elif t == "choice":
        picked = e.get("picked") or []
        if picked:
            texts = e.get("picked_text") or []
            parts = [f"{p} ({_q(x, 120)})" if x else p for p, x in zip(picked, texts + [""] * len(picked))]
            body = f"{bid}{title}→ picked " + ", ".join(parts) + note
        else:
            body = f"{bid}{title}→ NONE of these{note}"
    elif t == "ask":
        body = f"{bid}{title}→ answer: {_q(e.get('answer'), 2000)}"
    elif t == "params":
        body = f"{bid}{title}→ values {json.dumps(e.get('values', {}), ensure_ascii=False)}{note}"
    elif t == "pin":
        body = (f"{bid}{title}pin #{e.get('n')} on page {e.get('page')} of {e.get('side')} "
                f"({e.get('path')}) at x={e.get('x')}, y={e.get('y')} (fractions from top-left): "
                f"{_q(e.get('comment'), 2000)}")
    elif t == "selection":
        ctx = f" in {_q(e.get('context'), 80)}" if e.get("context") else ""
        body = f"{bid}selected{ctx}: «{_q(e.get('text'), 1200)[1:-1]}» — {_q(e.get('comment'), 2000)}"
    elif t == "message":
        body = _q(e.get("text"), 2000)
    elif t == "mcq":
        body = f"{_q(e.get('question'), 160)} → {_q(e.get('selected'), 120)} ({'correct' if e.get('correct') else 'WRONG'})"
    elif t == "rewrite":
        ctx = f" in {_q(e.get('context'), 80)}" if e.get("context") else ""
        body = (f"¶{e.get('para')}{ctx}: rewrite {_q(e.get('instruction'), 600)}\n"
                f"    original: {_q(e.get('text'), 2000)}\n"
                f"    → reply with: panel.sh suggest {e.get('seq')} <<'EOF' … EOF")
    elif t == "check":
        body = f"{bid}{title}item {e.get('item')} {_q(e.get('text'), 160)} → {e.get('state')}"
    elif t == "check_add":
        body = f"{bid}{title}asks to ADD item {_q(e.get('text'), 400)}"
    elif t in ("flag", "to_notes"):
        what = "flagged for later" if t == "flag" else "FILE THIS TO OBSIDIAN NOTES NOW"
        body = f"{_q(e.get('entry_title'), 80)} {what}: {e.get('file')}"
    elif t == "quiz_done":
        body = f"deck finished {e.get('score')}/{e.get('n')}"
    else:
        body = json.dumps({k: v for k, v in e.items() if k not in ("seq", "ts")}, ensure_ascii=False)
    return head + body


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_events(a) -> int:
    evs = _read_all() if a.all else unread()
    for e in evs:
        print(fmt(e))
    if not evs:
        print("no unread panel events")
    elif not a.all:
        _set_cursor(evs[-1]["seq"])
    return 0


def cmd_watch(_a) -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WATCH_PID, "w") as f:
        f.write(str(os.getpid()))

    def bye(*_):
        try:
            with open(WATCH_PID) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(WATCH_PID)
        except OSError:
            pass
        sys.exit(0)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, bye)
    try:
        backlog = unread()
        for e in backlog:
            print("(queued) " + fmt(e), flush=True)
        if backlog:
            _set_cursor(backlog[-1]["seq"])
        while True:
            evs = unread()
            for e in evs:
                print(fmt(e), flush=True)
            if evs:
                _set_cursor(evs[-1]["seq"])
            time.sleep(0.4)
    finally:
        bye()


def cmd_wait(a) -> int:
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        evs = unread()
        hit = [e for e in evs if not a.id or e.get("id") == a.id]
        if hit:
            for e in evs:
                print(fmt(e))
            _set_cursor(evs[-1]["seq"])
            return 0
        if deadline and time.time() > deadline:
            print("timeout: no panel event")
            return 3
        time.sleep(0.4)


def cmd_changed(a) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import interactive
    pages = interactive.changed_pages(os.path.abspath(os.path.expanduser(a.old)),
                                      os.path.abspath(os.path.expanduser(a.new)))
    print(",".join(map(str, pages)) if pages else "no visual differences")
    return 0


def cmd_reply(a) -> int:
    to_panel({"kind": "reply", "target": a.target, "text": " ".join(a.text)})
    print(f"reply queued for {a.target}")
    return 0


def cmd_suggest(a) -> int:
    new = sys.stdin.read().strip()
    if not new:
        print("suggest: replacement text on stdin is empty", file=sys.stderr)
        return 2
    to_panel({"kind": "suggest", "seq": int(a.seq.lstrip("#")), "text": new, "note": a.note or ""})
    print(f"suggestion queued for event #{a.seq.lstrip('#')}")
    return 0


def cmd_tick(a) -> int:
    to_panel({"kind": "tick", "id": a.id, "item": a.item, "state": a.state})
    print(f"{a.id}: item {a.item} → {a.state}")
    return 0


def _state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"shown_uid": None, "entries": []}


def _pick(st: dict, a) -> dict | None:
    es = st["entries"]
    if not es:
        return None
    if getattr(a, "entry", None):
        return next((e for e in es if e["index"] == a.entry), None)
    if getattr(a, "latest", False):
        return es[-1]
    return next((e for e in es if e["uid"] == st["shown_uid"]), es[-1])


def cmd_current(a) -> int:
    st = _state()
    e = _pick(st, a)
    if e is None:
        print("panel has no cards (not running, or nothing written yet)")
        return 1
    with open(EDIT_FILE, "w") as f:
        f.write(e["text"])
    with open(EDIT_UID, "w") as f:
        f.write(str(e["uid"]))
    n = len(st["entries"])
    shown = " (shown now)" if e["uid"] == st["shown_uid"] else ""
    latest = " (latest)" if e is st["entries"][-1] else ""
    edited = f", edited {e['edited']}" if e.get("edited") else ""
    print(f"card {e['index']}/{n}{shown}{latest} {_q(e['title'], 70)} written {e['time']}{edited}")
    print(f"markdown copied to {EDIT_FILE}: Read it, change it with the Edit tool, then `panel.sh edit`")
    return 0


def cmd_edit(a) -> int:
    text, from_checkout = None, False
    if a.file:
        text = open(a.file).read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text or not text.strip():
        try:
            text = open(EDIT_FILE).read()
            from_checkout = True
        except OSError:
            print("edit: nothing on stdin and no edit.md - run `panel.sh current` first", file=sys.stderr)
            return 2
    if not text.strip():
        print("edit: refusing to replace a card with empty markdown", file=sys.stderr)
        return 2
    if a.entry:
        e = _pick(_state(), a)
        if e is None:
            print(f"edit: no card {a.entry}", file=sys.stderr)
            return 2
        uid = e["uid"]
    elif a.latest:
        uid = "latest"
    elif a.shown:
        uid = "shown"
    elif from_checkout:
        try:
            uid = int(open(EDIT_UID).read().strip())
        except (OSError, ValueError):
            uid = "latest"
    else:
        uid = "latest"
    renderer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderer.py")
    import subprocess
    lint = subprocess.run([sys.executable, renderer, "--check"], input=text, capture_output=True, text=True)
    sys.stdout.write(lint.stdout)
    to_panel({"kind": "edit", "uid": uid, "text": text})
    where = {"latest": "the latest card", "shown": "the card on screen"}.get(uid, f"card uid {uid}")
    print(f"edit queued for {where} (updates in place, keeps his decisions on unchanged blocks)")
    return 0


def _flag_title(path: str) -> str:
    try:
        for line in open(path):
            if line.startswith("#"):
                return line.lstrip("# ").strip()
    except OSError:
        pass
    return ""


def cmd_flagged(a) -> int:
    os.makedirs(FILED, exist_ok=True)
    dirs = [FLAGGED] + ([FILED] if a.all else [])
    rows = []
    for d in dirs:
        for f in sorted(os.listdir(d)):
            if f.endswith(".md"):
                p = os.path.join(d, f)
                rows.append(f"{'filed   ' if d == FILED else 'pending '}{p}  {_q(_flag_title(p), 70)}")
    print("\n".join(rows) if rows else "nothing flagged")
    return 0


def cmd_filed(a) -> int:
    os.makedirs(FILED, exist_ok=True)
    for f in a.files:
        src = f if os.path.isabs(f) else os.path.join(FLAGGED, f)
        os.replace(src, os.path.join(FILED, os.path.basename(src)))
        print(f"filed {os.path.basename(src)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("events"); s.add_argument("--all", action="store_true"); s.set_defaults(f=cmd_events)
    s = sub.add_parser("watch"); s.set_defaults(f=cmd_watch)
    s = sub.add_parser("wait"); s.add_argument("--id"); s.add_argument("--timeout", type=float, default=0); s.set_defaults(f=cmd_wait)
    s = sub.add_parser("changed"); s.add_argument("old"); s.add_argument("new"); s.set_defaults(f=cmd_changed)
    s = sub.add_parser("reply"); s.add_argument("target"); s.add_argument("text", nargs="+"); s.set_defaults(f=cmd_reply)
    s = sub.add_parser("suggest"); s.add_argument("seq"); s.add_argument("--note"); s.set_defaults(f=cmd_suggest)
    s = sub.add_parser("tick"); s.add_argument("id"); s.add_argument("item")
    s.add_argument("state", nargs="?", default="done", choices=["todo", "doing", "done"]); s.set_defaults(f=cmd_tick)
    s = sub.add_parser("current"); g = s.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true"); g.add_argument("--entry", type=int); s.set_defaults(f=cmd_current)
    s = sub.add_parser("edit"); g = s.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true"); g.add_argument("--shown", action="store_true")
    g.add_argument("--entry", type=int); s.add_argument("file", nargs="?"); s.set_defaults(f=cmd_edit)
    s = sub.add_parser("flagged"); s.add_argument("--all", action="store_true"); s.set_defaults(f=cmd_flagged)
    s = sub.add_parser("filed"); s.add_argument("files", nargs="+"); s.set_defaults(f=cmd_filed)
    a = p.parse_args()
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
