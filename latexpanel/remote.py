"""Optional remote view of the panel: the same page served over HTTP to a tablet/phone browser.

Off unless switched on (`panel.sh remote on`, which creates REMOTE_FLAG). The GTK panel stays the
single source of truth - one history, one set of card uids, one events.jsonl - and the remote is a
second view of it:

  panel -> remote   every page the WebView loads and every IA.* call is mirrored (Server-Sent Events)
  remote -> panel   clicks post to /msg/<handler> and run the same code a WebKit message would;
                    toolbar taps post to /action

Access needs the token printed by `panel.sh remote on` (kept in DATA_DIR/remote_token, so a
bookmark keeps working); it becomes a cookie on first visit. /file only serves files the current
page references (PDF page renders, :::img, :::video).
"""

import html as H
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import sys
import threading
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gi.repository import GLib

DATA_DIR = os.environ.get("LATEX_PANEL_DATA", os.path.expanduser("~/.local/share/latex-panel"))
REMOTE_FLAG = os.path.join(DATA_DIR, "remote.on")       # exists = remote wanted; JSON {bind, port}
REMOTE_STATE = os.path.join(DATA_DIR, "remote.json")    # written while serving: url, clients
TOKEN_FILE = os.path.join(DATA_DIR, "remote_token")
DEFAULT_PORT = 8765


def token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    t = secrets.token_urlsafe(8)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


def lan_ip() -> str:
    """Address of the interface the default route uses (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def resolve_bind(bind: str) -> tuple[str, str]:
    """-> (address to bind, host to put in the URL)."""
    if bind in ("", "lan"):
        ip = lan_ip()
        return ip, ip
    if bind == "all":
        return "0.0.0.0", lan_ip()
    if bind == "tailscale":
        import subprocess
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                            timeout=5).stdout.split()[0]
        return ip, ip
    return bind, bind


# ── what the remote browser gets ────────────────────────────────────────────

# Runs first in every mirrored page: WebKit's message handlers become POSTs, and the scroll
# position to restore after an in-place edit is the tablet's own, not the desktop window's.
SHIM = r"""<meta name="viewport" content="width=device-width, initial-scale=1">
<script>
(function () {
  function handler(name) {
    return { postMessage: function (m) {
      fetch('/msg/' + name, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(m) })
      .then(function (r) { if (!r.ok) throw r.status; })
      .catch(function () { if (window.IA && IA.toast) IA.toast('Not sent - laptop unreachable'); });
    } };
  }
  window.webkit = { messageHandlers: { claudeEvent: handler('claudeEvent'), quizResult: handler('quizResult') } };
  var saved;
  Object.defineProperty(window, '_iaSaved', { configurable: true,
    get: function () { return saved; },
    set: function (v) {
      if (v && typeof v === 'object') {
        try { v.scroll = parent.LP ? parent.LP.scrollFor(v.card) : 0; } catch (e) { v.scroll = 0; }
      }
      saved = v;
    } });
})();
</script>
<style>
  @media (pointer: coarse) {
    body { padding-left: 16px; padding-right: 16px; }
    button { touch-action: manipulation; min-height: 44px; padding-left: 16px; padding-right: 16px; font-size: 16px; }
    .mcq .option { padding: 14px 16px; }
  }
</style>
"""

_FILE_URL = re.compile(r"""file://(/[^"'\s?<>]*)(?:\?([^"'\s<>]*))?""")


def to_remote(page: str) -> tuple[str, set[str]]:
    """Mirror-able copy of a WebView page, plus the local files it may fetch."""
    files: set[str] = set()

    def sub(m: re.Match) -> str:
        path = H.unescape(m.group(1))
        if path == "/tmp/":
            return m.group(0)
        files.add(path)
        q = "p=" + urllib.parse.quote(path, safe="")
        if m.group(2):
            q += "&amp;" + m.group(2)
        return "/file?" + q

    page = _FILE_URL.sub(sub, page)
    page = page.replace("<head>", "<head>\n" + SHIM, 1)
    return page, files


SHELL = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude panel</title>
<style>
  :root { --bar: #d2dde9; --ink: #131a21; --muted: #3f536a; --bg: #e7edf3; --btn: #f4f7fa; --line: #b7c2ce; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink);
               font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; }
  body { display: flex; flex-direction: column; }
  #bar { display: flex; align-items: center; gap: 6px; padding: 6px 10px; background: var(--bar);
         border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  #bar button { min-width: 48px; min-height: 48px; font-size: 20px; border: 1px solid var(--line);
                border-radius: 8px; background: var(--btn); color: var(--ink); padding: 0 12px;
                touch-action: manipulation; }
  #bar button:disabled { opacity: .35; }
  #bar button.on { background: #ffe9a8; border-color: #c9a227; }
  #bar button.warn { background: #f8d7da; border-color: #dc3545; }
  #pending { background: #fff3cd !important; border-color: #c9a227 !important; font-size: 17px !important; }
  #pos { font-size: 16px; font-weight: 600; min-width: 4em; text-align: center; }
  #time { font-size: 13px; color: var(--muted); }
  #sp { flex: 1; }
  #listen, #conn { font-size: 13px; color: var(--muted); white-space: nowrap; }
  #conn.down { color: #a3262f; font-weight: 600; }
  #frame { flex: 1; border: 0; width: 100%; background: var(--bg); }
</style>
</head><body>
<div id="bar">
  <button id="prev" aria-label="Previous card" disabled>◀</button>
  <span id="pos">—</span>
  <button id="next" aria-label="Next card" disabled>▶</button>
  <span id="time"></span>
  <button id="pending" hidden></button>
  <span id="sp"></span>
  <span id="conn">connecting…</span>
  <span id="listen"></span>
  <button id="zout" aria-label="Smaller text">A−</button>
  <button id="zin" aria-label="Larger text">A+</button>
  <button id="flag" aria-label="Flag for notes" disabled>★</button>
  <button id="notes" aria-label="File into notes now" disabled>📥</button>
  <button id="del" aria-label="Delete card" disabled>✕</button>
</div>
<iframe id="frame" title="Panel"></iframe>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var frame = $('frame'), ver = -1, lastCard = null, lastY = 0, wantPending = false;
  var zoom = 1;
  try { zoom = parseFloat(localStorage.getItem('lp-zoom')) || 1; } catch (e) {}

  function win() { try { return frame.contentWindow; } catch (e) { return null; } }
  function applyZoom() {
    var w = win(); if (w && w.document && w.document.body) w.document.body.style.zoom = zoom;
  }
  window.LP = {
    // an in-place edit of the card already on screen keeps the reader's place
    scrollFor: function (card) { return card && card === lastCard ? lastY : 0; }
  };

  function load(v) {
    if (v === ver) return;
    var w = win();
    try {
      var s = w && w._iaSaved;
      lastCard = s ? s.card : null; lastY = w ? w.scrollY : 0;
    } catch (e) { lastCard = null; lastY = 0; }
    ver = v;
    frame.src = '/frame?v=' + v;
  }
  frame.addEventListener('load', function () {
    applyZoom();
    if (wantPending) {
      wantPending = false;
      setTimeout(function () { var w = win(); if (w && w.IA) w.IA.scrollToPending(); }, 600);
    }
  });

  function bar(t) {
    $('pos').textContent = t.has ? t.pos + ' / ' + t.n : '—';
    $('time').textContent = t.time || '';
    $('prev').disabled = !t.can_prev; $('next').disabled = !t.can_next;
    ['flag', 'notes', 'del'].forEach(function (id) { $(id).disabled = !t.has; });
    $('flag').classList.toggle('on', !!t.flagged);
    var p = $('pending');
    if (t.pending) { p.hidden = false; p.textContent = '⏳ ' + t.pending + ' awaiting you'; } else p.hidden = true;
    $('listen').textContent = t.listening ? '● Claude listening' : '○ not listening';
    $('listen').style.color = t.listening ? '#2e7d4f' : '';
  }

  function act(a) {
    return fetch('/action', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ a: a }) })
      .then(function (r) { return r.json(); });
  }
  $('prev').onclick = function () { act('prev'); };
  $('next').onclick = function () { act('next'); };
  $('flag').onclick = function () { act('flag'); };
  $('notes').onclick = function () { act('notes'); };
  $('pending').onclick = function () {
    act('pending').then(function (r) {
      if (r.moved) wantPending = true;
      else { var w = win(); if (w && w.IA) w.IA.scrollToPending(); }
    });
  };
  var delArmed = null;
  $('del').onclick = function () {
    var b = $('del');
    if (delArmed) { clearTimeout(delArmed); delArmed = null; b.classList.remove('warn'); b.textContent = '✕'; act('delete'); return; }
    b.classList.add('warn'); b.textContent = 'Delete?';
    delArmed = setTimeout(function () { delArmed = null; b.classList.remove('warn'); b.textContent = '✕'; }, 3000);
  };
  function setZoom(z) {
    zoom = Math.max(0.6, Math.min(2.2, Math.round(z * 10) / 10));
    try { localStorage.setItem('lp-zoom', zoom); } catch (e) {}
    applyZoom();
  }
  $('zin').onclick = function () { setZoom(zoom + 0.1); };
  $('zout').onclick = function () { setZoom(zoom - 0.1); };

  var es = new EventSource('/stream');
  es.onopen = function () { $('conn').textContent = ''; $('conn').classList.remove('down'); };
  es.onerror = function () { $('conn').textContent = 'laptop unreachable - retrying'; $('conn').classList.add('down'); };
  es.onmessage = function (e) {
    var m = JSON.parse(e.data);
    if (m.t === 'page') load(m.v);
    else if (m.t === 'bar') bar(m.bar);
    else if (m.t === 'js') {
      var w = win();
      if (w && m.v === ver) { try { w.eval(m.code); } catch (err) {} }
    }
  };
})();
</script>
</body></html>
"""


# ── server ──────────────────────────────────────────────────────────────────

def call_main(fn, *args, timeout: float = 10.0):
    """Run fn on the GTK main loop and wait for its result (handler threads must not touch GTK)."""
    done, box = threading.Event(), {}

    def run():
        try:
            box["v"] = fn(*args)
        except Exception as e:      # reported to the HTTP client as a 500
            box["e"] = e
        done.set()
        return False

    GLib.idle_add(run)
    if not done.wait(timeout):
        raise TimeoutError("panel main loop busy")
    if "e" in box:
        raise box["e"]
    return box.get("v")


class RemoteServer:
    """Mirror of one RendererWindow. All public methods are called on the GTK main thread."""

    def __init__(self, window, bind: str = "lan", port: int = DEFAULT_PORT):
        self.win = window
        self.token = token()
        self.addr, self.host = resolve_bind(bind)
        self.port = port
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._page = "<!DOCTYPE html><html><head></head><body></body></html>"
        self._files: set[str] = set()
        self._ver = 0
        self._bar: dict = {}
        self._stopped = False
        self.httpd = ThreadingHTTPServer((self.addr, port), self._handler())
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?t={self.token}"

    def start(self):
        self._thread.start()
        self._write_state()

    def stop(self):
        self._stopped = True
        with self._lock:
            for q in self._clients:
                q.put(None)
            self._clients.clear()
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        self.httpd.server_close()
        try:
            os.remove(REMOTE_STATE)
        except OSError:
            pass

    # panel -> remote
    def set_page(self, page: str):
        page, files = to_remote(page)
        with self._lock:
            self._page, self._files = page, files
            self._ver += 1
            self._send({"t": "page", "v": self._ver})

    def js(self, code: str):
        with self._lock:
            self._send({"t": "js", "v": self._ver, "code": code})

    def set_bar(self, bar: dict):
        with self._lock:
            if bar == self._bar:
                return
            self._bar = bar
            self._send({"t": "bar", "bar": bar})

    def _send(self, msg: dict):           # caller holds the lock
        for q in self._clients:
            q.put(msg)

    def _write_state(self):
        if self._stopped:        # a client's disconnect can be queued after stop()
            return
        try:
            tmp = REMOTE_STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"url": self.url, "bind": self.addr, "port": self.port, "pid": os.getpid(),
                           "clients": len(self._clients)}, f)
            os.replace(tmp, REMOTE_STATE)
        except OSError as e:
            print(f"remote state write failed: {e}", file=sys.stderr)

    # remote -> panel
    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _authed(self, qs) -> bool:
                if qs.get("t", [None])[0] == srv.token:
                    return True
                c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                return "lpt" in c and secrets.compare_digest(c["lpt"].value, srv.token)

            def _reply(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                qs = urllib.parse.parse_qs(u.query)
                if not self._authed(qs):
                    return self._reply(403, b"latex-panel remote: open the URL printed by `panel.sh remote url`\n")
                if u.path == "/":
                    extra = []
                    if "t" in qs:
                        extra.append(("Set-Cookie", f"lpt={srv.token}; Path=/; Max-Age=31536000; SameSite=Strict; HttpOnly"))
                    return self._reply(200, SHELL.encode(), "text/html; charset=utf-8", extra)
                if u.path == "/frame":
                    with srv._lock:
                        page = srv._page
                    return self._reply(200, page.encode(), "text/html; charset=utf-8")
                if u.path == "/stream":
                    return self._stream()
                if u.path == "/file":
                    return self._file(qs.get("p", [""])[0])
                return self._reply(404, b"not found\n")

            do_HEAD = do_GET

            def do_POST(self):
                u = urllib.parse.urlsplit(self.path)
                if not self._authed(urllib.parse.parse_qs(u.query)):
                    return self._reply(403, b"forbidden\n")
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._reply(400, b"bad json\n")
                try:
                    if u.path in ("/msg/claudeEvent", "/msg/quizResult"):
                        call_main(srv.win._remote_message, u.path[5:], data)
                        return self._reply(200, b'{"ok":true}', "application/json")
                    if u.path == "/action":
                        out = call_main(srv.win._remote_action, str(data.get("a")))
                        return self._reply(200, json.dumps(out or {}).encode(), "application/json")
                except Exception as e:
                    print(f"remote {u.path} failed: {e}", file=sys.stderr)
                    return self._reply(500, str(e).encode())
                return self._reply(404, b"not found\n")

            def _stream(self):
                q: queue.Queue = queue.Queue()
                with srv._lock:
                    srv._clients.append(q)
                    q.put({"t": "bar", "bar": srv._bar})
                    q.put({"t": "page", "v": srv._ver})
                GLib.idle_add(lambda: (srv._write_state(), False)[1])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        try:
                            msg = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if msg is None:
                            break
                        self.wfile.write(b"data: " + json.dumps(msg).encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with srv._lock:
                        if q in srv._clients:
                            srv._clients.remove(q)
                    GLib.idle_add(lambda: (srv._write_state(), False)[1])
                    self.close_connection = True

            def _file(self, path: str):
                with srv._lock:
                    ok = path in srv._files
                if not ok or not os.path.isfile(path):
                    return self._reply(404, b"not on the current card\n")
                size = os.path.getsize(path)
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                start, end = 0, size - 1
                rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
                if rng and (rng.group(1) or rng.group(2)):
                    if rng.group(1):
                        start = int(rng.group(1))
                        end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                    else:
                        start = max(0, size - int(rng.group(2)))
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

        return Handler
