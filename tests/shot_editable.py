"""Offscreen screenshot of an editable proposal: python3 tests/shot_editable.py OUT.png [step]
step 0 = as proposed, 1 = edited, 2 = sent for review (default 1). Temp data dir; the live panel is untouched."""
import os, sys, json, tempfile
T = tempfile.mkdtemp(prefix="latex-panel-shot-"); os.makedirs(f"{T}/data")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["LATEX_PANEL_WATCH"] = f"{T}/resp.md"; os.environ["LATEX_PANEL_DATA"] = f"{T}/data"
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib
Gtk.Window = Gtk.OffscreenWindow
sys.path.insert(0, os.path.join(REPO, "latexpanel"))
import renderer
out = sys.argv[1]; step = int(sys.argv[2]) if len(sys.argv) > 2 else 1
OLD = r"\writeme{\#18/\#30 Define mode evaporation (mechanism, implications, prior solutions); cite the materials-science extinction literature and Buchner 2026; motivate why the central peak is missed.}"
NEW = ("With a finite number of live points, a mode can lose all of its live points by chance, after which nested sampling cannot recover it. "
       "We call this mode evaporation; the materials-science literature calls it extinction \\citep{Partay2021materials}, and \\citet{Buchner2026} describes it as mode collapse. "
       "Each iteration replaces the lowest-likelihood point with a new point drawn in a mode in proportion to its live-point occupancy, so the occupancy of a narrow mode performs a random walk that can reach zero \\citep{Buchner2026}.")
open(f"{T}/resp.md", "w").write(f"# Shot\n\n:::diff modeevap tex #18/#30 mode evaporation\n{OLD}\n===\n{NEW}\n:::\n")
win = renderer.RendererWindow(); win.set_default_size(940, 640)
def js(code): win._webview.evaluate_javascript(code, -1, None, None, None, None, None)
def prep():
    js("document.querySelector('.ia-tabs button[data-tab=\"1\"]').click()")
    if step >= 1:
        js("var ed=document.querySelector('.ia-ed');var ta=ed.querySelector('textarea');"
           "ta.value=ta.value.replace('lose all of its live points by chance','lose every one of its live points by chance').replace('We call this','We refer to this');"
           "ta.dispatchEvent(new Event('input',{bubbles:true}))")
    if step >= 2:
        js("document.querySelector('[data-act=ed-send]').click()")
    return False
def snap():
    pb = win.get_pixbuf(); pb.savev(out, "png", [], []); Gtk.main_quit(); return False
GLib.timeout_add(3500, prep); GLib.timeout_add(5500, snap); Gtk.main()
