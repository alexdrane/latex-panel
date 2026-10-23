"""Offscreen acceptance test of editable proposals: edit a :::diff / :::choice wording, Send it for review,
Claude's opinion / alternative, Approve my version, the typing guard, and the watch/show-event output.

    WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 python3 tests/smoke_editable.py

Temp watch file + data dir (LATEX_PANEL_WATCH / LATEX_PANEL_DATA): the running panel and its event log are untouched.
"""
import os, sys, json, tempfile, shutil, subprocess, traceback
T = tempfile.mkdtemp(prefix="latex-panel-editable-")
os.makedirs(f"{T}/data")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["LATEX_PANEL_WATCH"] = f"{T}/resp.md"; os.environ["LATEX_PANEL_DATA"] = f"{T}/data"
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib, WebKit2
Gtk.Window = Gtk.OffscreenWindow
sys.path.insert(0, os.path.join(REPO, "latexpanel"))
import renderer, events

FAILS: list[str] = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)

OLD = r"We use \citet{Handley2015} to estimate $\Delta t$ for each run."
NEW = r"Following \citet{Handley2015}, we estimate $\Delta t$ for every run."
EDIT = ("Following \\citet{Handley2015}, we estimate $\\Delta t$ and $\\ln Z$ for every run.\n"
        "A second line with a backslash-n look-alike: \\nu and \\newline, plus " + "a long tail of words " * 40 + "END")
CARD = f"""# Editable

:::diff p1 Tighten the method sentence
{OLD}
===
{NEW}
:::

:::choice c1 Pick a wording
@@ A
Option A: $\\Delta t$ is small.
@@ B
Option B: \\citet{{X}} shows $\\Delta t$ is small.
:::

:::approve other Unrelated
Nothing.
:::
"""
NEWCARD = CARD.replace(NEW, "A completely different proposal for the same block.")

def evs():
    return [json.loads(l) for l in open(events.EVENTS)] if os.path.exists(events.EVENTS) else []
def last(t):
    return next((e for e in reversed(evs()) if e.get("type") == t), None)
def cli(*args, stdin=""):
    return subprocess.run([sys.executable, os.path.join(REPO, "latexpanel/events.py"), *args], env=os.environ,
                          input=stdin, capture_output=True, text=True, timeout=20)
def click(sel):
    return f"(function(){{var b=document.querySelector({json.dumps(sel)}); if(!b) return 'missing'; b.click(); return 'ok'}})()"
def typ(bid, text, opt=None):
    return (f"(function(){{var ed=IA.edFind({json.dumps(bid)},{json.dumps(opt)}); var ta=ed.querySelector('textarea');"
            f"ta.value={json.dumps(text)}; ta.dispatchEvent(new Event('input',{{bubbles:true}})); return 'ok'}})()")
def info(bid, opt=None):
    return (f"(function(){{var ed=IA.edFind({json.dumps(bid)},{json.dumps(opt)}); var b=ed.closest('.ia');"
            "var now=ed.querySelector('.ia-edsteps .now'); var ap=IA.own(b,'[data-act=approve]');"
            "return {value: ed.querySelector('textarea').value, now: now?now.dataset.s:0, send: !ed.querySelector('[data-act=ed-send]').disabled,"
            "approveDisabled: ap?ap.disabled:null, approveText: ap?ap.textContent:null, ins: ed.querySelectorAll('.ia-edlive ins').length,"
            "alt: !ed.querySelector('.ia-edalt').hidden, edited: ed.hasAttribute('data-edited')}})()")


def scenario(win):
    entry = lambda: win._history[0]
    yield 4000
    check((yield "window.__errs.length") == 0, "page loads without JS errors")

    # 1. the New text is a plain textarea holding the exact source
    i = yield info("p1")
    check(i["value"].strip() == NEW and i["now"] == 0 and not i["send"], f"New tab is an editor holding the exact proposal {i['value'][:50]!r}")
    check((yield "!!document.querySelector('.ia[data-ia-id=p1] .ia-ed textarea[data-ia-edit]')") is True, "editor is a textarea")
    check(i["approveText"] == "✓ Approve" and i["approveDisabled"] is False, "unedited proposal: Approve unchanged")

    # 2. typing updates the live diff, enables Send, gates Approve; draft saved per keystroke
    yield typ("p1", EDIT)
    i = yield info("p1")
    check(i["now"] == "1" and i["send"] and i["edited"] and i["ins"] >= 1, f"edit: step 'edited', Send enabled, live diff has insertions {i['now']}/{i['ins']}")
    check(i["approveDisabled"] is True and i["approveText"] == "✓ Approve my version", "Approve my version is locked until Claude replies")
    changes = yield "document.querySelector('.ia[data-ia-id=p1] .ia-pane').textContent"
    check("\\citet{Handley2015}" in changes and "$\\Delta t$" in changes, "Changes tab is plain text: LaTeX not rendered")
    yield 700
    d = json.load(open(events.DRAFTS)) if os.path.exists(events.DRAFTS) else {}
    dk = [k for v in d.values() for k in v]
    check("e:p1" in dk and any(v.get("e:p1", {}).get("text") == EDIT for v in d.values()), f"draft saved per keystroke ({dk})")

    # 3. typing guard: edit of the card refused while the edited text is unsent
    r = cli("edit", "--latest", stdin=CARD + "\nExtra.\n")
    check(r.returncode == 3 and "REFUSED" in r.stderr, f"panel.sh edit refused with unsent edited text ({r.returncode})")
    events.to_panel({"kind": "edit", "uid": "latest", "text": CARD + "\nExtra.\n"})
    yield 800
    check("Extra." not in entry().text and "edit refused" in (last("panel_error") or {}).get("text", ""), "panel itself refuses the edit too")

    # 4. approving unsent edited wording is refused (server backstop for the page gate)
    yield (f"IA.send({{type:'diff',id:'p1',title:'t',decision:'approve',note:'',edited_text:{json.dumps(EDIT)},proposal_original:{json.dumps(NEW)}}},null)")
    yield 800
    check("p1" not in entry().decisions and "hasn't been sent" in (last("panel_error") or {}).get("text", ""), "approve of unsent edit ignored by the panel")
    yield 2200   # panel reloads after a refusal; the unsent text must come back from the draft store
    i = yield info("p1")
    check(i["value"] == EDIT and i["now"] == "1", "unsent edit restored after the page was re-rendered")

    # 5. Send my version for review -> diff_edit event with the FULL text
    check((yield click(".ia[data-ia-id=p1] [data-act=ed-send]")) == "ok", "Send button clicked")
    yield 900
    e = last("diff_edit")
    check(e is not None, "diff_edit event emitted")
    check(e and e["id"] == "p1" and e["card"] == entry().uid and e["rev"] == 1, "event carries card uid, revision, block id")
    check(e and e["edited"] == EDIT and len(e["edited"]) > 900, f"edited text is complete and exact ({len(e['edited']) if e else 0} chars)")
    check(e and e["original"] == NEW, "original proposed text carried")
    i = yield info("p1")
    check(i["now"] == "2" and not i["send"] and i["approveDisabled"] is True, "state: sent for review, Approve still locked")
    yield 600
    d = json.load(open(events.DRAFTS)) if os.path.exists(events.DRAFTS) else {}
    check(not any(k.startswith("e:") for v in d.values() for k in v), "draft cleared once the edit is sent")
    check("p1" not in entry().decisions, "sending is not a decision")

    # 6. watch / events / show-event output
    line = next(l for l in cli("events", "--all").stdout.splitlines() if "diff_edit" in l)
    check("\n" not in line and json.dumps(EDIT, ensure_ascii=False) in line and "…" not in line.split("original:")[0],
          "watch line has the FULL edited text, escaped, on one line")
    check("\\\\nu" in line and "\\n" in line, "newline escaped as \\n, backslash as \\\\ (\\nu stays distinguishable)")
    j = json.loads(cli("show-event", str(e["seq"])).stdout)
    check(j["edited"] == EDIT and j["type"] == "diff_edit", "show-event prints the full JSON payload")
    check(cli("show-event", str(e["seq"]), "--field", "edited").stdout.rstrip("\n") == EDIT, "show-event --field edited prints the raw text")
    check(cli("show", str(e["seq"])).returncode == 0, "show still works")

    # 7. typing guard no longer blocks (nothing unsent); a long note is not cut off
    LONGNOTE = "Please keep the claim about the evidence. " * 40 + "FINAL-WORDS"
    yield (f"IA.send({{type:'diff',id:'other-x',title:'t',decision:'changes',note:{json.dumps(LONGNOTE)}}},null)")
    yield 600
    wl = next(l for l in cli("events", "--all").stdout.splitlines() if "CHANGES REQUESTED" in l)
    check("FINAL-WORDS" in wl and "…" not in wl and "..." not in wl, "long note printed in full by watch/events")

    # 8. Claude replies -> approvable; also edit-before-reply states
    r = cli("reply", f"#{e['seq']}", "Reads better; keep \\citet. Drop 'every' if you want to match §3.")
    check(r.returncode == 0, "reply accepted by CLI")
    yield 900
    i = yield info("p1")
    check(i["now"] == "3" and i["approveDisabled"] is False, "Claude replied: Approve my version unlocked")
    check((yield "document.querySelectorAll('.ia[data-ia-id=p1] > .ia-reply').length") == 1, "reply shown under the block")
    # editing again after the reply puts it back to 'edited'
    yield typ("p1", EDIT + " (more)")
    i = yield info("p1")
    check(i["now"] == "1" and i["approveDisabled"] is True, "changing the text after the reply re-locks Approve")
    yield typ("p1", EDIT)
    i = yield info("p1")
    check(i["now"] == "3" and i["approveDisabled"] is False, "back to the reviewed text: approvable again")
    yield 600

    # 9. Approve my version -> diff event carries the edited text; state.json for apply_card-style tools
    check((yield click(".ia[data-ia-id=p1] [data-act=approve]")) == "ok", "Approve my version clicked")
    yield 900
    a = last("diff")
    check(a and a["decision"] == "approve" and a.get("edited_text") == EDIT and a.get("proposal_original") == NEW,
          "approve event carries the edited text and the original")
    ap = next(l for l in cli("events", "--all").stdout.splitlines() if "APPROVED" in l and "p1" in l)
    check(json.dumps(EDIT, ensure_ascii=False) in ap, "watch line for the approval carries the full edited text")
    st = json.load(open(events.STATE))["entries"][0]
    ed = st["edits"].get("p1", {})
    check(ed.get("approved") is True and ed.get("text") == EDIT and ed.get("orig") == NEW, f"state.json edits.p1 approved with the author's text")
    check(NEW in st["text"], "the card markdown still holds Claude's original (apply_card unchanged)")
    win._reload_keep_scroll(); yield 2500
    i = yield info("p1")
    check(i["value"] == EDIT and i["now"] == "3", "approved edited wording survives a re-render")

    # 10. choice option: edit, send, alt, use Claude's wording
    check((yield click(".ia[data-ia-id=c1] .ia-opt[data-label=B] [data-act=ed-open]")) == "ok", "option B editor opened")
    OPT = r"Option B: \citet{X} shows $\Delta t$ is tiny."
    yield typ("c1", OPT, "B")
    i = yield info("c1", "B")
    check(i["now"] == "1" and i["send"], "option edit recognised")
    pickdis = yield "document.querySelector('.ia[data-ia-id=c1] .ia-opt[data-label=B] [data-act=pick]').disabled"
    check(pickdis is True, "Pick of an unreviewed edited option is locked")
    yield click(".ia[data-ia-id=c1] .ia-opt[data-label=B] [data-act=ed-send]")
    yield 800
    oe = last("diff_edit")
    check(oe["id"] == "c1" and oe.get("option") == "B" and oe["edited"] == OPT and oe["original"].startswith("Option B"), "option diff_edit event")
    ALT = r"Option B: \citet{X} shows that $\Delta t$ is negligible."
    r = cli("alt", str(oe["seq"]), "--note", "Tiny is vague; 'negligible' is the usual word.", stdin=ALT + "\n")
    check(r.returncode == 0, f"alt accepted {r.stderr.strip()}")
    yield 1000
    i = yield info("c1", "B")
    check(i["now"] == "3" and i["alt"], "alternative wording shown beside the author's version")
    cols = yield "document.querySelectorAll('.ia[data-ia-id=c1] .ia-edalt > div').length"
    check(cols == 2, "two columns: author's version | Claude's alternative diff")
    yield click(".ia[data-ia-id=c1] [data-act=ed-use]")
    i = yield info("c1", "B")
    check(i["value"].strip() == ALT, "Use Claude's wording loads it into the editor")
    yield click(".ia[data-ia-id=c1] .ia-opt[data-label=B] [data-act=pick]")
    yield 800
    c = last("choice")
    check(c and c["picked"] == ["B"] and c["picked_text"] == [ALT] and c["picked_edited"] == [True], "picking the edited option sends the edited text")
    check(json.dumps(ALT, ensure_ascii=False) in cli("events", "--all").stdout.split("picked B")[-1].split("\n")[0], "watch line shows the edited pick in full")
    # alt on a stale (superseded) event is refused
    r = cli("alt", "9999", stdin="x")
    check(r.returncode == 2, "alt for an unknown event refused")

    # 11. a card edit that changes the proposal resets the author's edit, noting what they had
    r = cli("edit", "--latest", stdin=NEWCARD)
    check(r.returncode == 0, f"edit allowed once nothing is unsent ({r.stderr.strip()[:80]})")
    yield 2500
    check("p1" not in entry().edits and entry().rev == 2, f"edit reset on the changed proposal (rev {entry().rev})")
    note = entry().revised.get("p1", "")
    check("your edited wording was reset" in note and "Handley2015" in note, "revised note says what the author had")
    i = yield info("p1")
    check(i["value"].strip() == "A completely different proposal for the same block." and i["now"] == 0, "editor shows the new proposal")
    check((yield "window.__errs.length") == 0, f"no JS errors during the run {(yield 'JSON.stringify(window.__errs)')}")


def main():
    open(f"{T}/resp.md", "w").write(CARD)
    win = renderer.RendererWindow()
    win._ucm.add_script(WebKit2.UserScript.new(
        "window.__errs=[];addEventListener('error',function(e){__errs.push(String(e.message))});",
        WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserScriptInjectionTime.START, None, None))
    gen = scenario(win)

    def advance(value=None):
        try:
            step = gen.send(value)
        except StopIteration:
            Gtk.main_quit(); return
        except Exception:
            traceback.print_exc(); FAILS.append("scenario raised"); Gtk.main_quit(); return
        if isinstance(step, int):
            GLib.timeout_add(step, lambda: (advance(), False)[1])
        else:
            def done(wv, res, _d=None):
                try:
                    v = json.loads(wv.evaluate_javascript_finish(res).to_json(0) or "null")
                except Exception as ex:
                    v = f"<js error: {ex}>"
                GLib.idle_add(lambda: (advance(v), False)[1])
            win._webview.evaluate_javascript(step, -1, None, None, None, done, None)

    GLib.timeout_add(1000, lambda: (advance(), False)[1])
    GLib.timeout_add(120000, lambda: (FAILS.append("timed out"), Gtk.main_quit()))
    Gtk.main()
    shutil.rmtree(T, ignore_errors=True)
    print(f"\n{len(FAILS)} failed" + (": " + "; ".join(FAILS) if FAILS else ""))
    sys.exit(1 if FAILS else 0)


main()
