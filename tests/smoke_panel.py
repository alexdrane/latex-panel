"""Offscreen smoke test of the real WebKitGTK panel: click -> event, Claude -> panel updates.

    WEBKIT_DISABLE_COMPOSITING_MODE=1 python3 tests/smoke_panel.py

Uses a temp watch file + data dir (LATEX_PANEL_WATCH / LATEX_PANEL_DATA), so the
running panel and your real event log are untouched. Prints what it observed.
"""
import os, sys, json, time
import tempfile, shutil, subprocess
T = tempfile.mkdtemp(prefix="latex-panel-smoke-")
os.makedirs(f"{T}/data")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["LATEX_PANEL_WATCH"] = f"{T}/resp.md"; os.environ["LATEX_PANEL_DATA"] = f"{T}/data"
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib
Gtk.Window = Gtk.OffscreenWindow          # render without putting a window on screen
sys.path.insert(0, os.path.join(REPO, "latexpanel"))
import renderer, events
DEMO = """# Smoke

We find that repartitioning reduces the variance of $\\ln Z$ by a factor $\\sim 3.4$ across all runs.

:::checklist plan Plan
- [x] one
- [~] two
- [ ] update §4
:::

:::approve sec5 Sign off §5
Short text.
:::
"""
open(f"{T}/resp.md", "w").write(DEMO)
win = renderer.RendererWindow()
log = lambda *a: print(*a, flush=True)
def evs(): return [json.loads(l) for l in open(events.EVENTS)] if os.path.exists(events.EVENTS) else []
steps = [
 (4000, lambda: win._js("document.querySelector('.ia[data-ia-id=sec5] [data-act=approve]').click()")),
 (1500, lambda: log("after approve:", [(e['seq'], e['type'], e.get('id'), e.get('entry')) for e in evs()],
                    "pending btn:", win._btn_pending.get_visible() and win._btn_pending.get_label())),
 (100,  lambda: win._js("(function(){var el=document.querySelector('[data-para=\"1\"]');IA.send({type:'rewrite',para:1,text:IA.fragText(el).trim(),instruction:'shorter',context:''},null)})()")),
 (1500, lambda: (events.to_panel({"kind":"suggest","seq":evs()[-1]["seq"],"text":"Repartitioning cuts $\\mathrm{Var}[\\ln Z]$ by $\\sim 3.4\\times$.","note":"tightened"}),
                 events.to_panel({"kind":"tick","id":"plan","item":"§4","state":"done"}),
                 events.to_panel({"kind":"reply","target":"sec5","text":"Applying §5 now."}),
                 events._set_cursor(evs()[-1]["seq"]))),
 (2500, lambda: win._js("IA.send({type:'message', text: JSON.stringify({rw: !!document.querySelector('.ia[data-ia-id^=rw-]'), "
                        "ck: document.querySelector('.ia-checklist .ia-status').textContent, reply: (document.querySelector('.ia[data-ia-id=sec5] .ia-reply')||{}).textContent, "
                        "sec5: document.querySelector('.ia[data-ia-id=sec5] .ia-status').textContent})}, null)")),
 (1500, lambda: log("dom state:", evs()[-1]["text"], "| pending btn:", win._btn_pending.get_visible() and win._btn_pending.get_label())),
 (100,  lambda: (win._on_notes(None), log("notes ev:", evs()[-1]["type"], os.path.basename(evs()[-1]["file"])))),
 (100,  lambda: (open(f"{T}/resp.md", "w").write("# new entry\n\nplain\n"))),
 (1500, lambda: (log("entries:", len(win._history), "pending:", win._pending_entries()), win._on_pending(None))),
 (2500, lambda: log("pos after jump:", win._pos)),
 # ── in-place edit: card 1 is shown; revise sec5's text, keep the checklist unchanged
 (100,  lambda: win._js("window.scrollTo(0, 120)")),
 (300,  lambda: subprocess.run([sys.executable, os.path.join(REPO, "latexpanel/events.py"), "current", "--entry", "1"],
                               env=os.environ, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)),
 (300,  lambda: ((lambda t: open(events.EDIT_FILE, "w").write(t.replace("Short text.", "Shorter, revised text.")))(open(events.EDIT_FILE).read()),
                 subprocess.run([sys.executable, os.path.join(REPO, "latexpanel/events.py"), "edit"], env=os.environ,
                                check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=20))),
 (1500, lambda: None),
 (300, lambda: win._js("(function(){try{IA.send({type:'message', text: JSON.stringify({y: window.scrollY, "
                        "sec5: document.querySelector('.ia[data-ia-id=sec5] .ia-status').textContent, "
                        "rev: (document.querySelector('.ia[data-ia-id=sec5] .ia-revnote')||{}).textContent, "
                        "body: document.querySelector('.ia[data-ia-id=sec5] .ia-body').textContent.trim(), "
                        "ck: document.querySelector('.ia-checklist .ia-status').textContent})}, null)}catch(e){IA.send({type:'message',text:'ERR '+e},null)}})()")),
 (1500, lambda: log("after edit: cards", len(win._history), "| card1 versions", len(win._history[0].versions),
                    "| dom", evs()[-1].get("text", evs()[-2:]), "| time label", win._lbl_time.get_label())),
 (100,  lambda: (shutil.rmtree(T), Gtk.main_quit())),
]
t = 0
for dt, fn in steps:
    t += dt
    GLib.timeout_add(t, lambda fn=fn: (fn(), False)[1])
GLib.timeout_add(90000, Gtk.main_quit)
Gtk.main()
