# latex-panel

A floating GTK window that renders markdown + LaTeX from Claude Code responses in real time.

Claude writes maths to `/tmp/claude_response.md`; the panel picks up changes every 500 ms and renders them via MathJax. Keeps a session history so you can navigate back through responses.

## Dependencies

- Python 3.9+
- GTK 3 + WebKit2GTK 4.1 (system packages)
- `markdown-it-py` (installed automatically)

On Ubuntu/Debian:
```bash
sudo apt install python3-gi gir1.2-webkit2-4.1
```

## Install

```bash
git clone https://github.com/alexdrane/latex-panel
cd latex-panel
pip install -e .
```

## Usage

Start the panel in the background:
```bash
latex-panel &
```

The panel watches `/tmp/claude_response.md`. Anything written there is rendered immediately. With Claude Code, configure it (or ask Claude) to write maths/physics responses to that file.

### Controls

| Button | Action |
|--------|--------|
| ◀ / ▶ | Navigate session history |
| ★ Flag | Save response to `~/.local/share/latex-panel/flagged/` |
| ✕ Delete | Remove entry from history |

## LaTeX notes

- Inline math: `$...$` or `\(...\)`
- Display math: `$$...$$`
- Use `\not p` not `\slashed{p}` — the slashed package is not supported in the browser renderer

## How it works

- File watcher polls `/tmp/claude_response.md` every 500 ms via `GLib.timeout_add`
- Markdown rendered by `markdown-it-py`; LaTeX stashed before markdown parsing to prevent escaping, then restored and handed to MathJax 3
- Flagged responses saved as timestamped `.md` files for later consolidation into notes
