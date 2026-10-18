"""Offscreen test of the optional remote view: the panel mirrored over HTTP, clicks coming back.

    WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 python3 tests/smoke_remote.py

Temp watch file + data dir, bound to 127.0.0.1 on a free port, so the running panel is untouched.
"""
import os, sys, json, socket, struct, zlib, threading, time, tempfile, shutil, traceback
import urllib.request, urllib.error, urllib.parse
T = tempfile.mkdtemp(prefix="latex-panel-remote-")
os.makedirs(f"{T}/data")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["LATEX_PANEL_WATCH"] = f"{T}/resp.md"; os.environ["LATEX_PANEL_DATA"] = f"{T}/data"
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib
Gtk.Window = Gtk.OffscreenWindow
sys.path.insert(0, os.path.join(REPO, "latexpanel"))
import renderer, events, remote

FAILS: list[str] = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)

FIG = f"{T}/fig.png"
raw = b"".join(b"\0" + b"\xc8\x1e\x1e" * 8 for _ in range(8))
chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
open(FIG, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
SECRET = f"{T}/secret.txt"; open(SECRET, "w").write("nope")

open(os.environ["LATEX_PANEL_WATCH"], "w").write(
    f"# Card one\n\n$E=mc^2$\n\n:::approve s1 Sign off\nText.\n:::\n\n:::img {FIG}\n")

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
open(remote.REMOTE_FLAG, "w").write(json.dumps({"bind": "127.0.0.1", "port": PORT}))

win = renderer.RendererWindow()
BASE = f"http://127.0.0.1:{PORT}"


def main_get(fn):
    return remote.call_main(fn)


def req(path, data=None, cookie=None):
    r = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
                               headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode(errors="replace"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers


def last(t):
    evs = [e for e in events._read_all() if e.get("type") == t]
    return evs[-1] if evs else None


def run():
    try:
        for _ in range(50):
            if os.path.exists(remote.REMOTE_STATE):
                break
            time.sleep(0.1)
        st = json.load(open(remote.REMOTE_STATE))
        tok = remote.token()
        check(st.get("url", "").endswith(f"/?t={tok}"), f"remote.json has the URL ({st.get('url')})")

        code, _, _ = req("/frame")
        check(code == 403, f"no token -> 403 ({code})")
        code, body, hdr = req(f"/?t={tok}")
        cookie = (hdr.get("Set-Cookie") or "").split(";")[0]
        check(code == 200 and "EventSource" in body and cookie == f"lpt={tok}", "shell served, token becomes a cookie")

        code, page, _ = req("/frame", cookie=cookie)
        check(code == 200 and "window.webkit = { messageHandlers" in page, "mirrored page carries the message shim")
        check("Card one" in page and "file://" + FIG not in page, "page is the shown card, file:// rewritten")
        check(f"/file?p={urllib.parse.quote(FIG, safe='')}" in page, "image points at /file")
        code, png, _ = req(f"/file?p={urllib.parse.quote(FIG, safe='')}", cookie=cookie)
        check(code == 200 and png.startswith("�PNG"), f"referenced image served ({code})")
        code, _, _ = req(f"/file?p={urllib.parse.quote(SECRET, safe='')}", cookie=cookie)
        check(code == 404, f"unreferenced file refused ({code})")

        # SSE: first messages are the bar and the page version
        sse = urllib.request.urlopen(urllib.request.Request(BASE + "/stream", headers={"Cookie": cookie}), timeout=5)
        got = []
        while len(got) < 2:
            line = sse.readline().decode().strip()
            if line.startswith("data: "):
                got.append(json.loads(line[6:]))
        check(got[0]["t"] == "bar" and got[0]["bar"].get("pos") == 1 and got[1]["t"] == "page",
              f"stream opens with bar + page ({[g['t'] for g in got]})")

        uid, rev = main_get(lambda: (win._history[-1].uid, win._history[-1].rev))
        code, _, _ = req("/msg/claudeEvent", {"type": "approve", "id": "s1", "decision": "approve",
                                              "card": uid, "rev": rev, "cid": "c1"}, cookie)
        e = last("approve")
        check(code == 200 and e and e.get("card") == uid and e.get("id") == "s1",
              "tablet approve lands in events.jsonl with card identity")
        check(main_get(lambda: "s1" in win._history[-1].decisions), "panel records the decision")
        js = None
        t0 = time.time()
        while time.time() - t0 < 3:
            line = sse.readline().decode().strip()
            if line.startswith("data: "):
                m = json.loads(line[6:])
                if m["t"] == "js" and "IA.sent" in m["code"]:
                    js = m; break
        check(js is not None and '"c1"' in js["code"], "IA.sent acknowledgement mirrored to the tablet")

        code, _, _ = req("/msg/claudeEvent", {"type": "approve", "id": "s1", "decision": "approve",
                                              "card": uid, "rev": rev + 5, "cid": "c2"}, cookie)
        check((last("panel_error") or {}).get("text", "").startswith("ignored a stale approve"),
              "stale tablet click refused like a desktop one")

        # a second card, then navigate from the tablet
        open(os.environ["LATEX_PANEL_WATCH"], "w").write("# Card two\n\nhello\n")
        t0 = time.time()
        while time.time() - t0 < 4 and main_get(lambda: len(win._history)) < 2:
            time.sleep(0.1)
        code, page, _ = req("/frame", cookie=cookie)
        check("Card two" in page, "new card mirrored")
        req("/action", {"a": "prev"}, cookie)
        check(main_get(lambda: win._pos) == 0, "tablet ◀ moves the panel")
        code, page, _ = req("/frame", cookie=cookie)
        check("Card one" in page, "and the tablet sees card one again")
        req("/action", {"a": "flag"}, cookie)
        check(main_get(lambda: win._history[0].flagged) and last("flag"), "tablet ★ flags the card")

        # Claude -> panel reply reaches the tablet stream
        events.to_panel({"kind": "reply", "target": "s1", "text": "thanks"})
        js = None
        t0 = time.time()
        while time.time() - t0 < 3:
            line = sse.readline().decode().strip()
            if line.startswith("data: "):
                m = json.loads(line[6:])
                if m["t"] == "js" and "IA.reply" in m["code"]:
                    js = m; break
        check(js is not None and "thanks" in js["code"], "Claude reply mirrored")
        sse.close()

        os.remove(remote.REMOTE_FLAG)
        t0 = time.time()
        while time.time() - t0 < 3 and os.path.exists(remote.REMOTE_STATE):
            time.sleep(0.1)
        check(not os.path.exists(remote.REMOTE_STATE) and main_get(lambda: win._remote) is None, "remote off stops serving")
        try:
            urllib.request.urlopen(BASE + "/", timeout=2)
            check(False, "port closed after off")
        except (urllib.error.URLError, ConnectionError, OSError):
            check(True, "port closed after off")
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    finally:
        GLib.idle_add(Gtk.main_quit)


threading.Thread(target=run, daemon=True).start()
GLib.timeout_add_seconds(60, lambda: (FAILS.append("timeout"), Gtk.main_quit()))
Gtk.main()
shutil.rmtree(T, ignore_errors=True)
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
