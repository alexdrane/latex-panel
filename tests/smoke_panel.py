"""Offscreen acceptance test of the real WebKitGTK panel: clicks -> events, Claude -> panel, card identity.

    WEBKIT_DISABLE_COMPOSITING_MODE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 python3 tests/smoke_panel.py

Uses a temp watch file + data dir (LATEX_PANEL_WATCH / LATEX_PANEL_DATA), so the running
panel and your real event log are untouched. Exits non-zero on any failed check, any page
JavaScript error, or a timeout.
"""
import os, sys, json, struct, zlib
import tempfile, shutil, subprocess, traceback
T = tempfile.mkdtemp(prefix="latex-panel-smoke-")
os.makedirs(f"{T}/data")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["LATEX_PANEL_WATCH"] = f"{T}/resp.md"; os.environ["LATEX_PANEL_DATA"] = f"{T}/data"
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, GLib, WebKit2
Gtk.Window = Gtk.OffscreenWindow          # render without putting a window on screen
sys.path.insert(0, os.path.join(REPO, "latexpanel"))
import renderer, events

FAILS: list[str] = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)

def png(path, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * 8 for _ in range(8))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

FIG = f"{T}/fig.png"
png(FIG, (200, 30, 30))
PARA = "We find that repartitioning reduces the variance of $\\ln Z$ by a factor $\\sim 3.4$ across all runs."
DEMO = f"""# Smoke

{PARA}

Second paragraph stays.

:::checklist plan Plan
- [x] one
- [ ] update §4
:::

:::approve sec5 Sign off §5
Short text.
:::

:::approve fig Sign off the figure
:::img {FIG}
:::

:::approve other Unrelated sign-off
Nothing attached.
:::
"""

def evs():
    return [json.loads(l) for l in open(events.EVENTS)] if os.path.exists(events.EVENTS) else []
def last(t):
    return next((e for e in reversed(evs()) if e.get("type") == t), None)
def cli(*args, stdin=""):
    return subprocess.run([sys.executable, os.path.join(REPO, "latexpanel/events.py"), *args], env=os.environ,
                          input=stdin, capture_output=True, text=True, timeout=20)
def click(sel):
    return f"(function(){{var b=document.querySelector({json.dumps(sel)}); if(!b) return 'missing'; b.click(); return 'ok'}})()"
STATUS = "(document.querySelector('.ia[data-ia-id={}] .ia-status')||{{}}).textContent||''"


def scenario(win):
    entry = lambda: win._history[0]
    yield 4000
    errs = yield "window.__errs.length"
    check(errs == 0, "page loads without JS errors")

    # 1. a click carries the card identity and revision it was made on
    check((yield click(".ia[data-ia-id=sec5] [data-act=approve]")) == "ok", "approve button exists")
    yield 800
    e = last("approve")
    check(e and e.get("card") == entry().uid and e.get("rev") == 1, f"approve event carries card+rev ({e and (e.get('card'), e.get('rev'))})")
    check(entry().uid.startswith(renderer.SESSION + "-"), "card uid is session-unique")
    check("sec5" in entry().decisions, "decision stored")

    # 2. a click made on an out-of-date page is refused, not attributed to the new content
    yield "window._iaSaved.rev = 0"
    yield click(".ia[data-ia-id=other] [data-act=approve]")
    yield 800
    check("other" not in entry().decisions, "stale-revision click not stored as a decision")
    check((last("panel_error") or {}).get("text", "").startswith("ignored a stale approve"), "stale click reported to Claude")
    yield 2000    # panel reloads the page after a refusal

    # 3. rewrite: request -> proposal A -> replaced by B (fresh id, no inherited approval) -> approve B -> committed
    yield "(function(){var el=document.querySelector('[data-para=\"1\"]');IA.send({type:'rewrite',para:1,text:IA.fragText(el).trim(),instruction:'shorter',context:''},null)})()"
    yield 800
    rw = last("rewrite")
    check(rw and rw["text"].startswith("We find"), "rewrite request sent for paragraph 1")
    r = cli("suggest", str(rw["seq"]), stdin="Repartitioning cuts the variance by $\\sim 3.4\\times$ (A).")
    check(r.returncode == 0, f"suggest A accepted by CLI {r.stderr.strip()}")
    yield 1200
    idA = (yield "(document.querySelector('.ia-sugg .ia')||{dataset:{}}).dataset.iaId")
    check(idA == f"rw-{rw['seq']}-1", f"proposal A shown as {idA}")
    yield f"(function(){{var b=document.querySelector('.ia[data-ia-id=\"{idA}\"]'); b.querySelector('textarea').value='tighter'; b.querySelector('[data-act=approve]').click()}})()"
    yield 500
    text_after_A = entry().text
    check("(A)" in text_after_A, "approving A committed it into the card markdown")
    check(entry().rev == 2, f"commit bumped the revision (rev {entry().rev})")
    yield 2000
    # B arrives late for the same request, after the card was revised by A's commit -> rejected
    r = cli("suggest", str(rw["seq"]), stdin="Variance down $3.4\\times$ (B).")
    check(r.returncode == 2 and "revised" in r.stderr, "late proposal after a revision rejected by CLI")
    events.to_panel({"kind": "suggest", "seq": rw["seq"], "text": "Variance down (B).", "src": PARA})
    yield 1000
    check("(B)" not in entry().text and not entry().suggestions, "late proposal rejected by the panel too")
    check("revised" in (last("panel_error") or {}).get("text", ""), "late proposal reported to Claude")

    # replacement without an intervening revision: fresh id, A's approval doesn't transfer
    yield "(function(){var el=document.querySelector('[data-para=\"2\"]');IA.send({type:'rewrite',para:2,text:IA.fragText(el).trim(),instruction:'longer',context:''},null)})()"
    yield 800
    rw2 = last("rewrite")
    cli("suggest", str(rw2["seq"]), stdin="Second paragraph stays put (C).")
    yield 1000
    idC = (yield "(document.querySelector('.ia-sugg .ia')||{dataset:{}}).dataset.iaId")
    yield click(f".ia[data-ia-id='{idC}'] [data-act=reject]")
    yield 600
    cli("suggest", str(rw2["seq"]), stdin="Second paragraph stays put (D).")
    yield 1000
    idD = (yield "(document.querySelector('.ia-sugg .ia')||{dataset:{}}).dataset.iaId")
    nsugg = (yield "document.querySelectorAll('.ia-sugg').length")
    check(idD and idD != idC and nsugg == 1, f"replacement has a fresh id ({idC} → {idD}) and supersedes the old one")
    st = (yield STATUS.format(idD))
    check(idD not in entry().decisions and st == "awaiting you", f"replacement starts undecided ({st!r})")
    cli("suggest", str(rw2["seq"]), stdin="Second paragraph stays put (D).")   # identical retransmission
    yield 800
    check((yield "document.querySelector('.ia-sugg .ia').dataset.iaId") == idD, "identical retransmission is a no-op")

    # Notes / checkout use the committed wording
    win._on_notes(None)
    notes = open(last("to_notes")["file"]).read()
    check("(A)" in notes and PARA not in notes, "Notes export has the accepted wording")
    r = cli("current", "--entry", "1")
    check(r.returncode == 0 and "(A)" in open(events.EDIT_FILE).read(), "checkout has the accepted wording")

    # 4. checkout -> card changes -> edit from stale checkout refused
    events.to_panel({"kind": "edit", "uid": entry().uid, "text": entry().text + "\nAppended line.\n"})
    yield 1500
    r = cli("edit")
    check(r.returncode == 2 and "changed since checkout" in r.stderr, "edit from an outdated checkout refused")
    check("(A)" in entry().text and "Appended line." in entry().text, "card kept the newer content")

    # 5. a checkout from an earlier panel process can't alias a new card
    open(events.EDIT_UID, "w").write("zzzzzz-1@1")
    r = cli("edit")
    check(r.returncode == 2 and "no longer in the panel" in r.stderr, "edit to a card uid from another session refused")
    events.to_panel({"kind": "edit", "uid": "zzzzzz-1", "text": "# should not appear\n"})
    yield 1000
    check(len(win._history) == 1 and "should not appear" not in entry().text, "panel never turns an unknown edit into a new card")

    # 6. recompiled figure, identical markdown -> figure sign-off invalidated, unrelated one kept
    yield "(function(){document.querySelector('.ia[data-ia-id=fig] [data-act=approve]').click();" \
          "document.querySelector('.ia[data-ia-id=other] [data-act=approve]').click()})()"
    yield 800
    check({"fig", "other"} <= set(entry().decisions), "figure and unrelated block approved")
    rev0 = entry().rev
    src0 = (yield "document.querySelector('.ia[data-ia-id=fig] img').src")
    png(FIG, (30, 30, 200)); os.utime(FIG, (1, 2_000_000_000))
    r = cli("current", "--entry", "1"); r = cli("edit")
    check(r.returncode == 0, f"identical edit accepted {r.stderr.strip()}")
    yield 1800
    check("fig" not in entry().decisions and "fig" in entry().revised, "recompiled figure: its approval retired")
    check("other" in entry().decisions, "unrelated approval kept")
    check(entry().rev == rev0 + 1, "asset change bumped the revision")
    src1 = (yield "document.querySelector('.ia[data-ia-id=fig] img').src")
    check(src0 != src1, "page shows the new pixels (cache-busted src)")

    # 7. navigation: figure changes while another card is shown -> reconciled on the way back
    yield "document.querySelector('.ia[data-ia-id=fig] [data-act=approve]').click()"
    yield 800
    open(f"{T}/resp.md", "w").write("# second card\n\nplain\n")
    yield 1500
    png(FIG, (30, 200, 30)); os.utime(FIG, (1, 2_000_000_100))
    win._on_prev(None)
    yield 1800
    check("fig" not in entry().decisions and "other" in entry().decisions, "navigation reconciles changed files")

    # live updates and scroll survive as before
    events.to_panel({"kind": "tick", "id": "plan", "item": "§4", "state": "done"})
    events.to_panel({"kind": "reply", "target": "sec5", "text": "Applying §5 now."})
    events.to_panel({"kind": "reply", "target": "nope", "text": "lost"})
    yield 1000
    check((yield STATUS.format("plan")) == "2 / 2 done", "tick updates the checklist")
    check("Applying" in (yield "(document.querySelector('.ia[data-ia-id=sec5] .ia-reply')||{}).textContent||''"), "reply shown under its block")
    check("nope" in (last("panel_error") or {}).get("text", ""), "reply to an unknown block reported")
    yield "window.scrollTo(0, 120)"
    yield 200
    events.to_panel({"kind": "edit", "uid": entry().uid, "text": entry().text.replace("Short text.", "Shorter text.")})
    yield 1800
    check((yield "window.scrollY") == 120, "in-place edit keeps the scroll position")
    rn = (yield "(document.querySelector('.ia[data-ia-id=sec5] .ia-revnote')||{}).textContent||''")
    check("approved" in rn, f"edited block shows what he'd said before ({rn!r})")
    check((yield STATUS.format("plan")) == "2 / 2 done", "unchanged checklist keeps its ticks")

    # popups stay inside the unobscured window; keyboard reaches rewrite + checklist
    pop = (yield "(function(){var i=document.querySelector('.ia[data-ia-id=fig] img'), r=i.getBoundingClientRect();"
                 "scrollBy(0, r.bottom-(innerHeight-IA.bottomReserve()-4)); r=i.getBoundingClientRect();"
                 "i.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.right-3,clientY:r.bottom-3}));"
                 "var p=document.querySelector('.ia-pop').getBoundingClientRect(), q=document.querySelector('.ia-pin.draft').getBoundingClientRect();"
                 "return {inside: p.left>=0 && p.right<=innerWidth && p.top>=0 && p.bottom<=innerHeight-IA.bottomReserve(),"
                 " onBody: document.querySelector('.ia-pop').parentNode===document.body, pinFree: q.bottom<=p.top||q.top>=p.bottom||q.right<=p.left}})()")
    check(pop.get("inside") and pop.get("onBody") and pop.get("pinFree"), f"pin popup inside the visible area, pin not covered {pop}")
    yield "document.querySelector('.ia-pop textarea').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))"
    check((yield "document.querySelectorAll('.ia-pin.draft, .ia-pop').length") == 0, "Escape discards the draft pin")
    kb = (yield "(function(){var el=document.querySelector('[data-para=\"0\"]'); el.focus();"
                "el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));"
                "var open=!!document.getElementById('ia-rwpop'), f=document.activeElement.tagName;"
                "document.querySelector('#ia-rwpop textarea').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
                "return [el.tabIndex, open, f, document.activeElement===el, document.querySelector('.ia-box').tagName]})()")
    check(kb == [0, True, "TEXTAREA", True, "BUTTON"], f"keyboard: paragraph focusable, Enter opens rewrite, Escape returns focus, checkbox is a button {kb}")

    # 8. a reply addressed to an event from a deleted card is refused, not shown elsewhere
    seq = last("approve")["seq"]
    win._on_delete(None)          # deletes card 1 (shown)
    yield 1000
    r = cli("reply", f"#{seq}", "hello")
    check(r.returncode == 2, "reply to an event from a deleted card refused by CLI")
    events.to_panel({"kind": "reply", "target": f"#{seq}", "text": "hello"})
    yield 800
    check(not win._history[0].replies, "panel doesn't attach it to another card")

    errs = yield "JSON.stringify(window.__errs)"
    check(errs == "[]", f"no JS errors during the run {errs}")


def main():
    open(f"{T}/resp.md", "w").write(DEMO)
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
                except Exception as e:
                    v = f"<js error: {e}>"
                GLib.idle_add(lambda: (advance(v), False)[1])
            win._webview.evaluate_javascript(step, -1, None, None, None, done, None)

    def timeout():
        FAILS.append("timed out"); Gtk.main_quit()
    GLib.timeout_add(1000, lambda: (advance(), False)[1])
    GLib.timeout_add(120000, timeout)
    Gtk.main()
    shutil.rmtree(T, ignore_errors=True)
    print(f"\n{len(FAILS)} failed" + (": " + "; ".join(FAILS) if FAILS else ""))
    sys.exit(1 if FAILS else 0)


main()
