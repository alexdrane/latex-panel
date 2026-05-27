#!/usr/bin/env python3
"""Floating WebKit2GTK window that renders markdown+LaTeX responses."""

import sys
import os
import json
import datetime
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, WebKit2, GLib

WATCH_FILE = "/tmp/claude_response.md"
FLAGGED_DIR = os.path.expanduser("~/.local/share/latex-panel/flagged")

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
<style>
  body {{
    font-family: 'Linux Libertine', Georgia, serif;
    font-size: 15px;
    line-height: 1.6;
    max-width: 720px;
    margin: 20px auto;
    padding: 0 24px 40px;
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
    font-size: 13px;
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
</style>
</head>
<body>{body}</body>
</html>
"""


def md_to_html(text: str) -> str:
    import re
    from markdown_it import MarkdownIt

    stash: dict[str, str] = {}
    counter = [0]

    def stash_match(m: re.Match) -> str:
        key = f"MATHSTASH{counter[0]}Z"
        stash[key] = m.group(0)
        counter[0] += 1
        return key

    text = re.sub(r'\$\$[\s\S]+?\$\$', stash_match, text)
    text = re.sub(r'\$[^$\n]+?\$', stash_match, text)

    md = MarkdownIt("commonmark", {"html": True})
    html = md.render(text)

    for key, val in stash.items():
        html = html.replace(key, val)

    return html


def build_page(md_text: str) -> str:
    body = md_to_html(md_text)
    return HTML_TEMPLATE.format(body=body)


class HistoryEntry:
    def __init__(self, text: str):
        self.text = text
        self.timestamp = datetime.datetime.now()
        self.flagged = False

    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")


class RendererWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Claude")
        self.set_default_size(760, 720)
        self.set_keep_above(True)
        self.set_decorated(True)

        os.makedirs(FLAGGED_DIR, exist_ok=True)

        self._history: list[HistoryEntry] = []
        self._pos = -1  # index into history; -1 = empty
        self._mtime = None

        # ── toolbar ──────────────────────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_border_width(4)

        self._btn_prev = Gtk.Button(label="◀")
        self._btn_prev.set_tooltip_text("Previous response")
        self._btn_prev.connect("clicked", self._on_prev)
        self._btn_prev.set_sensitive(False)
        toolbar.pack_start(self._btn_prev, False, False, 0)

        self._btn_next = Gtk.Button(label="▶")
        self._btn_next.set_tooltip_text("Next response")
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

        # spacer
        toolbar.pack_start(Gtk.Label(), True, True, 0)

        self._btn_flag = Gtk.ToggleButton(label="★ Flag")
        self._btn_flag.set_tooltip_text("Flag this response as useful for notes")
        self._btn_flag.connect("toggled", self._on_flag)
        self._btn_flag.set_sensitive(False)
        toolbar.pack_end(self._btn_flag, False, False, 0)

        self._btn_delete = Gtk.Button(label="✕ Delete")
        self._btn_delete.set_tooltip_text("Remove this response from history")
        self._btn_delete.connect("clicked", self._on_delete)
        self._btn_delete.set_sensitive(False)
        toolbar.pack_end(self._btn_delete, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)

        # ── webview ──────────────────────────────────────────────
        self._webview = WebKit2.WebView()
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

        self._load_file()
        GLib.timeout_add(500, self._poll)
        self.connect("destroy", Gtk.main_quit)
        self.show_all()

    # ── nav ──────────────────────────────────────────────────────

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
            self._webview.load_html(build_page(entry.text), "file:///tmp/")
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

        # sync flag button without re-firing toggled signal
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

        # Only add if content actually changed from last history entry
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
