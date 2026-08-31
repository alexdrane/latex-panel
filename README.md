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

### As a Claude Code skill (recommended)

Symlink this repo into your skills directory:
```bash
ln -sfn "$PWD" ~/.claude/skills/latex-panel
```

Claude then loads [`SKILL.md`](SKILL.md) automatically whenever a session turns to
maths/physics, revision, or quizzing. It launches the window itself (via
`scripts/panel.sh`, which is idempotent) and writes responses to it — no manual
startup, and the LaTeX/MCQ/diagram syntax reference travels with the skill.

```bash
scripts/panel.sh ensure   # start the window if not already running
scripts/panel.sh status   # running? / not running
scripts/panel.sh stop     # close it
```

### Standalone

Start the panel in the background:
```bash
latex-panel &
```

The panel watches `/tmp/claude_response.md`. Anything written there is rendered immediately.

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
