#!/usr/bin/env python3
"""Floating WebKit2GTK window that renders markdown+LaTeX responses."""

import os
import json
import datetime
import re
import uuid
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, WebKit2, GLib

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interactive  # noqa: E402  (blocks that talk back to Claude)
import events       # noqa: E402

WATCH_FILE  = os.environ.get("LATEX_PANEL_WATCH", "/tmp/claude_response.md")
DATA_DIR    = os.environ.get("LATEX_PANEL_DATA", os.path.expanduser("~/.local/share/latex-panel"))
FLAGGED_DIR = os.path.join(DATA_DIR, "flagged")
QUIZ_LOG    = os.path.join(DATA_DIR, "quiz_results.jsonl")

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
  if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
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
  if (window.IA) {{ IA.mountComposer(); IA.send({{type: 'quiz_done', score: window._deckScore, n: n}}); }}
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
    color: #131a21;
    background: #e7edf3;
  }}
  pre {{
    background: #dfe8f3;
    border-left: 3px solid #7f97b3;
    padding: 10px 14px;
    overflow-x: auto;
    border-radius: 3px;
  }}
  code {{
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 14px;
    background: #dfe8f3;
    padding: 1px 4px;
    border-radius: 2px;
  }}
  pre code {{ background: none; padding: 0; }}
  h1, h2, h3 {{ color: #1a212a; margin-top: 1.4em; }}
  h2 {{ border-bottom: 1px solid #c5d6ea; padding-bottom: 4px; }}
  blockquote {{
    border-left: 3px solid #9aa9ba;
    margin-left: 0;
    padding-left: 16px;
    color: #3f536a;
  }}
  table {{
    border-collapse: collapse;
    margin: 1em 0;
    font-size: 0.95em;
  }}
  th, td {{
    border: 1px solid #c3cbd5;
    padding: 5px 12px;
    text-align: left;
  }}
  th {{
    background: #eef2f7;
    font-weight: 600;
  }}
  tr:nth-child(even) td {{ background: #f7f9fb; }}
  details {{
    border: 1px solid #c3cbd5;
    border-radius: 4px;
    padding: 8px 14px;
    margin: 1em 0;
    background: #e6eef7;
  }}
  details summary {{
    cursor: pointer;
    font-weight: 600;
    color: #334355;
    user-select: none;
    padding: 2px 0;
    list-style: none;
  }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::before {{ content: "\\25B6  "; font-size: 0.75em; color: #6986a7; }}
  details[open] summary::before {{ content: "\\25BC  "; }}
  details[open] summary {{ margin-bottom: 8px; }}
  /* deck progress bar */
  #deck-bar {{
    position: sticky;
    top: 0;
    background: #d2dde9;
    border-bottom: 1px solid #c3cbd5;
    padding: 8px 16px;
    font-size: 14px;
    font-family: 'JetBrains Mono', monospace;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 200;
    margin: 0 -28px 20px;
  }}
  #dk-prog  {{ font-weight: 700; color: #263240; }}
  #dk-score {{ color: #3f536a; }}
  .dk-hint  {{ color: #7f97b3; font-size: 12px; }}
  /* MCQ */
  .mcq {{
    border: 1px solid #b7c2ce;
    border-radius: 6px;
    padding: 18px 20px;
    margin: 1.4em 0;
    background: #eff3f7;
  }}
  .mcq .question {{
    font-weight: 600;
    font-size: 1.05em;
    margin-bottom: 14px;
    color: #1a212a;
  }}
  .mcq .option {{
    display: block;
    width: 100%;
    text-align: left;
    padding: 9px 14px;
    margin: 5px 0;
    border: 1px solid #c5d6ea;
    border-radius: 4px;
    background: white;
    cursor: pointer;
    font-size: 1em;
    font-family: inherit;
    transition: background 0.12s;
  }}
  .mcq .option:hover:not(.disabled) {{ background: #dde7f3; }}
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

    def put(html: str) -> str:
        key = new_key()
        stash[key] = html
        return f'\n{key}\n'

    text = interactive.process(text, md_to_html, put)

    # :::video /abs/path/to/clip.mp4 [caption]   (Manim renders; see panel.sh manim)
    def replace_video(m: re.Match) -> str:
        path = m.group(1)
        cap = html_mod.escape((m.group(2) or "").strip())
        key = new_key()
        rates = "".join(
            f'<button onclick="var f=this.closest(\'figure\'),v=f.querySelector(\'video\');'
            f'v.playbackRate={r};window._vrate={r};'
            f'f.querySelectorAll(\'.vspd\').forEach(function(b){{b.classList.toggle(\'on\',b===this)}},this)" '
            f'class="vspd{" on" if r == 0.75 else ""}">{r:g}\u00d7</button>'
            for r in (0.25, 0.5, 0.75, 1)
        )
        stash[key] = (
            '<figure class="mvid"><style>'
            '.mvid{margin:1em auto;text-align:center}'
            '.mvid video{max-width:100%;border-radius:6px;background:#000}'
            '.mvid .vctl{margin-top:4px;font-size:12px}'
            '.mvid button{margin:0 2px;padding:2px 8px;border:1px solid #888;border-radius:4px;background:transparent;color:inherit;cursor:pointer}'
            '.mvid button.on{background:#4a90d9;color:#fff;border-color:#4a90d9}'
            '.mvid figcaption{font-size:13px;opacity:.75;margin-top:4px}'
            '</style>'
            f'<video src="file://{html_mod.escape(path, quote=True)}" autoplay loop muted controls '
            'onloadedmetadata="this.playbackRate=window._vrate||0.75"></video>'
            f'<div class="vctl">{rates}'
            '<button onclick="var v=this.closest(\'figure\').querySelector(\'video\');v.currentTime=0;v.play()">\u21bb</button></div>'
            + (f'<figcaption>{cap}</figcaption>' if cap else '')
            + '</figure>'
        )
        return f'\n{key}\n'

    text = re.sub(r'^:::video[ \t]+(\S+)(?:[ \t]+(.*))?$', replace_video, text, flags=re.MULTILINE)

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

    for left in re.findall(r'^:::[ \t]*(\S*)', text, flags=re.MULTILINE):
        if left:
            interactive.WARNINGS.append(f"unrecognised or unclosed directive ':::{left}' (shown as text)")

    text = re.sub(r'\$\$[\s\S]+?\$\$', stash_match, text)
    text = re.sub(r'\$[^$\n]+?\$', stash_match, text)

    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    try:
        from mdit_py_plugins.tables import tables_plugin
        md = md.use(tables_plugin)
    except ImportError:
        pass  # table syntax falls back to plain text if the plugin isn't installed
    html = md.render(text)

    for key, val in stash.items():
        html = html.replace(key, val)

    return html


def render_body(md_text: str) -> str:
    """Markdown → html body; afterwards interactive.BLOCKS describes the document's blocks."""
    interactive.begin_document()
    return md_to_html(md_text)


def build_page(md_text: str, saved_answers: dict | None = None, saved_ia: dict | None = None,
               body: str | None = None) -> str:
    if body is None:
        body = render_body(md_text)
    html = HTML_TEMPLATE.format(body=body)
    inject = ''
    if saved_answers:
        inject += f'<script>window._savedAnswers = {json.dumps(saved_answers)};</script>'
    if saved_ia:
        inject += f'<script>window._iaSaved = {json.dumps(saved_ia).replace("</", "<\\/")};</script>'
    html = html.replace('</head>', inject + interactive.HEAD + '\n</head>', 1)
    return html


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
SESSION = uuid.uuid4().hex[:6]   # card uids from an earlier panel process must never alias new ones


class HistoryEntry:
    _next_uid = 1

    def __init__(self, text: str):
        self.uid = f"{SESSION}-{HistoryEntry._next_uid}"   # stable across deletes (indices shift)
        HistoryEntry._next_uid += 1
        self.rev = 1                               # bumped whenever what Alex is deciding on changes
        self.text = text
        self.versions: list[str] = []              # earlier texts, before in-place edits
        self.edited_at: datetime.datetime | None = None
        self.revised: dict[str, str] = {}          # block id → what Alex had said before Claude revised it
        self.old_pins: dict[str, list] = {}        # viewer id → pins made on a previous render
        self.timestamp = datetime.datetime.now()
        self.flagged = False
        self.mcq_answers: dict[str, dict] = {}  # question → {selected, correct}
        self.decisions: dict[str, dict] = {}    # interactive block id → last event
        self.pins: dict[str, list] = {}         # viewer id → pin events
        self.blocks: list[tuple] = []           # (kind, id, source hash) of interactive blocks, filled on render
        self.suggestions: dict[str, dict] = {}  # paragraph index → {id, html, seq, src, new} (in-place rewrites)
        self.rw_count = 0                        # proposal counter: each new proposal gets a fresh block id
        self.pending_rw: dict[int, int] = {}    # paragraph → seq of the rewrite request waiting on it
        self.ticks: list[tuple] = []            # (checklist id, item, state) in order applied
        self.replies: list[tuple] = []          # (block id or None, text, iso ts)

    def ids(self) -> set[str]:
        return {b[1] for b in self.blocks} | {s["id"] for s in self.suggestions.values()}

    @property
    def title(self) -> str:
        for line in self.text.splitlines():
            if line.startswith("#"):
                return line.lstrip("# ").strip()
        return next((l.strip() for l in self.text.splitlines() if l.strip()), "")[:60]

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
        self._restore_y = 0
        self._seen = events.seen_cursor()   # highest event seq a Claude session has read

        # ── UserContentManager for quiz recording ─────────────────
        self._ucm = WebKit2.UserContentManager()
        self._ucm.register_script_message_handler("quizResult")
        self._ucm.connect("script-message-received::quizResult", self._on_quiz_result)
        self._ucm.register_script_message_handler("claudeEvent")
        self._ucm.connect("script-message-received::claudeEvent", self._on_claude_event)

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

        self._lbl_listen = Gtk.Label()
        self._lbl_listen.set_margin_end(8)
        toolbar.pack_start(self._lbl_listen, False, False, 0)
        self._listening = None
        self._update_listening()
        GLib.timeout_add(1000, self._update_listening)

        self._btn_flag = Gtk.ToggleButton(label="★ Flag")
        self._btn_flag.set_tooltip_text("Flag this response for notes")
        self._btn_flag.connect("toggled", self._on_flag)
        self._btn_flag.set_sensitive(False)
        toolbar.pack_end(self._btn_flag, False, False, 0)

        self._btn_notes = Gtk.Button(label="📥 Notes")
        self._btn_notes.set_tooltip_text("Ask Claude to file this response into your Obsidian notes now")
        self._btn_notes.connect("clicked", self._on_notes)
        self._btn_notes.set_sensitive(False)
        toolbar.pack_end(self._btn_notes, False, False, 0)

        self._btn_pending = Gtk.Button()
        self._btn_pending.set_tooltip_text("Blocks still waiting on your decision - click to jump to the latest")
        self._btn_pending.connect("clicked", self._on_pending)
        self._btn_pending.set_no_show_all(True)
        toolbar.pack_start(self._btn_pending, False, False, 0)

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
        self._webview.connect("load-changed", self._on_load_changed)
        self._scroll_pending = False
        self._seq_map: dict[int, tuple] = {}   # event seq → (HistoryEntry, event) for this session
        try:
            self._tp_off = os.path.getsize(events.TO_PANEL)   # ignore messages from before startup
        except OSError:
            self._tp_off = 0

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

    # ── panel → Claude events ─────────────────────────────────────

    def _update_listening(self):
        seen = events.seen_cursor()
        if seen != self._seen:
            self._seen = seen
            self._js(f"IA.seen({seen})")
        alive = events.watcher_alive()
        if alive != self._listening:
            self._listening = alive
            if alive:
                self._lbl_listen.set_markup("<small><span foreground='#2e7d4f'>●</span> Claude listening</small>")
                self._lbl_listen.set_tooltip_text("Clicks reach the Claude session immediately")
            else:
                self._lbl_listen.set_markup("<small><span foreground='#8a96a3'>○</span> not listening</small>")
                self._lbl_listen.set_tooltip_text(
                    "No Claude session is watching right now - clicks are queued and\n"
                    "Claude sees them next time it checks (panel.sh events)")
        return True

    def _emit(self, data: dict, entry: "HistoryEntry | None" = None) -> dict:
        n = len(self._history)
        if entry is None and self._pos >= 0:
            entry = self._history[self._pos]
        if entry is not None and entry in self._history:
            i = self._history.index(entry)
            data.update(entry=i + 1, entries=n, stale=i != n - 1, card=entry.uid, rev=entry.rev,
                        entry_title=entry.title, entry_ts=entry.timestamp.isoformat())
        return events.append(data)

    def _notice(self, text: str, **extra):
        """Tell the Claude session something it asked for didn't happen (shows up in watch/events)."""
        print(f"panel: {text}", file=sys.stderr)
        events.append({"type": "panel_error", "text": text, **extra})

    def _entry_by_uid(self, uid):
        return next((e for e in self._history if e.uid == uid), None)

    def _on_claude_event(self, _mgr, js_result):
        try:
            data = json.loads(js_result.get_js_value().to_json(0))
            cid = data.get("cid")
            # the page says which card + revision the click was made on; never guess another one
            entry = self._entry_by_uid(data.pop("card", None))
            page_rev = data.pop("rev", None)
            if entry is None or (page_rev is not None and page_rev != entry.rev):
                why = "that card was deleted" if entry is None else \
                      f"the card was revised (r{page_rev} → r{entry.rev}) after it was shown"
                self._notice(f"ignored a stale {data.get('type')} click: {why}", rejected=data)
                if cid:
                    self._js(f"IA.refused({json.dumps(cid)}, {json.dumps('Not sent - ' + why + '. Reloading.')})")
                if entry is not None and self._shown(entry):
                    GLib.timeout_add(1200, lambda: (self._reload_keep_scroll(), False)[1])
                return
            ev = self._emit(data, entry)
            t = data.get("type")
            self._seq_map[ev["seq"]] = (entry, ev)
            if t == "rewrite":
                entry.pending_rw[int(data["para"])] = ev["seq"]
            elif t == "check":
                entry.ticks.append((data["id"], data["item"], data["state"]))
            elif t == "pin":
                entry.pins.setdefault(data["id"], []).append(ev)
            elif data.get("id") and t not in ("selection", "message", "check_add"):
                entry.decisions[data["id"]] = ev
            if cid:
                self._js(f"IA.sent({json.dumps(cid)}, {ev['seq']})")
            if t == "diff" and data.get("decision") == "approve":
                sg = next((s for s in entry.suggestions.values() if s["id"] == data.get("id")), None)
                if sg:
                    self._commit_rewrite(entry, sg)
            self._update_pending()
        except Exception as e:
            print(f"claudeEvent failed: {e}", file=sys.stderr)

    # ── Claude → panel (replies, rewrite suggestions, checklist ticks) ──

    def _js(self, code: str):
        try:
            self._webview.evaluate_javascript(f"window.IA && ({code});", -1, None, None, None, None, None)
        except (AttributeError, TypeError):
            self._webview.run_javascript(f"window.IA && ({code});", None, None, None)

    def _shown(self, entry) -> bool:
        return 0 <= self._pos < len(self._history) and self._history[self._pos] is entry

    def _entry_for_id(self, bid: str):
        for entry in reversed(self._history):
            if bid in entry.ids():
                return entry
        return None

    def _drain_to_panel(self):
        try:
            size = os.path.getsize(events.TO_PANEL)
        except OSError:
            return
        if size < self._tp_off:
            self._tp_off = 0
        if size == self._tp_off:
            return
        with open(events.TO_PANEL) as f:
            f.seek(self._tp_off)
            chunk = f.read()
            self._tp_off = f.tell()
        for line in chunk.splitlines():
            try:
                self._handle_msg(json.loads(line))
            except Exception as e:
                print(f"to_panel message failed: {e}: {line[:200]}", file=sys.stderr)
        self._update_pending()

    def _handle_msg(self, msg: dict):
        kind = msg.get("kind")
        now = datetime.datetime.now().isoformat()
        if kind == "reply":
            target, bid, entry = msg["target"], None, None
            if target.startswith("#") and target[1:].isdigit():
                entry, ev = self._seq_map.get(int(target[1:]), (None, {}))
                if entry is None or entry not in self._history:
                    self._notice(f"reply to {target} not shown: that event isn't from a card still in this panel "
                                 "(panel restarted, or card deleted)", text_was=msg["text"][:200])
                    return
                bid = ev.get("id") or None
            else:
                entry = self._entry_for_id(target)
                if entry is None:
                    self._notice(f"reply to '{target}' not shown: no block with that id in the panel",
                                 text_was=msg["text"][:200])
                    return
                bid = target
            entry.replies.append((bid, msg["text"], now))
            if self._shown(entry):
                self._js(f"IA.reply({json.dumps(bid)}, {json.dumps(msg['text'])}, {json.dumps(now)})")
        elif kind == "suggest":
            self._suggest(msg, now)
        elif kind == "edit":
            self._apply_edit(msg)
        elif kind == "tick":
            entry = self._entry_for_id(msg["id"])
            if entry is None:
                self._notice(f"tick: no checklist '{msg['id']}' in the panel")
                return
            entry.ticks.append((msg["id"], msg["item"], msg["state"]))
            if self._shown(entry):
                self._js(f"IA.tick({json.dumps(msg['id'])}, {json.dumps(msg['item'])}, {json.dumps(msg['state'])})")

    # ── rewrite proposals ─────────────────────────────────────────

    def _suggest(self, msg: dict, now: str):
        seq = int(msg["seq"])
        entry, ev = self._seq_map.get(seq, (None, None))
        if not ev or ev.get("type") != "rewrite":
            self._notice(f"suggest #{seq} dropped: not a rewrite request from this panel session")
            return
        if entry not in self._history:
            self._notice(f"suggest #{seq} dropped: its card was deleted")
            return
        if ev.get("rev") != entry.rev:
            self._notice(f"suggest #{seq} dropped: the card was revised (r{ev.get('rev')} → r{entry.rev}) after the "
                         "request, so the paragraph may have moved. Edit the card instead (panel.sh current / edit).")
            return
        para = int(ev["para"])
        if entry.pending_rw.get(para) not in (None, seq) and not any(
                s["seq"] == seq for s in entry.suggestions.values()):
            self._notice(f"suggest #{seq} dropped: a newer rewrite request #{entry.pending_rw[para]} "
                         "is waiting on that paragraph")
            return
        new, src = msg["text"].strip(), msg.get("src")
        cur = entry.suggestions.get(str(para))
        if cur and cur["seq"] == seq and cur["new"] == new:
            return                                   # identical retransmission
        if cur:                                      # replaced: the old proposal's decision must not carry over
            entry.decisions.pop(cur["id"], None)
            entry.replies = [r for r in entry.replies if r[0] != cur["id"]]
        entry.rw_count += 1
        bid = f"rw-{seq}-{entry.rw_count}"
        label = " ".join(ev.get("instruction", "").split())[:50]
        old_shown = src if src else ev["text"]
        html = md_to_html(f":::diff {bid} Rewrite ({label})\n{old_shown}\n===\n{new}\n:::\n")
        entry.suggestions[str(para)] = {"id": bid, "html": html, "seq": seq, "src": src, "new": new}
        entry.pending_rw.pop(para, None)
        if msg.get("note"):
            entry.replies.append((bid, msg["note"], now))
        if self._shown(entry):
            self._js(f"IA.suggest({para}, {json.dumps(html)}, false, {json.dumps(bool(cur))})")
            if msg.get("note"):
                self._js(f"IA.reply({json.dumps(bid)}, {json.dumps(msg['note'])}, {json.dumps(now)}, true)")

    def _commit_rewrite(self, entry: "HistoryEntry", sg: dict):
        """An approved rewrite becomes part of the card's markdown (checkout, Flag, Notes all see it)."""
        src = sg.get("src")
        if not src or entry.text.count(src) != 1:
            self._notice(f"rewrite {sg['id']} approved but not committed to the card: its source paragraph "
                         "couldn't be located exactly once. Apply it with panel.sh current / edit.")
            return
        prefix = re.match(r"\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX~]\]\s+)?|\d+[.)]\s+|>\s*)?", src).group(0)
        body = sg["new"]
        if prefix.strip() and not body.startswith(prefix.strip()):
            body = prefix + body
        self._apply_edit({"uid": entry.uid, "text": entry.text.replace(src, body, 1), "committed": sg["id"]})

    # ── in-place edits ────────────────────────────────────────────

    _WORD = {"approve": "approved", "changes": "requested changes", "reject": "rejected"}

    def _describe(self, ev: dict) -> str:
        t = ev.get("type")
        if t in ("approve", "diff"):
            out = self._WORD.get(ev.get("decision"), str(ev.get("decision")))
        elif t == "choice":
            out = "picked " + ", ".join(ev.get("picked") or []) if ev.get("picked") else "none of these"
        elif t == "ask":
            out = f"answered “{ev.get('answer', '')[:200]}”"
        elif t == "params":
            out = "sent values " + json.dumps(ev.get("values", {}))[:200]
        else:
            out = t or "responded"
        if ev.get("note"):
            out += f" — “{ev['note'][:300]}”"
        return out

    def _reconcile(self, entry: "HistoryEntry", new_blocks: list) -> set:
        """Retire decisions/pins/ticks on blocks whose source or attached files changed. Returns their ids."""
        if not entry.blocks:
            entry.blocks = list(new_blocks)
            return set()
        old = {b[1]: b for b in entry.blocks}
        new = {b[1]: b for b in new_blocks}
        changed = {bid for bid in old if bid not in new or new[bid] != old[bid]}
        for bid in changed:
            if bid in entry.decisions:
                entry.revised[bid] = self._describe(entry.decisions.pop(bid))
            if bid in entry.pins:
                entry.old_pins.setdefault(bid, []).extend(entry.pins.pop(bid))
        for bid in [b for b in entry.revised if b not in new]:
            entry.revised.pop(bid)
        entry.ticks = [t for t in entry.ticks if t[0] not in changed]
        entry.blocks = list(new_blocks)
        return changed

    def _apply_edit(self, msg: dict):
        target = msg.get("uid")
        if target in (None, "latest"):
            entry = self._history[-1] if self._history else None
        elif target == "shown":
            entry = self._history[self._pos] if self._pos >= 0 else None
        else:
            entry = self._entry_by_uid(target)
        if entry is None:
            self._notice(f"edit dropped: card {target} isn't in the panel (deleted, or the panel restarted). "
                         "Use panel.sh write for a new card.")
            return
        base = msg.get("base")
        if base is not None and base != entry.rev:
            self._notice(f"edit dropped: card {entry.uid} is at r{entry.rev} but the edit was made on r{base} "
                         "(it changed since your checkout). Run panel.sh current again and redo the change.")
            return
        text = msg["text"]
        render_body(text)
        changed = self._reconcile(entry, interactive.BLOCKS)
        if text == entry.text and not changed:
            if self._shown(entry):
                self._reload_keep_scroll()   # same markdown: still refresh pixels of recompiled files
            return
        entry.rev += 1
        # paragraph indices may have moved: unresolved rewrite requests and proposals can't be placed any more
        for sg in entry.suggestions.values():
            entry.decisions.pop(sg["id"], None)
        entry.suggestions.clear()
        entry.pending_rw.clear()
        if text != entry.text:
            entry.versions.append(entry.text)
            entry.text = text
            entry.edited_at = datetime.datetime.now()
        if msg.get("committed"):
            entry.replies.append((None, "Rewrite applied to the card.", datetime.datetime.now().isoformat()))
        if self._shown(entry):
            self._reload_keep_scroll()
        else:
            self._write_state()

    def _reload_keep_scroll(self):
        def done(wv, res, _data=None):
            try:
                self._restore_y = int(wv.evaluate_javascript_finish(res).to_double())
            except Exception:
                self._restore_y = 0
            self._render_current()
        try:
            self._webview.evaluate_javascript("window.scrollY", -1, None, None, None, done, None)
        except (AttributeError, TypeError):
            self._render_current()

    def _write_state(self):
        """Snapshot for `panel.sh current` / `edit`: which card is shown and every card's markdown."""
        state = {
            "shown_uid": self._history[self._pos].uid if self._pos >= 0 else None,
            "entries": [{
                "uid": e.uid, "rev": e.rev, "index": i + 1, "title": e.title, "time": e.timestamp_str(),
                "edited": e.edited_at.strftime("%H:%M:%S") if e.edited_at else None, "text": e.text,
            } for i, e in enumerate(self._history)],
        }
        try:
            tmp = events.STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, events.STATE)
        except OSError as e:
            print(f"state write failed: {e}", file=sys.stderr)

    # ── awaiting-you counter ──────────────────────────────────────

    def _pending_entries(self) -> list[tuple[int, int]]:
        base = lambda b: re.sub(r"-v\d+$", "", b)
        later: set[str] = set()
        out = []
        for i in range(len(self._history) - 1, -1, -1):
            e = self._history[i]
            ids = [b[1] for b in e.blocks if b[0] in interactive.DECISION_KINDS] + \
                  [sg["id"] for sg in e.suggestions.values()]
            n = sum(1 for b in ids if b not in e.decisions and base(b) not in later)
            if n:
                out.append((i, n))
            later |= {base(b) for b in e.ids()}
        return out

    def _update_pending(self):
        total = sum(n for _, n in self._pending_entries())
        if total:
            self._btn_pending.set_label(f"⏳ {total} awaiting you")
            self._btn_pending.show()
        else:
            self._btn_pending.hide()

    def _on_pending(self, _btn):
        pend = self._pending_entries()
        if not pend:
            return
        i = pend[0][0]   # most recent entry with something undecided
        self._scroll_pending = True
        if i != self._pos:
            self._pos = i
            self._render_current()
        else:
            self._js("IA.scrollToPending()")
            self._scroll_pending = False

    def _on_load_changed(self, _wv, event):
        if event == WebKit2.LoadEvent.FINISHED and self._scroll_pending:
            self._scroll_pending = False
            GLib.timeout_add(600, lambda: (self._js("IA.scrollToPending()"), False)[1])

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
            self._emit({"type": "mcq", **{k: data.get(k) for k in ("question", "selected", "correct")}})

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
            path = self._save_flagged(entry)
            btn.set_label("★ Flagged")
            self._emit({"type": "flag", "file": path})
        else:
            btn.set_label("★ Flag")

    def _save_flagged(self, entry: HistoryEntry):
        ts = entry.timestamp.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FLAGGED_DIR, f"{ts}.md")
        with open(path, "w") as f:
            f.write(f"<!-- flagged {entry.timestamp.isoformat()} -->\n\n")
            f.write(entry.text)
        return path

    def _on_notes(self, _btn):
        if self._pos < 0:
            return
        entry = self._history[self._pos]
        path = self._save_flagged(entry)
        entry.flagged = True
        self._btn_flag.handler_block_by_func(self._on_flag)
        self._btn_flag.set_active(True)
        self._btn_flag.set_label("★ Flagged")
        self._btn_flag.handler_unblock_by_func(self._on_flag)
        self._emit({"type": "to_notes", "file": path})
        self._js("IA.toast('Sent to Claude ✓ — filing into notes')")

    # ── rendering ────────────────────────────────────────────────

    def _render_current(self):
        if self._pos < 0:
            self._webview.load_html(
                HTML_TEMPLATE.format(body="<p style='color:#6986a7'>Waiting for response…</p>"),
                "file:///tmp/"
            )
            self._lbl_pos.set_text("—")
            self._lbl_time.set_markup("<small><i></i></small>")
            self._btn_prev.set_sensitive(False)
            self._btn_next.set_sensitive(False)
            self._btn_flag.set_sensitive(False)
            self._btn_delete.set_sensitive(False)
            self._btn_notes.set_sensitive(False)
            self._update_pending()
            self._write_state()
            return

        entry = self._history[self._pos]
        n = len(self._history)

        try:
            body = render_body(entry.text)
            if self._reconcile(entry, interactive.BLOCKS):
                entry.rev += 1      # a recompiled PDF/image: earlier sign-off no longer covers what's shown
            self._webview.load_html(
                build_page(entry.text, entry.mcq_answers or None, {
                    "card": entry.uid, "rev": entry.rev,
                    "decisions": entry.decisions, "pins": entry.pins, "suggestions": entry.suggestions,
                    "pending_rw": sorted(entry.pending_rw), "ticks": entry.ticks, "replies": entry.replies,
                    "cursor": self._seen, "revised": entry.revised, "old_pins": entry.old_pins,
                    "scroll": self._restore_y}, body=body),
                "file:///tmp/"
            )
            self._restore_y = 0
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._webview.load_html(
                HTML_TEMPLATE.format(body=f"<pre>Error: {e}</pre>"),
                "file:///tmp/"
            )

        self._lbl_pos.set_text(f"{self._pos + 1} / {n}")
        edited = f" · edited {entry.edited_at.strftime('%H:%M')}" if entry.edited_at else ""
        self._lbl_time.set_markup(f"<small><i>{entry.timestamp_str()}{edited}</i></small>")
        self._btn_prev.set_sensitive(self._pos > 0)
        self._btn_next.set_sensitive(self._pos < n - 1)
        self._btn_flag.set_sensitive(True)
        self._btn_delete.set_sensitive(True)
        self._btn_notes.set_sensitive(True)
        self._update_pending()
        self._write_state()

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
        self._drain_to_panel()
        return True


def check(md_text: str) -> int:
    """Lint a document the way the panel will render it; also warms the PDF cache."""
    interactive.begin_document()
    md_to_html(md_text)
    for w in dict.fromkeys(interactive.WARNINGS):
        print(f"panel warning: {w}")
    return 1 if interactive.WARNINGS else 0


def main():
    if sys.argv[1:2] == ["--check"]:
        sys.exit(check(sys.stdin.read()))
    GLib.set_prgname("latex-panel")     # dock matches this to latex-panel.desktop
    win = RendererWindow()
    try:
        import os as _os, sys as _sys
        _sys.path.insert(0, _os.path.expanduser("~/app-hub"))
        import apphub
        apphub.listen("maths", lambda m: win.present())
    except Exception:
        pass
    Gtk.main()


if __name__ == "__main__":
    main()
