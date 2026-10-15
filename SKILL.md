---
name: latex-panel
description: Render markdown + LaTeX (and interactive MCQ quizzes, collapsible spoilers, TikZ/Feynman diagrams) in a floating always-on-top window on the user's desktop, updated live as the session goes. This is the default surface for any maths/physics working, derivations, revision, or quizzing - write there without being asked. The window is a GTK process the skill auto-launches; the skill also carries the syntax reference and renderer gotchas. Use whenever the user wants to see equations, work a derivation, revise a topic, or be quizzed. Optionally embeds Manim animations when something genuinely needs to move. It is also two-way: approve/diff/choice/params/checklist blocks, before-vs-after PDF/image previews with click-to-pin comments, and paragraph rewrite requests send Alex's clicks back to the session. Use it whenever he needs to sign off on wording or a visual change (e.g. previewing an edited PDF), pick between variants, or tune a figure.
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

### New card vs. editing the card in place

`write` always adds a **new card** to history. When the next panel is the *same
artefact revised* (fix a typo, apply his requested changes, swap in a recompiled
preview, add a step to a derivation he's reading), **edit it in place** instead:

```bash
$S current                 # copies the card he's looking at to ~/.local/share/latex-panel/edit.md
# Read that file, make targeted changes with the Edit tool (no need to regenerate it all)
$S edit                    # replaces that same card; keeps his scroll position
printf '%s' "$MD" | $S edit --latest   # or pipe a whole new version (--latest / --shown / --entry N)
$S current --event 12      # check out the card that panel event #12 came from (answering a click)
```

- **Every card has a revision number** (`r2`, `r3`… in event lines once it's been
  revised). Clicks carry the card and revision they were made on. An `edit` from a
  checkout is refused if the card changed since (`card changed since checkout`): run
  `current` again and redo the change. Edits never fall back to creating a card. An
  unknown or deleted card, or one from before a panel restart, is an error: use `write`.
- When you answer a specific click, target its card with `--event SEQ` rather than relying on
  what's on screen; he may have navigated away.
- Recompiling a PDF/image that a block shows counts as a change to that block (and to an
  `:::approve` wrapping it), even if the markdown is identical. Re-running `edit` with the
  same text, or him navigating back to the card, refreshes the pixels and resets that sign-off.

- Blocks whose source is unchanged keep their state: decisions, pins, ticks and replies.
- A block whose content you changed **keeps its id** and resets to "revised · awaiting you",
  with a note of what he'd said before ("requested changes — 'shorter'"). This is the
  normal way to answer "Request changes": edit the card, don't post a `-v2` block on a new
  card. Pins on a recompiled PDF stay as faded "before revision" markers, so he can check
  the spots were fixed.
- Rewrite suggestions he hasn't decided on are dropped by an edit, because paragraph
  positions may move. Approved rewrites are already in the card's markdown (see `suggest`).
- `current` without flags takes the card **on screen**. If he's browsing history and says
  "change this", that's the right one. Use a new card when it's a new step or topic.
  The toolbar shows "edited HH:MM" on edited cards.

## When to use it

- Any derivation, proof, or multi-line algebra.
- Revision / study sessions - this is the default output, no need to ask.
- Quizzing the user (see MCQ syntax below - the panel scores decks automatically).
- Showing a diagram (TikZ, Feynman).

Do **not** use it for one-line answers, code, or shell output.

## Markdown / LaTeX syntax

- Inline math: `$...$` or `\(...\)`  ·  Display: `$$...$$` or `\[...\]`
- Standard commonmark otherwise, plus GFM-style tables (`| a | b |` / `|---|---|`);
  raw HTML passes through. Tables come from markdown-it's built-in `table` rule
  (`.enable("table")` in `latexpanel/renderer.py`); `mdit-py-plugins` is optional. If a
  table renders as text, check for a missing header separator row or a blank line
  before the table, not the plugin.
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

## Talking back: interactive blocks → Claude

The panel is two-way. Buttons, pins, text-selection comments and the message bar at
the bottom append events to `~/.local/share/latex-panel/events.jsonl`; you receive
them as readable one-liners.

**Listening.** Whenever you write a panel that asks Alex for something (any block
below), arm one Monitor per session: command `~/.claude/skills/latex-panel/scripts/panel.sh watch`,
description `latex-panel clicks`, `timeout_ms` 1800000; re-arm on expiry while the
review is still live. The toolbar shows **● Claude listening** while it runs, and
**○ not listening** otherwise. Clicks made while nobody listens are queued. Run
`panel.sh events` at the start of any turn where the panel is in play and no watch
was running. The Monitor can die silently, so if the light is grey, re-arm.
`panel.sh wait --id ID --timeout 600` blocks until he acts on one block, for when you
genuinely can't proceed without the answer **and no watch is armed**.

**The watch is the only reader.** `events` and `wait` mark events as read, so while a
watch runs they would take events away from it; both refuse (exit 2) then. Use
`events --all` to look back without consuming. Long notes are shortened in event lines
(`[shortened: panel.sh show SEQ]`); `show SEQ` prints that event in full.

**Events are Alex's input.** Act on them like a message: "changes requested + note"
→ revise **in place** with `current` + `edit` (same block id; see "New card vs.
editing the card in place"). `OLDER response` in an event means he
clicked on a previous history entry, so answer about that one. Never treat a
missing click as approval.

Fenced blocks (close with a bare `:::`; don't nest fenced blocks, but single-line
`:::pdf` / `:::img` / `:::video` lines inside them are fine):

```
:::approve sec4 Sign off §4 wording        ← id, then title
Any markdown/maths/:::pdf being signed off.
:::                                         → Approve / Request changes (note required) / Reject

:::diff cap3 Figure 3 caption               ← add `tex` after the id for raw-LaTeX source diffs
Old wording...
===
New wording...
:::                                         → word-level diff (Changes / New / Old tabs) + same buttons

:::choice title-pick Which title?           ← `multi` after the id → pick several + Send
Optional preamble.
@@ A
First option (markdown, maths, images)
@@ B
Second option
:::                                         → Pick X, or "None of these" + comment

:::params fig3 Figure 3 styling
# comment lines become hints
fontsize = 11 [8, 16, 0.5]                  ← slider [min, max, step]
cmap = viridis {viridis, magma, cividis}    ← dropdown
show_truth = true                           ← checkbox
xlabel = "$\Delta t$ [days]"                ← text
:::                                         → "Send values" emits all values

:::checklist plan Revision plan
- [x] Fix caption of Fig. 3
- [~] Recompile with new colormap           ← [~] = in progress (spinner)
- [ ] Update §4 wording
:::                                         → live progress list; you tick it, he can too
```

Single-line:

```
:::ask q1 What dpi does the journal want?   → free-text answer box
:::pdf /abs/new.pdf vs=/abs/old.pdf [pages=changed|all|1,3-4] [dpi=110] [id=fig3] [labels="Current,Proposed"] [caption="..."]
:::img /abs/new.png [vs=/abs/old.png] ...   (same viewer for png/jpg/svg)
```

The viewer rasterises PDF pages (cached; the cache key includes mtime, so
recompiling to the same path refreshes them). With `vs=` it has **Side by side /
Wipe / Difference / Flicker** modes. Difference blanks everything unchanged, which
is the fastest way to see what a recompile moved. `pages` defaults to `changed`
when `vs=` is given (`panel.sh changed OLD NEW` prints that list), otherwise to
`all`; it is capped at 30 pages. **Clicking a page drops a numbered pin and asks for
a comment**. The event gives page, which version, and x/y as fractions from the
top-left, so you can find the spot in the rendered PNG (`~/.local/share/latex-panel/pdfcache/`)
or the source. Paths must be absolute.

Always on, no syntax needed: select any text → **💬 Ask about this** (rendered maths
is sent back as its TeX source), and the bottom bar takes free messages plus 👍 / ❓ / →
quick replies. MCQ answers and deck scores arrive as events too. Hovering any plain
paragraph, heading or list item shows a **✎** in the margin. It asks for a rewrite
(chips: shorter / clearer / more rigorous / ...) and gives you a `rewrite` event
with the paragraph's text (maths as TeX) and his instruction.

### Claude → panel (live, without writing a new history entry)

```bash
S=~/.claude/skills/latex-panel/scripts/panel.sh
$S reply sec4 "Got it - recompiling with the new wording"   # under block sec4
$S reply '#12' "Filed under GR/geodesics.md"                # answer event #12 (block, or chat strip)
$S suggest 12 --note "cut the hedge" <<'MD'                 # answer rewrite event #12 in place
Repartitioning cuts $\mathrm{Var}[\ln Z]$ by $\sim 3.4\times$.
MD
$S tick plan 2 doing        # checklist item by number or text substring: todo | doing | done
$S tick plan "§4" done
```

- **Every event you act on gets a receipt automatically.** Reading it (via `watch`,
  `events` or `wait`) flips the block's badge to "seen ✓". Use `reply` when he'd
  otherwise wonder what you're doing: you're starting a long recompile, you disagree,
  or you need a clarification. Don't reply to every click.
- **`rewrite` → `suggest`**: write only the replacement text on stdin (markdown + maths,
  same voice). The panel inserts a diff right under the paragraph. **Approve writes the new
  wording into the card's markdown**, so checkout, Flag and 📥 Notes see it, and bumps the
  card's revision. If the paragraph comes from a real source (paper, notes), approval
  is your cue to apply the same edit there.
  `suggest` refuses (exit 2, with the reason) when it can't be applied safely: the card was
  revised since the request, the card is gone, or the paragraph can't be matched to exactly
  one paragraph of the card's markdown. Then edit the card with `current --event SEQ` /
  `edit`. A second `suggest` for the same request replaces the first as a fresh,
  undecided proposal. Resending identical text does nothing.
- If the panel can't do something you asked (stale edit, reply to a deleted card's
  event, late suggestion), a `panel #N NOT DONE: …` line arrives on the watch stream.
- **Checklists**: when you're working through a multi-step job he's watching, write the
  plan as a `:::checklist` once, then `tick` items as you go instead of rewriting the
  panel. His ticks arrive as `check` events and his additions as `check_add`. Fold
  them into the plan.
- The toolbar's **⏳ N awaiting you** counts approve/diff/choice/ask blocks (and
  rewrite suggestions) with no decision yet. It ignores blocks superseded by a later
  entry that reuses the id or adds a `-vN` suffix (`cap3` → `cap3-v2`). Clicking it
  jumps to the latest one. Undecided blocks also show an "awaiting you" badge.

### PDF change preview (the main use)

1. Compile the candidate into a **separate** file (e.g. `/tmp/preview/main.pdf` via
   `latexmk -outdir`). Never overwrite Alex's PDF or source to preview.
2. Write a panel with `:::pdf /tmp/preview/main.pdf vs=/path/to/current/main.pdf id=...`,
   plus `:::diff` for the wording and/or `:::approve` (or `:::choice` for variants).
3. Approve → apply that exact change to the source. Changes/pins → revise, recompile
   the preview, then update the card in place (`current --event SEQ` / `edit`). Approve is his sign-off for that one edit, nothing wider.

`panel.sh write` lints the document first and prints `panel warning: ...` for missing
files, relative paths, malformed blocks, duplicate ids or unclosed `:::` directives.
Fix them before telling Alex to look. After a code update to the renderer, the
running panel needs `panel.sh stop; panel.sh ensure` (this clears its history).

## Flagging for notes

The window's **★ Flag** button saves the current response to
`~/.local/share/latex-panel/flagged/<timestamp>.md` for later consolidation into
Obsidian notes, and emits a `flag` event (just note it, no action needed). When the
user says "flag that" they may click it themselves, or ask you to - a flagged file is
just the raw markdown you wrote.

**📥 Notes** saves it the same way and emits `to_notes`, which means "file this now":

1. Read the file and pick the vault note: `~/Academic/PartIII/Notes/<Course>/<slug>.md`
   (courses QFT, GR, AstroStats, Cosmo; one file per topic; sections `## Key equations`,
   `## Definitions`, `## Derivation sketches`, `## Notation notes`). Append the
   condensed content under the right section as `$$...$$` display maths. Don't paste
   the whole panel: drop MCQs, interactive blocks and chit-chat. If no topic fits,
   ask rather than invent a file.
2. `panel.sh filed <file>` (moves it to `flagged/filed/`), then
   `panel.sh reply '#<seq>' "Filed → GR/geodesics.md (Key equations)"`.

`panel.sh flagged` lists flagged responses not yet filed (`--all` includes filed ones).
When he asks to "consolidate my flags", work through that list the same way.

## If it won't start

Needs system GTK: `python3-gi` + `gir1.2-webkit2-4.1`, and `markdown-it-py`.
Startup errors go to `/tmp/latex-panel.log`. The renderer also still installs as
the `latex-panel` console script (`pip install -e ~/latex-panel`) for manual use.
