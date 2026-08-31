#!/usr/bin/env python3
"""Floating WebKit2GTK window that renders markdown+LaTeX responses."""

import os
import json
import datetime
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, WebKit2, GLib

WATCH_FILE  = "/tmp/claude_response.md"
FLAGGED_DIR = os.path.expanduser("~/.local/share/latex-panel/flagged")
QUIZ_LOG    = os.path.expanduser("~/.local/share/latex-panel/quiz_results.jsonl")

# ---------------------------------------------------------------------------
# HTML template – all JS/CSS braces doubled because we use str.format()
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<script>
MathJax = {{
  tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']] }},
  options: {{ skipHtmlTags: ['script','noscript','style','textarea','pre'] }}
}};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
<link rel="stylesheet" type="text/css" href="https://tikzjax.com/v1/fonts.css">
<script src="https://tikzjax.com/v1/tikzjax.js"></script>
<script>
/* globals — injected saved answers replace this empty object */
window._savedAnswers = {{}};
window._replaying    = false;

/* ── MCQ answer ─────────────────────────────────────── */
function mcqSelect(btn) {{
  var mcq = btn.closest('.mcq');
  if (mcq.dataset.solved) return;
  var correct = btn.dataset.correct === 'true';
  mcq.dataset.solved = '1';
  if (correct) {{
    mcq.dataset.correctAnswered = 'true';
    btn.classList.add('correct');
  }} else {{
    btn.classList.add('wrong');
    mcq.querySelectorAll('.option').forEach(function(b) {{
      if (b.dataset.correct === 'true') b.classList.add('correct');
    }});
  }}
  mcq.querySelectorAll('.option').forEach(function(b) {{ b.classList.add('disabled'); }});

  if (!window._replaying) {{
    // send result to Python
    try {{
      window.webkit.messageHandlers.quizResult.postMessage({{
        question:  mcq.dataset.question || '',
        selected:  btn.textContent.trim(),
        correct:   correct,
        timestamp: new Date().toISOString()
      }});
    }} catch(e) {{}}

    // deck: update score bar and auto-advance on correct
    if (window._deckMode) {{
      window._deckAnswered = true;
      if (correct) window._deckScore++;
      _deckUpdateBar();
      if (correct) setTimeout(_deckNext, 700);
    }}
  }}
}}

/* ── Restore saved answers (visual only, no side-effects) ── */
function _restoreSavedAnswers() {{
  if (!window._savedAnswers || !Object.keys(window._savedAnswers).length) return;
  window._replaying = true;
  document.querySelectorAll('.mcq').forEach(function(mcq) {{
    var q     = mcq.dataset.question;
    var saved = window._savedAnswers[q];
    if (!saved) return;
    mcq.dataset.solved = '1';
    if (saved.correct) mcq.dataset.correctAnswered = 'true';
    mcq.querySelectorAll('.option').forEach(function(b) {{
      var isSelected = b.textContent.trim() === saved.selected;
      var isCorrect  = b.dataset.correct === 'true';
      if (isSelected) {{
        b.classList.add(isCorrect ? 'correct' : 'wrong');
      }} else if (isCorrect && !saved.correct) {{
        b.classList.add('correct');  // reveal correct answer when user was wrong
      }}
      b.classList.add('disabled');
    }});
  }});
  window._replaying = false;
}}

/* ── Deck mode ──────────────────────────────────────── */
window._deckMode     = false;
window._deckIdx      = 0;
window._deckScore    = 0;
window._deckAnswered = false;
window._deck         = [];

document.addEventListener('DOMContentLoaded', function() {{
  // restore saved MCQ states before doing anything else
  _restoreSavedAnswers();

  var mcqs = Array.from(document.querySelectorAll('.mcq'));
  if (mcqs.length < 2) return;

  window._deck     = mcqs;
  window._deckMode = true;

  // resume from first unanswered question (or last if all done)
  var startIdx = mcqs.findIndex(function(m) {{ return !m.dataset.solved; }});
  if (startIdx === -1) startIdx = mcqs.length - 1;
  window._deckIdx      = startIdx;
  window._deckAnswered = !!mcqs[startIdx].dataset.solved;
  window._deckScore    = mcqs.filter(function(m) {{ return m.dataset.correctAnswered; }}).length;

  // inject sticky progress bar
  var bar = document.createElement('div');
  bar.id = 'deck-bar';
  bar.innerHTML =
    '<span id="dk-prog"></span>' +
    '<span id="dk-score"></span>' +
    '<span class="dk-hint">1–4 → answer · Enter / → next · ← back</span>';
  document.body.insertBefore(bar, document.body.firstChild);

  // hide all but current
  mcqs.forEach(function(m, i) {{ m.style.display = i === startIdx ? '' : 'none'; }});
  window.scrollTo(0, 0);
  _deckUpdateBar();

  document.addEventListener('keydown', _deckKey);
}});

function _deckUpdateBar() {{
  var n = window._deck.length;
  var prog = document.getElementById('dk-prog');
  var sc   = document.getElementById('dk-score');
  if (!prog) return;
  prog.textContent = 'Q ' + (window._deckIdx + 1) + ' / ' + n;
  var answered = window._deck.filter(function(m) {{ return m.dataset.solved; }}).length;
  sc.textContent  = window._deckScore + ' correct / ' + answered + ' answered';
}}

function _deckKey(e) {{
  if (!window._deckMode) return;
  var mcq  = window._deck[window._deckIdx];
  var opts = mcq ? Array.from(mcq.querySelectorAll('.option')) : [];

  // 1-4: pick option — call mcqSelect directly to avoid browser scroll-to-focus
  if (!window._deckAnswered && e.key >= '1' && e.key <= '4') {{
    e.preventDefault();
    var idx = parseInt(e.key, 10) - 1;
    if (idx < opts.length) mcqSelect(opts[idx]);
    return;
  }}

  // Enter / Space / → : advance after answering
  if (window._deckAnswered &&
      (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowRight')) {{
    e.preventDefault();
    _deckNext();
    return;
  }}

  // ← : go back
  if (e.key === 'ArrowLeft' && window._deckIdx > 0) {{
    e.preventDefault();
    _deckPrev();
  }}
}}

function _deckNext() {{
  if (window._deckIdx >= window._deck.length - 1) {{
    _deckFinish(); return;
  }}
  window._deck[window._deckIdx].style.display = 'none';
  window._deckIdx++;
  window._deck[window._deckIdx].style.display = '';
  window._deckAnswered = !!window._deck[window._deckIdx].dataset.solved;
  window.scrollTo(0, 0);
  _deckUpdateBar();
}}

function _deckPrev() {{
  window._deck[window._deckIdx].style.display = 'none';
  window._deckIdx--;
  window._deck[window._deckIdx].style.display = '';
  window._deckAnswered = !!window._deck[window._deckIdx].dataset.solved;
  window.scrollTo(0, 0);
  _deckUpdateBar();
}}

function _deckFinish() {{
  var n   = window._deck.length;
  var pct = Math.round(100 * window._deckScore / n);
  var col = pct >= 80 ? '#155724' : pct >= 60 ? '#856404' : '#721c24';
  document.body.innerHTML =
    '<div style="padding:60px 40px;text-align:center;">' +
    '<h2 style="margin-bottom:8px">Quiz complete</h2>' +
    '<p style="font-size:3em;font-weight:700;color:' + col + ';margin:16px 0">' +
      window._deckScore + ' / ' + n +
    '</p>' +
    '<p style="font-size:1.4em;color:' + col + '">' + pct + '%</p>' +
    '</div>';
  window._deckMode = false;
}}
</script>
<style>
  body {{
    font-family: 'Linux Libertine', Georgia, serif;
    font-size: 17px;
    line-height: 1.6;
    max-width: 900px;
    margin: 20px auto;
    padding: 0 28px 40px;
    color: #1a1a1a;
    background: #fafaf8;
  }}
  pre {{
    background: #f0f0ec;
    border-left: 3px solid #999;
    padding: 10px 14px;
    overflow-x: auto;
    border-radius: 3px;
  }}
  code {{
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 14px;
    background: #f0f0ec;
    padding: 1px 4px;
    border-radius: 2px;
  }}
  pre code {{ background: none; padding: 0; }}
  h1, h2, h3 {{ color: #222; margin-top: 1.4em; }}
  h2 {{ border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  blockquote {{
    border-left: 3px solid #aaa;
    margin-left: 0;
    padding-left: 16px;
    color: #555;
  }}
  details {{
    border: 1px solid #ccc;
    border-radius: 4px;
    padding: 8px 14px;
    margin: 1em 0;
    background: #f7f7f5;
  }}
  details summary {{
    cursor: pointer;
    font-weight: 600;
    color: #444;
    user-select: none;
    padding: 2px 0;
    list-style: none;
  }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::before {{ content: "\\25B6  "; font-size: 0.75em; color: #888; }}
  details[open] summary::before {{ content: "\\25BC  "; }}
  details[open] summary {{ margin-bottom: 8px; }}
  /* deck progress bar */
  #deck-bar {{
    position: sticky;
    top: 0;
    background: #eeeee8;
    border-bottom: 1px solid #ccc;
    padding: 8px 16px;
    font-size: 14px;
    font-family: 'JetBrains Mono', monospace;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 200;
    margin: 0 -28px 20px;
  }}
  #dk-prog  {{ font-weight: 700; color: #333; }}
  #dk-score {{ color: #555; }}
  .dk-hint  {{ color: #999; font-size: 12px; }}
  /* MCQ */
  .mcq {{
    border: 1px solid #c5c5c0;
    border-radius: 6px;
    padding: 18px 20px;
    margin: 1.4em 0;
    background: #f9f9f7;
  }}
  .mcq .question {{
    font-weight: 600;
    font-size: 1.05em;
    margin-bottom: 14px;
    color: #222;
  }}
  .mcq .option {{
    display: block;
    width: 100%;
    text-align: left;
    padding: 9px 14px;
    margin: 5px 0;
    border: 1px solid #ddd;
    border-radius: 4px;
    background: white;
    cursor: pointer;
    font-size: 1em;
    font-family: inherit;
    transition: background 0.12s;
  }}
  .mcq .option:hover:not(.disabled) {{ background: #efefeb; }}
  .mcq .option.correct  {{ background: #d4edda; border-color: #28a745; color: #155724; font-weight: 600; }}
  .mcq .option.wrong    {{ background: #f8d7da; border-color: #dc3545; color: #721c24; }}
  .mcq .option.disabled {{ cursor: default; }}
  .tikz-wrap {{ text-align: center; margin: 1.5em 0; }}
  .tikz-wrap svg {{ display: inline-block; max-width: 100%; }}
</style>
</head>
<body>{body}</body>
</html>
"""


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------
def md_to_html(text: str) -> str:
    import re
    import html as html_mod
    from markdown_it import MarkdownIt

    stash: dict[str, str] = {}
    counter = [0]

    def new_key() -> str:
        k = f"STASH{counter[0]}Z"
        counter[0] += 1
        return k

    # :::tikz
    def replace_tikz(m: re.Match) -> str:
        key = new_key()
        stash[key] = (
            f'<div class="tikz-wrap">'
            f'<script type="text/tikz">{m.group(1)}</script>'
            f'</div>'
        )
        return f'\n{key}\n'

    text = re.sub(r'^:::tikz\n([\s\S]*?)^:::[ \t]*$', replace_tikz, text, flags=re.MULTILINE)

    # :::feynman
    def replace_feynman_diag(m: re.Match) -> str:
        inner = m.group(1).strip()
        tikz_src = (
            r'\begin{tikzpicture}' + '\n'
            r'\begin{feynman}' + '\n'
            + inner + '\n'
            + r'\end{feynman}' + '\n'
            + r'\end{tikzpicture}'
        )
        key = new_key()
        stash[key] = (
            f'<div class="tikz-wrap">'
            f'<script type="text/tikz">{tikz_src}</script>'
            f'</div>'
        )
        return f'\n{key}\n'

    text = re.sub(r'^:::feynman\n([\s\S]*?)^:::[ \t]*$', replace_feynman_diag, text, flags=re.MULTILINE)

    # :::spoiler
    def replace_spoiler(m: re.Match) -> str:
        title = m.group(1).strip() or "Show"
        inner_html = md_to_html(m.group(2))
        key = new_key()
        stash[key] = f'<details><summary>{title}</summary>{inner_html}</details>'
        return f'\n{key}\n'

    text = re.sub(r'^:::spoiler(.*?)\n([\s\S]*?)^:::[ \t]*$', replace_spoiler, text, flags=re.MULTILINE)

    # ?? Question\n( ) wrong\n(*) correct
    def replace_mcq(m: re.Match) -> str:
        question = m.group(1).strip()
        options_raw = m.group(2)
        tuples = re.findall(r'\(([* ])\)\s*(.+)', options_raw)
        opts_html = ''.join(
            f'<button class="option" onclick="mcqSelect(this)" data-correct="{str(mk == "*").lower()}">'
            f'{ot.strip()}</button>'
            for mk, ot in tuples
        )
        q_escaped = html_mod.escape(question, quote=True)
        key = new_key()
        stash[key] = (
            f'<div class="mcq" data-question="{q_escaped}">'
            f'<div class="question">{question}</div>{opts_html}</div>'
        )
        return f'\n{key}\n'

    text = re.sub(
        r'^\?\?\s*(.+?)\n((?:[ \t]*\([ *]\)[ \t]*.+\n?)+)',
        replace_mcq, text, flags=re.MULTILINE
    )

    # stash math
    def stash_match(m: re.Match) -> str:
        key = new_key()
        stash[key] = m.group(0)
        return key

    text = re.sub(r'\$\$[\s\S]+?\$\$', stash_match, text)
    text = re.sub(r'\$[^$\n]+?\$', stash_match, text)

    md = MarkdownIt("commonmark", {"html": True})
    html = md.render(text)

    for key, val in stash.items():
        html = html.replace(key, val)

    return html


def build_page(md_text: str, saved_answers: dict | None = None) -> str:
    body = md_to_html(md_text)
    html = HTML_TEMPLATE.format(body=body)
    if saved_answers:
        inject = f'<script>window._savedAnswers = {json.dumps(saved_answers)};</script>'
        html = html.replace('</head>', inject + '\n</head>', 1)
    return html


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class HistoryEntry:
    def __init__(self, text: str):
        self.text = text
        self.timestamp = datetime.datetime.now()
        self.flagged = False
        self.mcq_answers: dict[str, dict] = {}  # question → {selected, correct}

    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class RendererWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Claude")
        self.set_default_size(760, 720)
        self.set_keep_above(True)
        self.set_decorated(True)

        os.makedirs(FLAGGED_DIR, exist_ok=True)

        self._history: list[HistoryEntry] = []
        self._pos = -1
        self._mtime = None

        # ── UserContentManager for quiz recording ─────────────────
        self._ucm = WebKit2.UserContentManager()
        self._ucm.register_script_message_handler("quizResult")
        self._ucm.connect("script-message-received::quizResult", self._on_quiz_result)

        # ── toolbar ──────────────────────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_border_width(4)

        self._btn_prev = Gtk.Button(label="◀")
        self._btn_prev.set_tooltip_text("Previous response  [Alt+Left]")
        self._btn_prev.connect("clicked", self._on_prev)
        self._btn_prev.set_sensitive(False)
        toolbar.pack_start(self._btn_prev, False, False, 0)

        self._btn_next = Gtk.Button(label="▶")
        self._btn_next.set_tooltip_text("Next response  [Alt+Right]")
        self._btn_next.connect("clicked", self._on_next)
        self._btn_next.set_sensitive(False)
        toolbar.pack_start(self._btn_next, False, False, 0)

        self._lbl_pos = Gtk.Label(label="—")
        self._lbl_pos.set_margin_start(6)
        self._lbl_pos.set_margin_end(6)
        toolbar.pack_start(self._lbl_pos, False, False, 0)

        self._lbl_time = Gtk.Label(label="")
        self._lbl_time.set_markup("<small><i></i></small>")
        toolbar.pack_start(self._lbl_time, False, False, 0)

        toolbar.pack_start(Gtk.Label(), True, True, 0)

        self._btn_flag = Gtk.ToggleButton(label="★ Flag")
        self._btn_flag.set_tooltip_text("Flag this response for notes")
        self._btn_flag.connect("toggled", self._on_flag)
        self._btn_flag.set_sensitive(False)
        toolbar.pack_end(self._btn_flag, False, False, 0)

        self._btn_delete = Gtk.Button(label="✕ Delete")
        self._btn_delete.set_tooltip_text("Remove from history")
        self._btn_delete.connect("clicked", self._on_delete)
        self._btn_delete.set_sensitive(False)
        toolbar.pack_end(self._btn_delete, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)

        # ── webview ──────────────────────────────────────────────
        self._webview = WebKit2.WebView.new_with_user_content_manager(self._ucm)
        self._webview.get_settings().set_enable_javascript(True)
        self._webview.get_settings().set_enable_developer_extras(False)
        self._webview.get_settings().set_allow_universal_access_from_file_urls(True)

        scroll = Gtk.ScrolledWindow()
        scroll.add(self._webview)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(toolbar, False, False, 0)
        vbox.pack_start(sep, False, False, 0)
        vbox.pack_start(scroll, True, True, 0)
        self.add(vbox)

        # window-level key handler for history nav
        self.connect("key-press-event", self._on_key)

        self._load_file()
        GLib.timeout_add(500, self._poll)
        self.connect("destroy", Gtk.main_quit)
        self.show_all()

    # ── quiz recording ────────────────────────────────────────────

    def _on_quiz_result(self, _mgr, js_result):
        try:
            val = js_result.get_js_value()
            raw = val.to_json(0)
            data = json.loads(raw)
            data["session_ts"] = (
                self._history[self._pos].timestamp.isoformat()
                if self._pos >= 0 else datetime.datetime.now().isoformat()
            )
            os.makedirs(os.path.dirname(QUIZ_LOG), exist_ok=True)
            with open(QUIZ_LOG, "a") as f:
                f.write(json.dumps(data) + "\n")

            # persist answer in the history entry so it survives navigation
            if self._pos >= 0:
                entry = self._history[self._pos]
                entry.mcq_answers[data["question"]] = {
                    "selected": data["selected"],
                    "correct":  data["correct"],
                }
        except Exception:
            pass

    # ── history nav ───────────────────────────────────────────────

    def _on_key(self, _win, event):
        from gi.repository import Gdk
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        alt = state == Gdk.ModifierType.MOD1_MASK
        if alt and event.keyval == Gdk.KEY_Left:
            self._on_prev(None)
            return True
        if alt and event.keyval == Gdk.KEY_Right:
            self._on_next(None)
            return True
        return False

    def _on_prev(self, _btn):
        if self._pos > 0:
            self._pos -= 1
            self._render_current()

    def _on_next(self, _btn):
        if self._pos < len(self._history) - 1:
            self._pos += 1
            self._render_current()

    def _on_delete(self, _btn):
        if self._pos < 0:
            return
        self._history.pop(self._pos)
        if not self._history:
            self._pos = -1
        elif self._pos >= len(self._history):
            self._pos = len(self._history) - 1
        self._render_current()

    def _on_flag(self, btn):
        if self._pos < 0:
            return
        entry = self._history[self._pos]
        entry.flagged = btn.get_active()
        if entry.flagged:
            self._save_flagged(entry)
            btn.set_label("★ Flagged")
        else:
            btn.set_label("★ Flag")

    def _save_flagged(self, entry: HistoryEntry):
        ts = entry.timestamp.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FLAGGED_DIR, f"{ts}.md")
        with open(path, "w") as f:
            f.write(f"<!-- flagged {entry.timestamp.isoformat()} -->\n\n")
            f.write(entry.text)

    # ── rendering ────────────────────────────────────────────────

    def _render_current(self):
        if self._pos < 0:
            self._webview.load_html(
                HTML_TEMPLATE.format(body="<p style='color:#888'>Waiting for response…</p>"),
                "file:///tmp/"
            )
            self._lbl_pos.set_text("—")
            self._lbl_time.set_markup("<small><i></i></small>")
            self._btn_prev.set_sensitive(False)
            self._btn_next.set_sensitive(False)
            self._btn_flag.set_sensitive(False)
            self._btn_delete.set_sensitive(False)
            return

        entry = self._history[self._pos]
        n = len(self._history)

        try:
            self._webview.load_html(
                build_page(entry.text, entry.mcq_answers or None),
                "file:///tmp/"
            )
        except Exception as e:
            self._webview.load_html(
                HTML_TEMPLATE.format(body=f"<pre>Error: {e}</pre>"),
                "file:///tmp/"
            )

        self._lbl_pos.set_text(f"{self._pos + 1} / {n}")
        self._lbl_time.set_markup(f"<small><i>{entry.timestamp_str()}</i></small>")
        self._btn_prev.set_sensitive(self._pos > 0)
        self._btn_next.set_sensitive(self._pos < n - 1)
        self._btn_flag.set_sensitive(True)
        self._btn_delete.set_sensitive(True)

        self._btn_flag.handler_block_by_func(self._on_flag)
        self._btn_flag.set_active(entry.flagged)
        self._btn_flag.set_label("★ Flagged" if entry.flagged else "★ Flag")
        self._btn_flag.handler_unblock_by_func(self._on_flag)

    # ── file watching ─────────────────────────────────────────────

    def _load_file(self):
        if not os.path.exists(WATCH_FILE):
            self._render_current()
            return
        try:
            with open(WATCH_FILE) as f:
                text = f.read()
        except Exception as e:
            self._webview.load_html(
                HTML_TEMPLATE.format(body=f"<pre>Error reading file: {e}</pre>"),
                "file:///tmp/"
            )
            return

        if self._history and self._history[-1].text == text:
            return

        entry = HistoryEntry(text)
        self._history.append(entry)
        self._pos = len(self._history) - 1
        self._render_current()

    def _poll(self):
        try:
            mtime = os.path.getmtime(WATCH_FILE) if os.path.exists(WATCH_FILE) else None
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            self._load_file()
        return True


def main():
    RendererWindow()
    Gtk.main()


if __name__ == "__main__":
    main()
