---
name: latex-panel
description: Render markdown + LaTeX (and interactive MCQ quizzes, collapsible spoilers, TikZ/Feynman diagrams) in a floating always-on-top window on the user's desktop, updated live as the session goes. This is the default surface for any maths/physics working, derivations, revision, or quizzing - write there without being asked. The window is a GTK process the skill auto-launches; the skill also carries the syntax reference and renderer gotchas. Use whenever the user wants to see equations, work a derivation, revise a topic, or be quizzed. Optionally embeds Manim animations when something genuinely needs to move.
---

# latex-panel

A floating always-on-top window that live-renders markdown + LaTeX via MathJax.
The skill's job: make sure the window is running, then write content to the file
it watches (`/tmp/claude_response.md`). The user reads it on their second monitor
while the terminal stays for conversation.

## Launching and writing

One script does everything - `scripts/panel.sh` (idempotent, safe to call every turn):

```bash
S=~/.claude/skills/latex-panel/scripts/panel.sh

$S ensure                    # start the window if not already up
printf '%s' "$CONTENT" | $S write   # start if needed + render this markdown
$S status                    # running? / not running
$S stop                      # close it
```

Normal flow: pipe your markdown into `$S write`. It launches the panel on first
use and overwrites the watch file; the panel picks up the change within 500 ms and
pushes the previous response into its history (◀ / ▶ to navigate).

Write the **whole** response for that turn as one document - it replaces, not
appends. Prose the user needs stays in the terminal too; the panel is for the
maths-heavy artefact.

## When to use it

- Any derivation, proof, or multi-line algebra.
- Revision / study sessions - this is the default output, no need to ask.
- Quizzing the user (see MCQ syntax below - the panel scores decks automatically).
- Showing a diagram (TikZ, Feynman).

Do **not** use it for one-line answers, code, or shell output.

## Markdown / LaTeX syntax

- Inline math: `$...$` or `\(...\)`  ·  Display: `$$...$$`
- Standard commonmark otherwise; raw HTML passes through.
- **Use `\not p`, never `\slashed{p}`** - the slashed package is not in the browser renderer.
- Keep display math in `$$...$$`, one equation per block, so history diffs stay readable.

### Interactive MCQ

```
?? What is the trace of the 4x4 identity?
( ) 1
(*) 4
( ) 0
```

`(*)` marks the correct option; `( )` a distractor. Click-to-answer, colour
feedback, and results logged to `~/.local/share/latex-panel/quiz_results.jsonl`.
**Two or more `??` blocks in one document become a deck**: sticky progress bar,
keys 1-4 to answer, Enter / -> next, <- back, score screen at the end. Use this
for revision quizzes.

### Collapsible spoiler (hide a solution)

```
:::spoiler Show solution
The determinant is $ad - bc$ because ...
:::
```

### Diagrams

````
:::tikz
\begin{tikzpicture}
  \draw (0,0) -- (2,1);
\end{tikzpicture}
:::
````

`:::feynman ... :::` wraps the body in `tikzpicture`+`feynman` for you. Rendered
by TikZJax in the browser - only packages TikZJax bundles are available.

## Optional: Manim animations (`:::video`)

Everything above is unchanged and stays the default. Only when motion genuinely
explains something (a morph, a process unfolding, points shrinking) - or the user
asks for an animation - render a Manim scene and embed it in the same document:

```bash
S=~/.claude/skills/latex-panel/scripts/panel.sh
LINE=$($S manim [-q l|m|h] [-s Scene] <<'PY'
from manim import *
class Demo(Scene):
    def construct(self):
        self.play(Write(MathTex(r"e^{i\pi}+1=0")), run_time=2); self.wait(2)
PY
)   # prints e.g. ":::video /home/.../media/<hash>.mp4"
printf '%s\n\n%s\n' "Text and maths as usual" "$LINE" | $S write
```

- `$S manim` renders (blocking, cached by scene hash) and prints a `:::video <abs path> [caption]`
  line. Put that line in the markdown you `write`; the panel shows a looping video with
  0.25-1x speed buttons (default 0.75x) and a replay button. It mixes freely with maths/MCQs
  and works with history and Flag.
- **Failed renders are never shown**: the error goes to stderr and exit is 1 - fix and re-run.
- Quality `m` (720p30) default; avoid `l` (15fps looks choppy/fast). Draft is fine at `m`.
- Pacing: users find default Manim too fast. Use `run_time` 1.5-3 s per beat, `self.wait(1-2)`
  between ideas, long final hold. Keep scenes 5-30 s. Silent output.
- Manim CE v0.20 (not manimgl) lives in `~/.local/share/manim-panel/env` (conda-forge via
  micromamba; pip can't build ManimPango here). Ad hoc use needs `PYTHONNOUSERSITE=1`.
  Needs LaTeX for `MathTex` (installed). Gotchas: `Square(side_length=)`, `np.trapezoid`
  (no `trapz`), `plot_line_graph(...)["line_graph"]` for a single VMobject.
- Videos are saved in `~/.local/share/latex-panel/media/` (with the scene `.py`), so
  flagged notes keep working. The running panel must be restarted once
  (`panel.sh stop; panel.sh ensure`) to pick up `:::video` support.

## Flagging for notes

The window's **★ Flag** button saves the current response to
`~/.local/share/latex-panel/flagged/<timestamp>.md` for later consolidation into
Obsidian notes. When the user says "flag that" they may click it themselves, or
ask you to - a flagged file is just the raw markdown you wrote.

## If it won't start

Needs system GTK: `python3-gi` + `gir1.2-webkit2-4.1`, and `markdown-it-py`.
Startup errors go to `/tmp/latex-panel.log`. The renderer also still installs as
the `latex-panel` console script (`pip install -e ~/latex-panel`) for manual use.
