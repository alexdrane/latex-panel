"""Interactive blocks that talk back to Claude.

Every button here posts a `claudeEvent` message to the GTK side, which appends it to
events.jsonl (see events.py); a Claude session streams that file with `panel.sh watch`.

Fenced (closed by a bare `:::` line):
    :::approve ID Title          content + Approve / Request changes / Reject + note
    :::diff ID [tex] Title       old text, a line `===`, new text -> word diff + same buttons
    :::choice ID [multi] Title   options introduced by `@@ Label` lines -> pick one (or several)
    :::params ID Title           `name = value [min, max, step]` / `{a, b}` / true / "text" lines
    :::checklist ID Title        `- [ ]` / `- [~]` (doing) / `- [x]` items; Claude ticks them live
    :::gallery ID Title          multi-image viewer: `cols: A | B | ...`, optional `alt: name`, then rows
                                 `# Row title` / `/abs/a.png | /abs/b.png | ...` / optional `alt: /a2.png | ...`
Single line:
    :::ask ID Prompt text        free-text answer box
    :::pdf PATH [vs=PATH] [pages=1,3-4|all|changed] [dpi=N] [id=ID] [labels="A,B"] [caption="..."]
    :::img PATH [vs=PATH] ...    same viewer for png/jpg/svg
"""
from __future__ import annotations

import difflib
import hashlib
import html as H
import os
import re
import shlex
import subprocess

CACHE = os.path.expanduser("~/.local/share/latex-panel/pdfcache")
MAX_PAGES = 30

WARNINGS: list[str] = []
BLOCKS: list[tuple[str, str, str]] = []   # (kind, id, source hash) in the last rendered document
DECISION_KINDS = ("approve", "diff", "choice", "ask")   # blocks that wait on Alex
_ids: set[str] = set()
_auto = [0]
_cur_src = [""]   # raw source of the block being parsed, hashed so in-place edits can tell what changed


def _h(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def begin_document() -> None:
    WARNINGS.clear()
    BLOCKS.clear()
    _ids.clear()
    _auto[0] = 0


def _warn(msg: str) -> str:
    WARNINGS.append(msg)
    return f'<div class="ia-err">⚠ {H.escape(msg)}</div>'


def _split_head(kind: str, head: str, flags: tuple[str, ...] = ()) -> tuple[str, set[str], str]:
    toks = head.strip().split(None, 1)
    if toks and re.fullmatch(r"[A-Za-z0-9_.\-]+", toks[0]):
        bid, rest = toks[0], (toks[1] if len(toks) > 1 else "")
    else:
        _auto[0] += 1
        bid, rest = f"{kind}-{_auto[0]}", head.strip()
    got = set()
    while True:
        t = rest.split(None, 1)
        if t and t[0] in flags:
            got.add(t[0])
            rest = t[1] if len(t) > 1 else ""
        else:
            break
    if bid in _ids:
        WARNINGS.append(f"duplicate block id '{bid}' - clicks on both report the same id")
    _ids.add(bid)
    BLOCKS.append((kind, bid, _h(_cur_src[0] + _asset_fp(_cur_src[0]))))
    return bid, got, rest.strip()


def _asset_fp(src: str) -> str:
    """Fingerprints of files a block shows (nested :::pdf/:::img), so a recompile invalidates its sign-off."""
    out = []
    for m in re.finditer(r"^:::(?:pdf|img)[ \t]+(.+)$", src, re.M):
        try:
            toks = shlex.split(m.group(1))
        except ValueError:
            continue
        paths = [toks[0]] if toks else []
        paths += [t[3:] for t in toks[1:] if t.startswith("vs=")]
        for p in paths:
            try:
                out.append(_key(os.path.expanduser(p)))
            except OSError:
                out.append("missing:" + p)
    return "|".join(out)


def _open(kind: str, bid: str, title: str, extra: str = "") -> str:
    return (f'<div class="ia ia-{kind}" data-ia-kind="{kind}" data-ia-id="{H.escape(bid, True)}" '
            f'data-title="{H.escape(title, True)}"{extra}>'
            f'<div class="ia-head"><span class="ia-title">{H.escape(title)}</span>'
            f'<span class="ia-status"></span></div>')


def _decision_foot(placeholder: str = "Note for Claude (required for “Request changes”)…") -> str:
    return (f'<div class="ia-foot"><textarea rows="2" placeholder="{placeholder}"></textarea>'
            '<div class="ia-actions">'
            '<button class="ia-btn ok" data-act="approve">✓ Approve</button>'
            '<button class="ia-btn mid" data-act="changes">✎ Request changes</button>'
            '<button class="ia-btn bad" data-act="reject">✕ Reject</button>'
            '</div></div>')


def _editor(text: str, base: str, opt: str | None = None) -> str:
    """The plain-text editor for a proposal: a textarea (never markdown-rendered, so LaTeX round-trips),
    a live word diff, the edited -> sent -> replied steps and the Send button. Behaviour lives in the page JS."""
    o = "" if opt is None else f' data-opt="{H.escape(opt, True)}"'
    return (f'<div class="ia-ed" data-orig="{H.escape(text, True)}" data-base="{H.escape(base, True)}"{o}>'
            '<div class="ia-edrich" contenteditable="true" spellcheck="true" role="textbox" aria-multiline="true" '
            'aria-label="Edit the proposed wording (rendered)"></div>'
            f'<textarea class="ia-edta" data-ia-edit spellcheck="false" rows="6" hidden '
            f'aria-label="Edit the proposed wording (plain text)">\n{H.escape(text)}</textarea>'
            '<div class="ia-edbar"><div class="ia-edsteps" hidden><span data-s="1">Edited</span><span data-s="2">Sent for review</span>'
            '<span data-s="3">Claude replied</span><span data-s="4">Approve your version</span></div>'
            '<div class="ia-edactions"><button type="button" class="ia-btn" data-act="ed-src" title="Show or hide the raw LaTeX">Source</button>'
            '<button type="button" class="ia-btn" data-act="ed-reset" hidden>Reset</button>'
            '<button type="button" class="ia-btn mid" data-act="ed-send" disabled>Send for review</button></div></div>'
            '<div class="ia-edlive ia-src tex2jax_ignore" hidden></div>'
            '<div class="ia-edalt tex2jax_ignore" hidden></div></div>')


# ── word diff ────────────────────────────────────────────────────────────────

_TOK = re.compile(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\s+|[^\s$]+|\$")


def _wrap(tag: str, s: str) -> str:
    # never let a tag span a paragraph break (breaks markdown block parsing)
    parts = re.split(r"(\n[ \t]*\n)", s)
    return "".join(p if (i % 2 or not p.strip()) else
                   re.sub(r"^(\s*)([\s\S]*?)(\s*)$", rf"\1<{tag}>\2</{tag}>\3", p)
                   for i, p in enumerate(parts))


def word_diff(old: str, new: str, esc: bool) -> str:
    a, b = _TOK.findall(old), _TOK.findall(new)
    f = H.escape if esc else (lambda s: s)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.45:   # mostly rewritten: interleaved words are unreadable, show whole blocks
        return _wrap("del", f(old)) + "\n\n" + _wrap("ins", f(new))
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        A, B = f("".join(a[i1:i2])), f("".join(b[j1:j2]))
        if op == "equal":
            out.append(A)
        if op in ("delete", "replace"):
            out.append(_wrap("del", A))
        if op in ("insert", "replace"):
            out.append(_wrap("ins", B))
    return "".join(out)


# ── pdf / image rasterising ──────────────────────────────────────────────────

def _key(path: str, *extra) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{extra}".encode()).hexdigest()[:16]


def pdf_npages(path: str) -> int:
    out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def render_page(path: str, page: int, dpi: int) -> str:
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, f"{_key(path, page, dpi)}.png")
    if not os.path.exists(out):
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
                        "-singlefile", path, out[:-4]], check=True, capture_output=True, timeout=120)
    return out


def changed_pages(old: str, new: str, dpi: int = 36) -> list[int]:
    na, nb = pdf_npages(old), pdf_npages(new)
    out = []
    for p in range(1, max(na, nb) + 1):
        if p > na or p > nb:
            out.append(p)
            continue
        ha = hashlib.md5(open(render_page(old, p, dpi), "rb").read()).digest()
        hb = hashlib.md5(open(render_page(new, p, dpi), "rb").read()).digest()
        if ha != hb:
            out.append(p)
    return out


def _parse_pages(spec: str, n: int) -> list[int]:
    if spec == "all":
        return list(range(1, n + 1))
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages += list(range(int(lo), int(hi) + 1))
        else:
            pages.append(int(part))
    return pages


def _src(path: str) -> str:
    """file:// URL with an mtime query so a rewritten file isn't served stale."""
    return "file://" + H.escape(path, True) + f"?v={_key(path)}"


def viewer(kind: str, args: str) -> str:
    try:
        toks = shlex.split(args)
    except ValueError as e:
        return _warn(f":::{kind} {args}: bad quoting ({e})")
    if not toks:
        return _warn(f":::{kind} needs a path")
    opts = dict(t.split("=", 1) for t in toks[1:] if "=" in t)
    path = os.path.expanduser(toks[0])
    vs = os.path.expanduser(opts["vs"]) if "vs" in opts else None
    for p in filter(None, (path, vs)):
        if not os.path.isabs(p):
            return _warn(f":::{kind} {p}: use an absolute path (the panel's cwd is not yours)")
        if not os.path.exists(p):
            return _warn(f":::{kind} {p}: file not found")
    try:
        dpi = int(opts.get("dpi", 110))
        if not 30 <= dpi <= 400:
            raise ValueError
    except ValueError:
        WARNINGS.append(f":::{kind} {toks[0]}: dpi={opts['dpi']!r} isn't a number in 30-400; using 110")
        dpi = 110
    is_pdf = path.lower().endswith(".pdf")
    _auto[0] += 1
    bid = opts.get("id") or f"view-{_auto[0]}"
    _ids.add(bid)
    # a recompiled file changes the hash too, so pins on the old render get marked stale
    BLOCKS.append(("view", bid, _h(args + "|" + _key(path) + "|" + (_key(vs) if vs else ""))))
    labels = [s.strip() for s in opts.get("labels", "Current,Proposed").split(",")] + ["", ""]
    lab_old, lab_new = (labels[0] or "Current"), (labels[1] or "Proposed")
    title = opts.get("caption") or os.path.basename(path) + (f"  vs  {os.path.basename(vs)}" if vs else "")
    note = ""

    if is_pdf:
        try:
            n = pdf_npages(path)
            spec = opts.get("pages") or ("changed" if vs else "all")
            if spec == "changed":
                if not vs:
                    return _warn(f":::{kind} pages=changed needs vs=")
                pages = changed_pages(vs, path)
                if not pages:
                    note = '<div class="ia-hint">No visual differences between the two PDFs - showing page 1.</div>'
                    pages = [1]
            else:
                pages = _parse_pages(spec, n)
            if len(pages) > MAX_PAGES:
                note += f'<div class="ia-hint">Showing first {MAX_PAGES} of {len(pages)} pages.</div>'
                pages = pages[:MAX_PAGES]
            nv = pdf_npages(vs) if vs else 0
            imgs = []
            for p in pages:
                new = render_page(path, p, dpi) if p <= n else None
                old = render_page(vs, p, dpi) if (vs and p <= nv) else None
                imgs.append((p, old, new))
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            return _warn(f":::{kind} {path}: render failed ({e})")
    else:
        imgs = [(1, vs, path)]

    attrs = (f' data-path="{H.escape(path, True)}" data-vs="{H.escape(vs or "", True)}"'
             f' data-mode="{"side" if vs else "single"}" data-lab-old="{H.escape(lab_old, True)}"'
             f' data-lab-new="{H.escape(lab_new, True)}"')
    out = [_open("view", bid, title, attrs)]
    bar = ['<div class="ia-vbar">']
    if vs:
        for m, lab in (("side", "Side by side"), ("wipe", "Wipe"), ("diff", "Difference"), ("flicker", "Flicker")):
            bar.append(f'<button class="ia-mode{" on" if m == "side" else ""}" data-act="mode" data-mode="{m}">{lab}</button>')
    bar.append('<span class="ia-sp"></span><button data-act="zoom" data-dz="-0.25">−</button>'
               '<button data-act="zoom" data-dz="0.25">+</button>'
               '<span class="ia-hint">click the page to pin a comment</span></div>')
    out.append("".join(bar) + note)

    def img(p: str | None) -> str:
        return f'<img src="{_src(p)}" draggable="false">' if p else '<div class="ia-missing">no such page</div>'

    for p, old, new in imgs:
        pl = f'<div class="ia-ptitle">page {p}</div>' if is_pdf else ""
        if vs:
            stack = ('<input type="range" class="ia-wipe" min="0" max="100" value="50" aria-label="Wipe position">'
                     f'<div class="ia-stack ia-pinnable" data-which="stack">{img(old)}'
                     f'{img(new).replace("<img ", "<img class=\"b\" ", 1)}'
                     f'<span class="ia-flabel"></span></div>')
            out.append(
                f'<div class="ia-page" data-page="{p}">{pl}'
                f'<div class="ia-side"><figure class="ia-pinnable" data-which="old">'
                f'<figcaption>{H.escape(lab_old)}</figcaption>{img(old)}</figure>'
                f'<figure class="ia-pinnable" data-which="new">'
                f'<figcaption>{H.escape(lab_new)}</figcaption>{img(new)}</figure></div>'
                f'<div class="ia-stackwrap">{stack}</div></div>')
        else:
            out.append(f'<div class="ia-page" data-page="{p}">{pl}'
                       f'<div class="ia-single"><figure class="ia-pinnable" data-which="single">{img(new)}</figure></div></div>')
    out.append("</div>")
    return "".join(out)


# ── gallery (multi-image viewer) ─────────────────────────────────────────────

def gallery(bid: str, title: str, body: str) -> str:
    """Rows of images that share one set of column labels (e.g. a figure x colour scheme), with an
    optional alternate layer per image (e.g. a colour-blindness simulation). Views: single image
    (flip with keys), grid (all columns of one row), matrix (everything as thumbnails)."""
    cols, alt_name, rows, cur = [], "", [], None

    def paths(s: str, where: str) -> list[str] | None:
        ps = [os.path.expanduser(x.strip()) for x in s.split("|")]
        for q in ps:
            if q and not os.path.isabs(q):
                WARNINGS.append(f"gallery '{bid}' {where}: {q}: use an absolute path"); return None
            if q and not os.path.exists(q):
                WARNINGS.append(f"gallery '{bid}' {where}: {q}: file not found"); return None
        return ps

    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("cols:"):
            cols = [c.strip() for c in s[5:].split("|")]
        elif s.startswith("#"):
            cur = {"title": s.lstrip("#").strip(), "main": None, "alt": None}; rows.append(cur)
        elif s.lower().startswith("alt:"):
            rest = s[4:].strip()
            if cur is None or "/" not in rest:
                alt_name = rest
            elif (ps := paths(rest, f"row '{cur['title']}' alt")) is not None:
                cur["alt"] = ps
        elif cur is not None and cur["main"] is None:
            ps = paths(s, f"row '{cur['title']}'")
            if ps is not None:
                cur["main"] = ps
        else:
            WARNINGS.append(f"gallery '{bid}': ignoring line {s[:80]!r}")
    rows = [r for r in rows if r["main"]]
    if not cols or not rows:
        return _warn(f"gallery '{bid}': needs a `cols: A | B` line and at least one `# Row` with image paths")
    n = len(cols)
    has_alt = any(r["alt"] for r in rows)
    BLOCKS.append(("gallery", bid, _h(body + "|" + "|".join(_key(q) for r in rows for q in (r["main"] + (r["alt"] or [])) if q))))

    def url(q: str) -> str:
        return _src(q) if q else ""

    data = []
    for i, r in enumerate(rows):
        main = (r["main"] + [""] * n)[:n]
        alt = ((r["alt"] or []) + [""] * n)[:n]
        data.append(f'<div class="ia-grow" data-row="{i}" data-title="{H.escape(r["title"], True)}">'
                    + "".join(f'<span data-c="{j}" data-src="{url(a)}" data-alt="{url(b)}"></span>'
                              for j, (a, b) in enumerate(zip(main, alt))) + "</div>")
    rbtn = "".join(f'<button data-act="grow" data-row="{i}"{" class=on" if i == 0 else ""}>{H.escape(r["title"])}</button>'
                   for i, r in enumerate(rows))
    cbtn = "".join(f'<button data-act="gcol" data-col="{j}"{" class=on" if j == 0 else ""}>'
                   f'<b>{j}</b> {H.escape(c)}</button>' for j, c in enumerate(cols))
    modes = "".join(f'<button data-act="gmode" data-mode="{m}"{" class=on" if m == "single" else ""}>{lab}</button>'
                    for m, lab in (("single", "Single"), ("grid", "Grid"), ("matrix", "Matrix")))
    altb = (f'<button data-act="galt">{H.escape(alt_name or "alternate")} (d)</button>' if has_alt else "")
    attrs = (f' tabindex="0" data-mode="single" data-row="0" data-col="0" data-alt="0"'
             f' data-cols="{H.escape("|".join(cols), True)}" data-alt-name="{H.escape(alt_name, True)}"')
    return (_open("gallery", bid, title or "Gallery", attrs)
            + f'<div class="ia-gbar ia-grows">{rbtn}</div>'
            + f'<div class="ia-gbar ia-gcols">{cbtn}</div>'
            + f'<div class="ia-gbar">{modes}{altb}<span class="ia-sp"></span>'
              '<span class="ia-hint">←/→ or 0–9 column · ↑/↓ row · g grid · m matrix · d alternate · click a tile to open</span></div>'
            + f'<div class="ia-gdata" hidden>{"".join(data)}</div><div class="ia-gstage"></div>'
            + '<div class="ia-foot"><textarea rows="1" placeholder="Note (optional)…"></textarea>'
              '<div class="ia-actions"><button class="ia-btn ok" data-act="gpick">★ Prefer the shown column for this row</button>'
              '<button class="ia-btn" data-act="gpickall">★ Prefer the shown column for every row</button></div></div></div>')


# ── params ───────────────────────────────────────────────────────────────────

def params_form(body: str) -> str:
    rows = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            rows.append(f'<div class="ia-hint">{H.escape(s.lstrip("# "))}</div>')
            continue
        m = re.fullmatch(r"([A-Za-z_][\w.\-]*)\s*=\s*(.+)", s)
        if not m:
            rows.append(_warn(f"params: can't parse line {s!r}"))
            continue
        name, spec = m.group(1), m.group(2).strip()
        n = H.escape(name, True)
        if r := re.fullmatch(r"(\S+)\s*\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*(?:,\s*([^\]]+))?\]", spec):
            v, lo, hi, st = r.group(1), r.group(2), r.group(3), (r.group(4) or "any")
            ctl = (f'<input type="range" data-param="{n}" data-num="1" min="{lo}" max="{hi}" step="{st.strip()}" value="{v}" '
                   'oninput="this.nextElementSibling.textContent=this.value">'
                   f'<output>{H.escape(v)}</output>')
        elif r := re.fullmatch(r"(\S+)\s*\{(.+)\}", spec):
            v = r.group(1)
            choices = [c.strip() for c in r.group(2).split(",")]
            ctl = f'<select data-param="{n}">' + "".join(
                f'<option{" selected" if c == v else ""}>{H.escape(c)}</option>' for c in choices) + "</select>"
        elif spec in ("true", "false"):
            ctl = f'<input type="checkbox" data-param="{n}"{" checked" if spec == "true" else ""}>'
        else:
            v = spec[1:-1] if len(spec) >= 2 and spec[0] == spec[-1] == '"' else spec
            num = ' data-num="1"' if re.fullmatch(r"-?[\d.]+(e-?\d+)?", v) else ""
            ctl = f'<input type="text" data-param="{n}"{num} value="{H.escape(v, True)}">'
        rows.append(f'<label class="ia-prow"><span>{H.escape(name)}</span>{ctl}</label>')
    return "".join(rows)


# ── entry point from md_to_html ──────────────────────────────────────────────

def process(text: str, md, put) -> str:
    """Replace interactive blocks in `text` by stash keys. `md` renders nested markdown,
    `put(html)` stashes html and returns the placeholder line."""

    def fenced(m: re.Match) -> str:
        kind, head, body = m.group(1), m.group(2), m.group(3)
        _cur_src[0] = m.group(0)
        if kind == "approve":
            bid, _, title = _split_head(kind, head)
            return put(_open(kind, bid, title or "Sign-off") + f'<div class="ia-body">{md(body)}</div>' + _decision_foot() + "</div>")
        if kind == "diff":
            bid, fl, title = _split_head(kind, head, ("tex",))
            parts = re.split(r"^===[ \t]*$\n?", body, maxsplit=1, flags=re.M)
            if len(parts) != 2:
                return put(_warn(f"diff '{bid}': needs a line containing only === between old and new"))
            old, new = parts[0].rstrip("\n"), parts[1].rstrip("\n")
            if "tex" in fl:
                views = [f'<div class="ia-src tex2jax_ignore">{word_diff(old, new, True)}</div>',
                         f'<div class="ia-src tex2jax_ignore">{H.escape(new)}</div>',
                         f'<div class="ia-src tex2jax_ignore">{H.escape(old)}</div>']
            else:
                views = [md(word_diff(old, new, False)), md(new), md(old)]
            hint = '<div class="ia-hint ia-edhint">Edit the <b>New</b> tab to change the wording yourself.</div>'
            views[0] = hint + views[0]
            views[1] = _editor(new, old) + f'<div class="ia-newview" hidden>{views[1]}</div>'
            tabs = ('<div class="ia-tabs"><button class="on" data-act="tab" data-tab="0">Changes</button>'
                    '<button data-act="tab" data-tab="1">New ✎</button><button data-act="tab" data-tab="2">Old</button></div>')
            panes = "".join(f'<div class="ia-pane"{"" if i == 0 else " hidden"}>{v}</div>' for i, v in enumerate(views))
            return put(_open(kind, bid, title or "Proposed change") + tabs + f'<div class="ia-body">{panes}</div>' + _decision_foot() + "</div>")
        if kind == "choice":
            bid, fl, title = _split_head(kind, head, ("multi",))
            chunks = re.split(r"^@@[ \t]*(.*)$\n?", body, flags=re.M)
            pre, opts = chunks[0], list(zip(chunks[1::2], chunks[2::2]))
            if len(opts) < 2:
                return put(_warn(f"choice '{bid}': needs at least two `@@ Label` options"))
            multi = "multi" in fl
            cards = []
            for i, (lab, ob) in enumerate(opts):
                lab = lab.strip() or chr(65 + i)
                btn = "Select" if multi else f"Pick {H.escape(lab)}"
                cards.append(f'<div class="ia-opt" data-label="{H.escape(lab, True)}" data-src="{H.escape(ob.strip(), True)}">'
                             f'<div class="ia-olabel">{H.escape(lab)}<button type="button" class="ia-oedit" data-act="ed-open" '
                             f'aria-expanded="false">✎ Edit wording</button></div><div class="ia-obody">{md(ob)}</div>'
                             f'{_editor(ob.strip(), ob.strip(), lab)}'
                             f'<button class="ia-btn" data-act="pick">{btn}</button></div>')
            foot = ('<div class="ia-foot"><textarea rows="2" placeholder="Comment (optional; required for “none”)…"></textarea>'
                    '<div class="ia-actions">'
                    + ('<button class="ia-btn ok" data-act="submit">Send selection</button>' if multi else "")
                    + '<button class="ia-btn mid" data-act="none">None of these</button></div></div>')
            return put(_open(kind, bid, title or "Choose", ' data-multi="1"' if multi else "")
                       + (md(pre) if pre.strip() else "") + f'<div class="ia-opts">{"".join(cards)}</div>' + foot + "</div>")
        if kind == "checklist":
            bid, _, title = _split_head(kind, head)
            items = []
            for line in body.splitlines():
                mm = re.match(r"\s*[-*]\s*\[([ xX~])\]\s*(.*)$", line)
                if not mm:
                    if line.strip():
                        WARNINGS.append(f"checklist '{bid}': ignoring line {line.strip()!r} (want `- [ ] item`)")
                    continue
                st = {"x": "done", "X": "done", "~": "doing"}.get(mm.group(1), "todo")
                inner = re.sub(r"^<p>([\s\S]*)</p>\s*$", r"\1", md(mm.group(2)).strip())
                items.append(f'<li class="ia-ck" data-n="{len(items) + 1}" data-state="{st}" '
                             f'data-src="{H.escape(mm.group(2), True)}"><button type="button" class="ia-box" aria-label="Toggle done"></button>'
                             f'<span class="ia-ckt">{inner}</span></li>')
            return put(_open(kind, bid, title or "Checklist") + '<div class="ia-prog"><div></div></div>'
                       f'<ul class="ia-cklist">{"".join(items)}</ul>'
                       '<div class="ia-ckadd"><input placeholder="Suggest another item… (Enter)"></div></div>')
        if kind == "gallery":
            bid, _, title = _split_head(kind, head)
            return put(gallery(bid, title, body))
        if kind == "params":
            bid, _, title = _split_head(kind, head)
            return put(_open(kind, bid, title or "Parameters") + f'<div class="ia-pgrid">{params_form(body)}</div>'
                       '<div class="ia-foot"><textarea rows="1" placeholder="Note (optional)…"></textarea>'
                       '<div class="ia-actions"><button class="ia-btn ok" data-act="send">Send values ▶</button></div></div></div>')
        return m.group(0)

    text = re.sub(r"^:::(approve|diff|choice|params|checklist|gallery)\b[ \t]*(.*)\n([\s\S]*?)^:::[ \t]*$",
                  fenced, text, flags=re.M)

    def ask(m: re.Match) -> str:
        _cur_src[0] = m.group(0)
        bid, _, prompt = _split_head("ask", m.group(1))
        return put(_open("ask", bid, prompt or "Your answer")
                   + '<div class="ia-foot"><textarea rows="3" placeholder="Type here… (Ctrl+Enter sends)"></textarea>'
                   '<div class="ia-actions"><button class="ia-btn ok" data-act="send">Send ▶</button></div></div></div>')

    text = re.sub(r"^:::ask\b[ \t]*(.*)$", ask, text, flags=re.M)
    text = re.sub(r"^:::(pdf|img)[ \t]+(.+)$", lambda m: put(viewer(m.group(1), m.group(2))), text, flags=re.M)
    return text


# ── page-side code (injected before </head>) ─────────────────────────────────

HEAD = r"""
<style>
  body { padding-bottom: 76px !important; }
  .ia { border: 1px solid #b7c2ce; border-radius: 8px; margin: 1.4em 0; background: #eff3f7; overflow: hidden; }
  .ia-head { display: flex; align-items: baseline; gap: 10px; padding: 9px 14px; background: #dbe4ee;
             border-bottom: 1px solid #c3cbd5; font-weight: 600; color: #1a212a; }
  .ia-head .ia-title { flex: 1; }
  .ia-status { font: 12px 'JetBrains Mono', monospace; color: #3f536a; font-weight: 500; }
  .ia-status.on { background: #2b5d8a; color: #fff; padding: 2px 8px; border-radius: 10px; }
  .ia-body { padding: 4px 16px; }
  .ia-foot { padding: 10px 14px 12px; border-top: 1px solid #d3dbe4; }
  .ia-foot textarea, .ia-pop textarea, #ia-composer input {
    width: 100%; box-sizing: border-box; font: 14px/1.4 system-ui, sans-serif; padding: 6px 9px;
    border: 1px solid #b7c2ce; border-radius: 5px; background: #fff; color: #131a21; resize: vertical; }
  .ia-foot textarea.need { border-color: #b8860b; box-shadow: 0 0 0 2px #f3d98b; }
  .ia-actions { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .ia-btn, .ia button { font: 13px system-ui, sans-serif; padding: 5px 12px; border-radius: 5px; cursor: pointer;
    border: 1px solid #9aa9ba; background: #fff; color: #1a212a; }
  .ia-btn:hover, .ia button:hover { background: #e3ebf4; }
  .ia-btn.ok.on   { background: #2e7d4f; border-color: #2e7d4f; color: #fff; }
  .ia-btn.mid.on  { background: #856000; border-color: #856000; color: #fff; }
  .ia-btn.bad.on  { background: #a33a3a; border-color: #a33a3a; color: #fff; }
  .ia-err { border: 1px solid #c55; background: #fde8e8; color: #7a1d1d; padding: 8px 12px; border-radius: 6px; margin: 1em 0;
            font: 13px 'JetBrains Mono', monospace; }
  .ia-hint { font-size: 12px; color: #3f536a; }
  del { background: #f7d4d4; color: #7a1d1d; text-decoration: line-through; text-decoration-color: #a33a3a; }
  ins { background: #cfeedd; color: #14532d; text-decoration: none; }
  del + ins { margin-left: .2em; }
  .ia-src { white-space: pre-wrap; font: 13.5px/1.55 'JetBrains Mono', monospace; padding: 10px 0; }
  .ia-tabs { display: flex; gap: 4px; padding: 8px 14px 0; }
  .ia-tabs button.on, .ia-vbar button.on { background: #2b5d8a; border-color: #2b5d8a; color: #fff; }
  /* choice */
  .ia-opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; padding: 12px 14px; }
  .ia-opt { border: 1px solid #c3cbd5; border-radius: 6px; background: #fff; padding: 8px 12px 10px; display: flex; flex-direction: column; }
  .ia-opt .ia-obody { flex: 1; }
  .ia-opt .ia-olabel { font: 600 12px 'JetBrains Mono', monospace; color: #3f536a; }
  .ia-opt.sel { border-color: #2b5d8a; box-shadow: 0 0 0 2px #9cc0e4; }
  .ia-opt.picked { border-color: #2e7d4f; box-shadow: 0 0 0 2px #9fd5b3; }
  .ia-opt.picked .ia-btn { background: #2e7d4f; color: #fff; border-color: #2e7d4f; }
  .ia-opt .ia-btn { align-self: flex-start; }
  /* params */
  .ia-pgrid { padding: 10px 14px; display: grid; gap: 8px; }
  .ia-prow { display: grid; grid-template-columns: 150px 1fr auto; align-items: center; gap: 10px; font: 13px 'JetBrains Mono', monospace; }
  .ia-prow input[type=text], .ia-prow select { grid-column: span 2; padding: 4px 6px; }
  .ia-prow input[type=checkbox] { justify-self: start; }
  .ia-prow output { min-width: 4em; text-align: right; }
  /* viewer */
  .ia-view { --z: 1; }
  .ia-vbar { display: flex; gap: 4px; align-items: center; padding: 8px 14px; flex-wrap: wrap; }
  .ia-vbar .ia-sp { flex: 1; }
  .ia-vbar .ia-hint { margin-left: 8px; }
  .ia-page { padding: 4px 14px 14px; overflow-x: auto; }
  .ia-ptitle { font: 12px 'JetBrains Mono', monospace; color: #3f536a; margin: 4px 0; }
  .ia-side { display: flex; gap: 10px; width: calc(100% * var(--z)); }
  .ia-side figure, .ia-single figure { margin: 0; flex: 1; position: relative; }
  .ia-single figure { width: calc(100% * var(--z)); }
  .ia-side figcaption { font: 600 12px system-ui; text-align: center; color: #3f536a; margin-bottom: 4px; }
  .ia-view img { width: 100%; display: block; box-shadow: 0 1px 4px rgba(0,0,0,.25); background: #fff; cursor: crosshair; user-select: none; }
  .ia-stackwrap, .ia-wipe { display: none; }
  .ia-view:not([data-mode=side]):not([data-mode=single]) .ia-side { display: none; }
  .ia-view:not([data-mode=side]):not([data-mode=single]) .ia-stackwrap { display: block; }
  .ia-stack { position: relative; width: calc(100% * var(--z)); isolation: isolate; }
  .ia-stack img.b { position: absolute; top: 0; left: 0; }
  .ia-view[data-mode=wipe] .ia-stack img.b { clip-path: inset(0 0 0 var(--wipe, 50%)); }
  .ia-view[data-mode=wipe] .ia-stack { box-shadow: none; }
  .ia-view[data-mode=wipe] .ia-wipe { display: block; width: calc(100% * var(--z)); margin: 0 0 6px; }
  .ia-view[data-mode=diff] .ia-stack { filter: invert(1); }
  .ia-view[data-mode=diff] .ia-stack img { box-shadow: none; }
  .ia-view[data-mode=diff] .ia-stack img.b { mix-blend-mode: difference; }
  .ia-view[data-mode=flicker] .ia-stack img.b { visibility: hidden; }
  .ia-view[data-mode=flicker] .ia-stack.flip img.b { visibility: visible; }
  .ia-flabel { display: none; position: absolute; top: 6px; left: 6px; z-index: 3; background: #2b5d8a; color: #fff;
               font: 12px system-ui; padding: 2px 8px; border-radius: 10px; }
  .ia-view[data-mode=flicker] .ia-flabel, .ia-view[data-mode=wipe] .ia-flabel { display: block; }
  .ia-missing { padding: 40px; text-align: center; color: #3f536a; border: 1px dashed #9aa9ba; }
  .ia-pin { position: absolute; z-index: 4; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%;
            background: #d9480f; color: #fff; font: 600 12px/22px system-ui; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
  .ia-pop, #ia-selpop { position: absolute; z-index: 10; width: 280px; background: #fff; border: 1px solid #9aa9ba; border-radius: 6px;
            padding: 8px; box-shadow: 0 4px 16px rgba(0,0,0,.25); cursor: auto; }
  .ia-pop .ia-actions, #ia-selpop .ia-actions { margin-top: 6px; }
  #ia-selpop { position: fixed; width: 340px; }
  #ia-selpop blockquote { max-height: 90px; overflow: auto; margin: 0 0 6px; font-size: 12.5px; }
  #ia-selbtn { position: fixed; z-index: 11; display: none; font: 13px system-ui; padding: 4px 10px; border-radius: 14px;
               border: 1px solid #2b5d8a; background: #2b5d8a; color: #fff; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
  #ia-composer { position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; display: flex; gap: 6px; align-items: center;
                 padding: 8px 12px; background: #d2dde9; border-top: 1px solid #b7c2ce; }
  #ia-composer input { flex: 1; }
  #ia-composer button { font: 13px system-ui; padding: 5px 10px; border-radius: 5px; border: 1px solid #9aa9ba; background: #fff; cursor: pointer; white-space: nowrap; }
  #ia-toast { position: fixed; top: 10px; right: 16px; z-index: 60; padding: 6px 14px; border-radius: 16px; background: #2e7d4f;
              color: #fff; font: 13px system-ui; opacity: 0; transition: opacity .2s; pointer-events: none; }
  #ia-toast.show { opacity: 1; } #ia-toast.bad { background: #a33a3a; }
  .ia button:focus-visible, .ia input:focus-visible, .ia select:focus-visible, [data-para]:focus-visible, .ia-pin:focus-visible,
  #ia-chat button:focus-visible, #ia-rwbtn:focus-visible { outline: 2px solid #2b5d8a; outline-offset: 2px; }
  [data-para]:focus { outline: none; }
  [data-para]:focus-visible { outline: 2px solid #9fb6cf; }
</style>
<script>
(function () {
  var IA = window.IA = {};
  function $all(root, sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }
  function hhmm(iso) { var d = iso ? new Date(iso) : new Date(); return d.toTimeString().slice(0, 5); }
  function toast(msg, bad) {
    var t = document.getElementById('ia-toast');
    if (!t) { t = document.createElement('div'); t.id = 'ia-toast'; document.body.appendChild(t); }
    t.textContent = msg; t.className = bad ? 'bad show' : 'show';
    clearTimeout(t._h); t._h = setTimeout(function () { t.className = ''; }, 1600);
  }
  /* space at the bottom of the window taken by the message bar and Claude's reply strip */
  IA.bottomReserve = function () {
    var h = 0;
    ['ia-composer', 'ia-chat'].forEach(function (id) { var e = document.getElementById(id); if (e) h += e.getBoundingClientRect().height; });
    return h;
  };
  /* keep a popup fully inside the unobscured viewport: scroll the page if it can, then shift the popup */
  IA.clampPop = function (pop, anchor) {
    var fixed = getComputedStyle(pop).position === 'fixed';
    var limit = function () { return window.innerHeight - IA.bottomReserve() - 8; };
    var r = pop.getBoundingClientRect();
    if (!fixed && r.bottom > limit()) {
      window.scrollBy(0, Math.min(r.bottom - limit(), r.top - 8));
      r = pop.getBoundingClientRect();
    }
    var dx = 0, dy = 0;
    if (r.right > window.innerWidth - 8) dx = window.innerWidth - 8 - r.right;
    if (r.left + dx < 8) dx = 8 - r.left;
    if (r.bottom > limit() && anchor) {        // no room below: open above what it points at, not over it
      var a = anchor.getBoundingClientRect();
      if (a.top - r.height - 6 >= 8) dy = a.top - r.height - 6 - r.top;
    }
    if (r.bottom + dy > limit()) dy = limit() - r.bottom;
    if (r.top + dy < 8) dy = 8 - r.top;
    if (dx || dy) { pop.style.left = (pop.offsetLeft + dx) + 'px'; pop.style.top = (pop.offsetTop + dy) + 'px'; }
  };
  IA._out = {}; IA._bySeq = {}; IA._cursor = 0;
  IA.send = function (ev, el) {
    ev.ts = new Date().toISOString();
    ev.cid = Math.random().toString(36).slice(2, 10);
    var sv = window._iaSaved || {};
    if (sv.card) { ev.card = sv.card; ev.rev = sv.rev; }   // which card revision this click was made on
    try { window.webkit.messageHandlers.claudeEvent.postMessage(ev); }
    catch (e) { toast('Panel not connected', true); return false; }
    IA._out[ev.cid] = { el: el || null, type: ev.type };
    if (ev.type !== '_draft') IA._clearDrafts(ev);
    toast('Sent ✓ — waiting for Claude'); return true;
  };
  // --- unsent text: saved on every keystroke so a re-render (edit, refresh, restart) can't eat it,
  //     and so the panel refuses `panel.sh edit` on a card while Alex is mid-sentence
  IA._dkeys = {}; var _dT = {};
  function draftKey(el) {
    if (!el || !el.matches || !el.matches('textarea, input[type=text], input:not([type])')) return null;
    if (el.hasAttribute('data-ia-edit')) return null;   // proposal editors keep their own drafts ('e:' keys, see IA.edSync)
    if (el.closest('#ia-composer')) return 'composer';
    var pop = el.closest('.ia-pop, #ia-selpop, #ia-rwpop');
    var block = el.closest('.ia');
    if (pop) return 'pop:' + (block && block.dataset.iaId || '_page');
    if (!block || !block.dataset.iaId) return null;
    var f = $all(block, 'textarea, input[type=text], input:not([type])')
      .filter(function (x) { return x.closest('.ia') === block && !x.closest('.ia-pop') && !x.hasAttribute('data-ia-edit'); });
    return 'b:' + block.dataset.iaId + ':' + f.indexOf(el);
  }
  function postDraft(key, text) {   // exposed as IA.postDraft below
    var sv = window._iaSaved || {};
    if (text.trim()) IA._dkeys[key] = 1; else delete IA._dkeys[key];
    try { window.webkit.messageHandlers.claudeEvent.postMessage({ type: '_draft', card: sv.card || null, key: key, text: text }); }
    catch (e) {}
  }
  IA.postDraft = function (key, text) { clearTimeout(_dT[key]); postDraft(key, text); };
  IA._dT = _dT;
  document.addEventListener('input', function (e) {
    var el = e.target, key = draftKey(el); if (!key) return;
    clearTimeout(_dT[key]); _dT[key] = setTimeout(function () { postDraft(key, el.value || ''); }, 250);
  }, true);
  IA._clearDrafts = function (ev) {
    if (ev.type === 'diff_edit') return;   // sending an edit for review is not sending the note
    var carries = ['note', 'answer', 'comment', 'text', 'instruction'].some(function (f) { return ev[f] !== undefined; });
    if (!carries) return;
    Object.keys(IA._dkeys).forEach(function (k) {
      if (k.indexOf('pop:') === 0 || (ev.id && k.indexOf('b:' + ev.id + ':') === 0) || (ev.type === 'message' && k === 'composer')) {
        clearTimeout(_dT[k]); postDraft(k, '');
      }
    });
  };
  IA.restoreDrafts = function () {
    var d = (window._iaSaved || {}).drafts || {}, n = 0;
    Object.keys(d).forEach(function (k) {
      var el = null;
      if (k === 'composer') el = document.querySelector('#ia-composer input');
      else if (k.indexOf('b:') === 0) {
        var i = k.lastIndexOf(':'), id = k.slice(2, i), idx = +k.slice(i + 1);
        var block = $all(document, '.ia[data-ia-id]').filter(function (x) { return x.dataset.iaId === id; }).pop();
        if (block) el = $all(block, 'textarea, input[type=text], input:not([type])')
          .filter(function (x) { return x.closest('.ia') === block && !x.closest('.ia-pop') && !x.hasAttribute('data-ia-edit'); })[idx] || null;
      } else if (k.indexOf('e:') === 0) {   // an unsent edit of a proposal
        var rest = k.slice(2), j = rest.indexOf(':');
        var ed = IA.edFind(j < 0 ? rest : rest.slice(0, j), j < 0 ? null : rest.slice(j + 1));
        if (ed) { var ta = ed.querySelector('textarea'); if (ta.value !== d[k]) { ta.value = d[k]; ta.classList.add('ia-restored'); n++; } IA.edOpen(ed); }
      }
      IA._dkeys[k] = 1;   // still unsent, wherever it lives
      if (el && el.value !== d[k]) { el.value = d[k]; el.classList.add('ia-restored'); n++; }
    });
    if (IA.edRefreshAll) IA.edRefreshAll();
    if (n) toast('Restored your unsent text');
  };
  IA.refused = function (cid, why) {   // the panel rejected a click (card changed under it)
    var o = IA._out[cid]; delete IA._out[cid];
    if (o && o.el && o.el.classList.contains('ia-ed') && IA.edUnsent) IA.edUnsent(o.el);
    if (o && o.el) { var s = own(o.el, '.ia-status'); if (s) { s.textContent = 'not sent'; s.classList.remove('on'); } }
    toast(why, true);
  };
  function own(block, sel) {   // first match that belongs to this block, not a nested one
    return $all(block, sel).filter(function (el) { return el.closest('.ia') === block; })[0] || null;
  }
  function note(block) { var t = own(block, '.ia-foot textarea'); return t ? t.value.trim() : ''; }
  function setStatus(block, txt) {
    var s = own(block, '.ia-status'); if (s) { s.textContent = txt; s.classList.add('on'); s.classList.remove('wait', 'seen'); }
  }
  var WORD = { approve: '✓ approved', changes: '✎ changes requested', reject: '✕ rejected' };

  function markDecision(block, ev) {
    var k = block.dataset.iaKind;
    if (k === 'diff' && ev.edited_text !== undefined && IA.edFinal) IA.edFinal(block, null, ev.edited_text);
    if (k === 'choice' && IA.edFinal) (ev.picked || []).forEach(function (l, i) {
      if ((ev.picked_edited || [])[i]) IA.edFinal(block, l, (ev.picked_text || [])[i]);
    });
    if (k === 'approve' || k === 'diff') {
      $all(block, '.ia-actions [data-act]').forEach(function (b) {
        if (b.closest('.ia') === block) b.classList.toggle('on', b.dataset.act === ev.decision);
      });
      setStatus(block, WORD[ev.decision] + ' · ' + hhmm(ev.ts));
    } else if (k === 'choice') {
      $all(block, '.ia-opt').forEach(function (o) {
        o.classList.toggle('picked', (ev.picked || []).indexOf(o.dataset.label) >= 0); o.classList.remove('sel');
      });
      setStatus(block, ((ev.picked || []).length ? 'picked ' + ev.picked.join(', ') : 'none of these') + ' · ' + hhmm(ev.ts));
    } else if (k === 'ask') {
      setStatus(block, 'sent · ' + hhmm(ev.ts));
    } else if (k === 'params') {
      setStatus(block, 'sent · ' + hhmm(ev.ts));
    }
    if (ev.note !== undefined) { var t = own(block, '.ia-foot textarea'); if (t && !t.value) t.value = ev.note; }
    if (block.dataset.rw !== undefined && IA.applyRewrite) IA.applyRewrite(block, ev.decision);
  }

  function needNote(block, msg) {
    var t = own(block, '.ia-foot textarea'); t.classList.add('need'); t.placeholder = msg; t.focus();
  }

  function base(block) {
    return { id: block.dataset.iaId, title: block.dataset.title || '' };
  }

  function onAction(b) {
    var block = b.closest('.ia'); if (!block) return;
    var k = block.dataset.iaKind, act = b.dataset.act, ev = base(block);
    if (act.indexOf('ed-') === 0) return IA.edAction(b, block);
    if (act === 'tab') {
      $all(block, '.ia-tabs button').forEach(function (x) { x.classList.toggle('on', x === b); });
      $all(block, '.ia-pane').forEach(function (p, i) { p.hidden = String(i) !== b.dataset.tab; });
      return;
    }
    if (act === 'mode') {
      block.dataset.mode = b.dataset.mode;
      $all(block, '.ia-mode').forEach(function (x) { x.classList.toggle('on', x === b); });
      labelStacks(block); return;
    }
    if (act === 'zoom') {
      var z = parseFloat(getComputedStyle(block).getPropertyValue('--z')) || 1;
      z = Math.min(4, Math.max(0.5, z + parseFloat(b.dataset.dz)));
      block.style.setProperty('--z', z); return;
    }
    if (k === 'approve' || k === 'diff') {
      ev.type = k; ev.decision = act; ev.note = note(block);
      if (k === 'diff' && act === 'approve' && IA.edApprove && IA.edApprove(block, ev) === false) return;
      if (act === 'changes' && !ev.note) return needNote(block, 'Say what to change, then press “Request changes” again');
    } else if (k === 'choice') {
      var multi = block.dataset.multi === '1';
      if (act === 'pick' && multi) { b.closest('.ia-opt').classList.toggle('sel'); return; }
      var opts = act === 'pick' ? [b.closest('.ia-opt')] : act === 'submit' ? $all(block, '.ia-opt.sel') : [];
      if (act === 'submit' && !opts.length) return toast('Select at least one option', true);
      ev.type = 'choice'; ev.note = note(block);
      if (act === 'none' && !ev.note) return needNote(block, 'Say what you want instead, then press “None of these” again');
      ev.picked = opts.map(function (o) { return o.dataset.label; });
      ev.picked_text = opts.map(function (o) { return o.dataset.src; });
      if (IA.edPick && IA.edPick(opts, ev) === false) return;
    } else if (k === 'ask') {
      ev.type = 'ask'; ev.answer = note(block);
      if (!ev.answer) return toast('Nothing to send', true);
    } else if (k === 'params') {
      ev.type = 'params'; ev.note = note(block); ev.values = {};
      $all(block, '[data-param]').forEach(function (el) {
        var v = el.type === 'checkbox' ? el.checked : el.value;
        if (el.dataset.num && v !== '' && !isNaN(+v)) v = +v;
        ev.values[el.dataset.param] = v;
      });
    } else return;
    if (IA.send(ev, block)) markDecision(block, ev);
  }

  /* ── viewer: pins + wipe + flicker ────────────────────────── */
  function labelStacks(view) {
    $all(view, '.ia-stack').forEach(function (s) {
      var l = s.querySelector('.ia-flabel'); if (!l) return;
      if (view.dataset.mode === 'wipe') l.textContent = '◀ ' + view.dataset.labOld + ' | ' + view.dataset.labNew + ' ▶';
      else l.textContent = s.classList.contains('flip') ? view.dataset.labNew : view.dataset.labOld;
    });
  }
  setInterval(function () {
    $all(document, '.ia-view[data-mode=flicker]').forEach(function (v) {
      $all(v, '.ia-stack').forEach(function (s) { s.classList.toggle('flip'); }); labelStacks(v);
    });
  }, 800);
  document.addEventListener('input', function (e) {
    if (e.target.classList.contains('ia-wipe')) {
      e.target.previousElementSibling.style.setProperty('--wipe', e.target.value + '%');
    }
  });

  function placePin(pz, x, y, n) {
    var p = document.createElement('div'); p.className = 'ia-pin'; p.textContent = n; p.tabIndex = 0;
    p.style.left = (x * 100) + '%'; p.style.top = (y * 100) + '%'; pz.appendChild(p); return p;
  }
  function closePops() {
    $all(document, '.ia-pop').forEach(function (p) { p.remove(); });
    $all(document, '.ia-pin.draft').forEach(function (p) { p.remove(); });   // abandoned, never sent
  }

  function openPin(pz, e) {
    closePops();
    var view = pz.closest('.ia-view'), page = pz.closest('.ia-page');
    var img = pz.querySelector('img') || pz;
    var r = img.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    var side = pz.dataset.which, mode = view.dataset.mode;
    if (side === 'stack') {
      var stack = pz;
      side = mode === 'wipe' ? (x * 100 < parseFloat(stack.style.getPropertyValue('--wipe') || '50') ? 'old' : 'new')
           : mode === 'flicker' ? (stack.classList.contains('flip') ? 'new' : 'old') : 'both (difference view)';
    }
    var label = side === 'old' ? view.dataset.labOld : side === 'new' || side === 'single' ? view.dataset.labNew : side;
    var path = side === 'old' ? view.dataset.vs : view.dataset.path;
    var n = $all(view, '.ia-pin').length + 1;
    var pin = placePin(pz, x, y, n); pin.classList.add('draft');
    var pop = document.createElement('div'); pop.className = 'ia-pop';

    pop.innerHTML = '<textarea rows="3" placeholder="Comment on this spot… (Ctrl+Enter sends)"></textarea>' +
      '<div class="ia-actions"><button class="ia-btn ok">Send</button><button class="ia-btn">Cancel</button></div>';
    // on <body>, not inside the block: blocks clip their overflow
    var pr = pin.getBoundingClientRect();
    pop.style.left = (pr.left + window.scrollX - 12) + 'px'; pop.style.top = (pr.bottom + window.scrollY + 6) + 'px';
    document.body.appendChild(pop);
    IA.clampPop(pop, pin);
    var ta = pop.querySelector('textarea'); ta.focus({ preventScroll: true });
    function go() {
      var c = ta.value.trim(); if (!c) { ta.focus(); return; }
      var ev = base(view);
      ev.type = 'pin'; ev.n = n; ev.page = +page.dataset.page; ev.side = side === 'single' ? 'image' : label; ev.path = path;
      ev.x = Math.round(x * 1000) / 1000; ev.y = Math.round(y * 1000) / 1000; ev.comment = c;
      ev.pin_which = pz.dataset.which;
      if (IA.send(ev, pin)) { pin.title = c; pin.classList.remove('draft'); pop.remove(); pin.focus(); }
    }
    var bs = pop.querySelectorAll('button');
    bs[0].onclick = function (ev) { ev.stopPropagation(); go(); };
    bs[1].onclick = function (ev) { ev.stopPropagation(); pin.remove(); pop.remove(); };
    ta.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Enter' && ev.ctrlKey) go();
      if (ev.key === 'Escape') { pin.remove(); pop.remove(); }
    });
    pop.addEventListener('click', function (ev) { ev.stopPropagation(); });
  }

  function tgt(e) { var t = e.target; return t && t.nodeType === 1 ? t : (t && t.parentElement) || document.body; }
  document.addEventListener('click', function (e) {
    var T = tgt(e), b = T.closest('[data-act]');
    if (b && b.closest('.ia')) { onAction(b); return; }
    var pz = T.closest('.ia-pinnable');
    if (pz && !T.closest('.ia-pop') && T.tagName === 'IMG') openPin(pz, e);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.ctrlKey && e.target.tagName === 'TEXTAREA' && e.target.closest && e.target.closest('.ia-foot')) {
      var block = e.target.closest('.ia');
      var btn = own(block, '[data-act=send]') || own(block, '[data-act=submit]');
      if (btn) { e.preventDefault(); onAction(btn); }
    }
  });

  /* ── select text → comment ─────────────────────────────────── */
  function annotateMath() {
    try {
      var list = MathJax.startup.document.math;
      for (var it of list) {
        if (it.typesetRoot) { it.typesetRoot.dataset.tex = it.math; it.typesetRoot.dataset.display = it.display ? '1' : '0'; }
      }
    } catch (e) {}
  }
  window.addEventListener('load', function () {
    try { MathJax.startup.promise.then(annotateMath); } catch (e) {}
  });
  function texOf(el) { return el.dataset.display === '1' ? '$$' + el.dataset.tex + '$$' : '$' + el.dataset.tex + '$'; }
  function fragText(node) {
    var out = '';
    node.childNodes.forEach(function (c) {
      if (c.nodeType === 3) out += c.nodeValue;
      else if (c.nodeType === 1) {
        if (c.tagName === 'MJX-CONTAINER' && c.dataset.tex != null) out += texOf(c);
        else if (c.tagName === 'MJX-ASSISTIVE-MML') {}
        else { out += fragText(c); if (/^(P|DIV|LI|H[1-6]|TR|BLOCKQUOTE)$/.test(c.tagName)) out += '\n'; }
      }
    });
    return out;
  }
  function selectionText(sel) {
    var r = sel.getRangeAt(0), anc = r.commonAncestorContainer;
    var m = (anc.nodeType === 1 ? anc : anc.parentElement).closest('mjx-container');
    if (m && m.dataset.tex != null) return texOf(m);
    return fragText(r.cloneContents()).replace(/\n{3,}/g, '\n\n').trim();
  }
  function headingBefore(node) {
    var hs = $all(document, 'h1,h2,h3,h4'), best = '';
    hs.forEach(function (h) { if (h.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) best = h.textContent.trim(); });
    return best;
  }
  var selState = null;
  document.addEventListener('mouseup', function (e) {
    var btn = document.getElementById('ia-selbtn');
    if (tgt(e).closest('#ia-selbtn,#ia-selpop,#ia-composer,.ia-pop,textarea,input,select,button')) return;
    setTimeout(function () {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) { btn.style.display = 'none'; return; }
      var rr = sel.getRangeAt(0).getBoundingClientRect();
      var node = sel.anchorNode, el = node.nodeType === 1 ? node : node.parentElement;
      var blk = el && el.closest('.ia');
      selState = { text: selectionText(sel), context: headingBefore(node), id: blk ? blk.dataset.iaId : '' };
      btn.style.left = Math.min(rr.right, window.innerWidth - 150) + 'px';
      btn.style.top = Math.min(rr.bottom + 6, window.innerHeight - 110) + 'px';
      btn.style.display = 'block';
    }, 10);
  });
  function openSelPop() {
    var btn = document.getElementById('ia-selbtn'); btn.style.display = 'none';
    var old = document.getElementById('ia-selpop'); if (old) old.remove();
    var pop = document.createElement('div'); pop.id = 'ia-selpop';
    pop.style.left = Math.min(parseFloat(btn.style.left), window.innerWidth - 360) + 'px';
    pop.style.top = Math.min(parseFloat(btn.style.top), window.innerHeight - 220) + 'px';
    var q = document.createElement('blockquote'); q.textContent = selState.text; pop.appendChild(q);
    pop.insertAdjacentHTML('beforeend', '<textarea rows="3" placeholder="What about this? (Ctrl+Enter sends)"></textarea>' +
      '<div class="ia-actions"><button class="ia-btn ok">Send</button><button class="ia-btn">Cancel</button></div>');
    document.body.appendChild(pop);
    IA.clampPop(pop);
    var ta = pop.querySelector('textarea'); ta.focus();
    function go() {
      var c = ta.value.trim(); if (!c) { ta.focus(); return; }
      if (IA.send({ type: 'selection', id: selState.id, text: selState.text, context: selState.context, comment: c })) pop.remove();
    }
    var bs = pop.querySelectorAll('button');
    bs[0].onclick = go; bs[1].onclick = function () { pop.remove(); };
    ta.addEventListener('keydown', function (ev) {
      ev.stopPropagation();
      if (ev.key === 'Enter' && ev.ctrlKey) go();
      if (ev.key === 'Escape') pop.remove();
    });
  }

  /* ── composer (always-on message bar) ─────────────────────── */
  IA.mountComposer = function () {
    if (!document.getElementById('ia-selbtn')) {
      var sb = document.createElement('button'); sb.id = 'ia-selbtn'; sb.textContent = '💬 Ask about this';
      sb.addEventListener('mousedown', function (e) { e.preventDefault(); });
      sb.onclick = openSelPop; document.body.appendChild(sb);
    }
    if (document.getElementById('ia-composer')) return;
    var c = document.createElement('div'); c.id = 'ia-composer';
    c.innerHTML = '<input placeholder="Message Claude about this response…  (Enter sends)">' +
      '<button data-q="">Send</button>' +
      '<button data-q="👍 Looks good — carry on." title="Looks good">👍</button>' +
      '<button data-q="I don\'t follow this — explain it differently / in more detail." title="Unclear">❓</button>' +
      '<button data-q="Continue." title="Continue">→</button>';
    document.body.appendChild(c);
    var inp = c.querySelector('input');
    function go(txt) {
      txt = (txt || inp.value).trim(); if (!txt) return;
      if (IA.send({ type: 'message', text: txt })) inp.value = '';
    }
    c.querySelectorAll('button').forEach(function (b) { b.onclick = function () { go(b.dataset.q); }; });
    inp.addEventListener('keydown', function (e) { e.stopPropagation(); if (e.key === 'Enter') go(); });
  };

  IA.own = own; IA.setStatus = setStatus; IA.markDecision = markDecision; IA.toast = toast;
  IA.annotateMath = annotateMath; IA.fragText = fragText; IA.headingBefore = headingBefore;
  IA.placePin = placePin; IA.all = $all;

  /* ── restore decisions saved on this history entry ────────── */
  document.addEventListener('DOMContentLoaded', function () {
    IA.mountComposer();
    var saved = window._iaSaved || {};
    Object.keys(saved.decisions || {}).forEach(function (id) {
      var ev = saved.decisions[id];
      var block = $all(document, '.ia[data-ia-id]').filter(function (b) { return b.dataset.iaId === id; })[0];
      if (!block) return;
      if (ev.type === 'ask') { var t = own(block, '.ia-foot textarea'); if (t) t.value = ev.answer || ''; }
      if (ev.type === 'params') {
        $all(block, '[data-param]').forEach(function (el) {
          if (!(el.dataset.param in ev.values)) return;
          var v = ev.values[el.dataset.param];
          if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
          if (el.nextElementSibling && el.nextElementSibling.tagName === 'OUTPUT') el.nextElementSibling.textContent = v;
        });
      }
      markDecision(block, ev);
      if (ev.seq) IA._bySeq[ev.seq] = { el: block, type: ev.type };
    });
    Object.keys(saved.pins || {}).forEach(function (id) {
      var view = $all(document, '.ia-view').filter(function (b) { return b.dataset.iaId === id; })[0];
      if (!view) return;
      saved.pins[id].forEach(function (p) {
        var page = $all(view, '.ia-page').filter(function (x) { return +x.dataset.page === p.page; })[0];
        if (!page) return;
        var pz = page.querySelector('.ia-pinnable[data-which="' + p.pin_which + '"]') || page.querySelector('.ia-pinnable');
        var pin = placePin(pz, p.x, p.y, p.n); pin.title = p.comment;
        if (p.seq) IA._bySeq[p.seq] = { el: pin, type: 'pin' };
      });
    });
  });
})();
</script>
"""

# second chunk: receipts, replies, awaiting badges, paragraph rewrites, checklists
HEAD += r"""
<style>
  .ia-status.wait { background: #f3e3b5; color: #6b4e00; padding: 2px 8px; border-radius: 10px; }
  .ia-status.seen { background: #2e7d4f; }
  .ia-pin.seen { background: #2e7d4f; }
  .ia-reply { margin: 0 14px 12px; padding: 7px 12px; background: #fff; border-left: 3px solid #2b5d8a; border-radius: 4px;
              font-size: 14px; position: relative; }
  .ia-reply b { color: #2b5d8a; margin-right: 6px; }
  .ia-reply .x { position: absolute; right: 4px; top: 3px; cursor: pointer; color: #3f536a; background: none; border: 0;
                 font: 13px system-ui; padding: 2px 6px; border-radius: 4px; }
  .ia-reply .x:hover { background: #e3ebf4; }
  #ia-chat { position: fixed; left: 12px; right: 12px; bottom: 50px; z-index: 49; }
  #ia-chat .ia-chat-list { max-height: 26vh; overflow: auto; }
  #ia-chat.folded .ia-chat-list { display: none; }
  #ia-chat .ia-chat-bar { text-align: right; }
  #ia-chat .ia-chat-fold { font: 12px system-ui; padding: 2px 10px; border-radius: 10px 10px 0 0; border: 1px solid #b7c2ce;
                           border-bottom: 0; background: #dbe4ee; color: #1a212a; cursor: pointer; }
  #ia-chat .ia-reply { margin: 0 0 6px; padding-right: 30px; box-shadow: 0 2px 10px rgba(0,0,0,.18); }
  [data-para] { position: relative; }
  #ia-rwbtn { position: absolute; z-index: 9; display: none; width: 22px; height: 22px; border-radius: 50%; border: 1px solid #9aa9ba;
              background: #fff; color: #2b5d8a; font: 13px/20px system-ui; text-align: center; cursor: pointer; padding: 0; opacity: .75; }
  #ia-rwbtn:hover { opacity: 1; background: #2b5d8a; color: #fff; }
  #ia-rwpop { position: absolute; z-index: 10; width: 380px; background: #fff; border: 1px solid #9aa9ba; border-radius: 6px;
              padding: 8px; box-shadow: 0 4px 16px rgba(0,0,0,.25); }
  #ia-rwpop .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
  #ia-rwpop .chips button { font: 12px system-ui; padding: 2px 9px; border-radius: 12px; border: 1px solid #9aa9ba; background: #eff3f7; cursor: pointer; }
  #ia-rwpop textarea { width: 100%; box-sizing: border-box; font: 14px system-ui; padding: 6px; border: 1px solid #b7c2ce; border-radius: 5px; }
  .rw-pending { box-shadow: -10px 0 0 -7px #b8860b; }
  .rw-applied { box-shadow: -10px 0 0 -7px #2e7d4f; }
  .ia.ia-collapsed > :not(.ia-head):not(.ia-reply) { display: none; }
  .ia.ia-collapsed { cursor: pointer; }
  .ia button.ia-expand { font: 12px system-ui; padding: 1px 9px; border-radius: 10px; }
  .ia-sugg { margin-top: -0.6em; }
  /* checklist */
  .ia-prog { height: 4px; background: #d3dbe4; }
  .ia-prog div { height: 100%; width: 0; background: #2e7d4f; transition: width .4s; }
  .ia-cklist { list-style: none; padding: 8px 16px 4px; margin: 0; }
  .ia-ck { display: flex; gap: 10px; align-items: baseline; padding: 4px 6px; border-radius: 4px; }
  .ia button.ia-box { flex: none; width: 15px; height: 15px; padding: 0; background: #fff; border: 1.5px solid #3f536a;
            border-radius: 4px; cursor: pointer; position: relative; top: 3px; box-sizing: border-box; }
  .ia button.ia-box:hover { background: #e3ebf4; }
  .ia-ck[data-state=done] .ia-box { background: #2e7d4f; border-color: #2e7d4f; }
  .ia-ck[data-state=done] .ia-box::after { content: "✓"; color: #fff; font: 11px/12px system-ui; position: absolute; left: 1.5px; top: 0; }
  .ia-ck[data-state=done] .ia-ckt { color: #3f536a; text-decoration: line-through; }
  .ia-ck[data-state=doing] .ia-box { border-color: #2b5d8a; border-top-color: transparent; border-radius: 50%; animation: iaspin 1s linear infinite; }
  .ia-ck[data-state=doing] .ia-ckt { font-weight: 600; }
  @keyframes iaspin { to { transform: rotate(360deg); } }
  .ia-ck.flash { animation: iaflash 1.2s; }
  @keyframes iaflash { from { background: #cfeedd; } to { background: transparent; } }
  .ia-ckadd { padding: 0 16px 12px; }
  .ia-ckadd input { width: 100%; box-sizing: border-box; font: 13px system-ui; padding: 5px 8px; border: 1px dashed #9aa9ba;
                    border-radius: 5px; background: transparent; }
  .ia.flash { animation: iaflash 1.4s; }
</style>
<script>
(function () {
  var IA = window.IA;
  var DECIDE = { approve: 1, diff: 1, choice: 1, ask: 1 };
  function hhmm(iso) { var d = iso ? new Date(iso) : new Date(); return d.toTimeString().slice(0, 5); }
  function blockById(id) {
    return IA.all(document, '.ia[data-ia-id]').filter(function (b) { return b.dataset.iaId === id; }).pop() || null;
  }
  function typeset(el) {
    try { MathJax.startup.promise.then(function () { return MathJax.typesetPromise([el]); }).then(IA.annotateMath); } catch (e) {}
  }

  /* ── delivery receipts ─────────────────────────────────── */
  function markSeen(o) {
    if (!o || o.seen) return; o.seen = true;
    var el = o.el; if (!el) return;
    if (el.classList.contains('ia-pin') || el.classList.contains('ia-ed')) { el.classList.add('seen'); return; }
    var s = IA.own(el, '.ia-status');
    if (s && s.classList.contains('on') && s.textContent.indexOf('seen') < 0) { s.textContent += ' · seen ✓'; s.classList.add('seen'); }
  }
  IA.sent = function (cid, seq) {
    var o = IA._out[cid]; if (!o) return;
    o.seq = seq; IA._bySeq[seq] = o;
    if (o.el && o.el.classList.contains('ia-ed') && IA.edConfirmed) IA.edConfirmed(o.el);
    if (seq <= IA._cursor) markSeen(o);
  };
  IA.seen = function (cursor, quiet) {
    IA._cursor = cursor; var n = 0;
    Object.keys(IA._bySeq).forEach(function (k) {
      var o = IA._bySeq[k]; if (+k <= cursor && !o.seen) { markSeen(o); n++; }
    });
    if (n && !quiet) IA.toast('Claude received ✓');
  };

  /* ── replies from Claude ───────────────────────────────── */
  function chatStrip() {
    var c = document.getElementById('ia-chat');
    if (c) return c;
    c = document.createElement('div'); c.id = 'ia-chat';
    c.innerHTML = '<div class="ia-chat-bar"><button type="button" class="ia-chat-fold" aria-expanded="true"></button></div>' +
                  '<div class="ia-chat-list"></div>';
    c.querySelector('.ia-chat-fold').onclick = function () { c.classList.toggle('folded'); chatUpdate(); };
    document.body.appendChild(c); return c;
  }
  function chatUpdate() {
    var c = document.getElementById('ia-chat'); if (!c) return;
    var n = c.querySelectorAll('.ia-chat-list .ia-reply').length, folded = c.classList.contains('folded');
    c.style.display = n ? '' : 'none';
    var f = c.querySelector('.ia-chat-fold');
    f.textContent = (folded ? '▸ ' : '▾ ') + n + ' message' + (n === 1 ? '' : 's') + ' from Claude' + (folded ? '' : ' · hide');
    f.setAttribute('aria-expanded', String(!folded));
    // keep the end of the page scrollable above the bar + strip
    document.body.style.setProperty('padding-bottom', (IA.bottomReserve() + 20) + 'px', 'important');
  }
  IA.chatUpdate = chatUpdate;
  IA.reply = function (id, text, ts, quiet) {
    var d = document.createElement('div'); d.className = 'ia-reply';
    d.innerHTML = '<b>Claude · ' + hhmm(ts) + '</b>';
    var sp = document.createElement('span'); sp.textContent = text; d.appendChild(sp);
    var block = id ? blockById(id) : null;
    if (block) { block.appendChild(d); }
    else {
      var c = chatStrip();
      var x = document.createElement('button'); x.type = 'button'; x.className = 'x'; x.textContent = '✕';
      x.setAttribute('aria-label', 'Dismiss reply');
      x.onclick = function () {
        var next = d.nextElementSibling || d.previousElementSibling;
        d.remove(); chatUpdate(); (next && next.querySelector('.x') || document.querySelector('#ia-composer input')).focus();
      };
      d.appendChild(x); c.querySelector('.ia-chat-list').appendChild(d);
      c.classList.remove('folded'); chatUpdate();
    }
    typeset(d);
    if (!quiet) { IA.toast('Claude: ' + text.slice(0, 70)); if (block) { block.classList.remove('flash'); void block.offsetWidth; block.classList.add('flash'); } }
  };

  /* ── awaiting-you badges ───────────────────────────────── */
  IA.markAwaiting = function () {
    IA.all(document, '.ia[data-ia-kind]').forEach(function (b) {
      if (!DECIDE[b.dataset.iaKind]) return;
      var s = IA.own(b, '.ia-status');
      if (s && !s.classList.contains('on')) { s.textContent = 'awaiting you'; s.classList.add('wait'); }
    });
  };
  IA.scrollToPending = function () {
    var s = IA.all(document, '.ia-status.wait')[0]; if (!s) return;
    var b = s.closest('.ia'); b.scrollIntoView({ block: 'center' });
    b.classList.remove('flash'); void b.offsetWidth; b.classList.add('flash');
  };

  /* ── paragraph rewrites ────────────────────────────────── */
  function rewritables() {
    return IA.all(document.body, ':scope > p, :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > blockquote,' +
                                 ':scope > ul > li, :scope > ol > li').filter(function (el) {
      return el.textContent.trim() && !el.querySelector('div,figure,details,video,script,table,.ia,.mcq,ul,ol');
    });
  }
  var rwTarget = null, rwHide = null;
  function rwButton() {
    var b = document.getElementById('ia-rwbtn');
    if (!b) {
      b = document.createElement('button'); b.id = 'ia-rwbtn'; b.textContent = '✎'; b.title = 'Ask Claude to rewrite this'; b.setAttribute('aria-label', 'Ask Claude to rewrite this paragraph');
      b.onclick = function (e) { e.stopPropagation(); openRewrite(rwTarget); };
      b.onmouseenter = function () { clearTimeout(rwHide); };
      document.body.appendChild(b);
    }
    return b;
  }
  document.addEventListener('mouseover', function (e) {
    var t = e.target.nodeType === 1 ? e.target : e.target.parentElement;
    if (!t || t.id === 'ia-rwbtn') return;
    var el = t.closest('[data-para]'), b = rwButton();
    if (el) {
      clearTimeout(rwHide); rwTarget = el;
      var r = el.getBoundingClientRect();
      b.style.left = (r.left + window.scrollX - 26) + 'px'; b.style.top = (r.top + window.scrollY + 1) + 'px';
      b.style.display = 'block';
    } else { clearTimeout(rwHide); rwHide = setTimeout(function () { b.style.display = 'none'; }, 700); }
  });
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest && e.target.closest('[data-para]'); if (!el || el !== e.target) return;
    var b = rwButton(), r = el.getBoundingClientRect();
    clearTimeout(rwHide); rwTarget = el;
    b.style.left = (r.left + window.scrollX - 26) + 'px'; b.style.top = (r.top + window.scrollY + 1) + 'px';
    b.style.display = 'block';
  });
  document.addEventListener('keydown', function (e) {   // Enter on a focused paragraph = ask for a rewrite
    if (e.key === 'Enter' && !e.ctrlKey && !e.altKey && !window._deckMode && e.target.dataset && e.target.dataset.para !== undefined) {
      e.preventDefault(); e.stopPropagation(); openRewrite(e.target);
    }
  }, true);
  var CHIPS = ['Shorter', 'Clearer', 'More rigorous', 'Simpler', 'More formal', 'Fix errors'];
  function openRewrite(el) {
    if (!el) return;
    var old = document.getElementById('ia-rwpop'); if (old) old.remove();
    var back = document.activeElement && document.activeElement.id === 'ia-rwbtn' ? el : (document.activeElement || el);
    var r = el.getBoundingClientRect();
    var pop = document.createElement('div'); pop.id = 'ia-rwpop';
    pop.style.left = (Math.max(8, r.left + window.scrollX)) + 'px'; pop.style.top = (r.bottom + window.scrollY + 4) + 'px';
    pop.innerHTML = '<div class="chips">' + CHIPS.map(function (c) { return '<button>' + c + '</button>'; }).join('') + '</div>' +
      '<textarea rows="2" placeholder="How should it change? (Ctrl+Enter sends)"></textarea>' +
      '<div class="ia-actions"><button class="ia-btn ok">Ask for rewrite</button><button class="ia-btn">Cancel</button></div>';
    document.body.appendChild(pop);
    IA.clampPop(pop, el);
    var ta = pop.querySelector('textarea'); ta.focus({ preventScroll: true });
    function close() { pop.remove(); if (back && back.focus) back.focus({ preventScroll: true }); }
    IA.all(pop, '.chips button').forEach(function (c) {
      c.onclick = function () { ta.value = (ta.value ? ta.value.replace(/\s*$/, '; ') : '') + c.textContent.toLowerCase(); ta.focus(); };
    });
    function go() {
      var ins = ta.value.trim(); if (!ins) { ta.focus(); return; }
      var ev = { type: 'rewrite', para: +el.dataset.para, text: IA.fragText(el).trim(), instruction: ins,
                 context: el.matches('h1,h2,h3,h4') ? el.textContent.trim() : IA.headingBefore(el) };
      if (IA.send(ev, null)) { el.classList.add('rw-pending'); el.title = 'Waiting for Claude’s rewrite'; close(); }
    }
    var bs = pop.querySelectorAll('.ia-actions button');
    bs[0].onclick = go; bs[1].onclick = close;
    pop.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter' && e.ctrlKey) go();
      if (e.key === 'Escape') close();
    });
  }
  IA.suggest = function (para, html, quiet, replaced) {
    var el = document.querySelector('[data-para="' + para + '"]'); if (!el) return null;
    IA.all(document, '.ia-sugg [data-rw="' + para + '"]').forEach(function (b) {   // superseded proposal
      if (el._orig !== undefined) { el.innerHTML = el._orig; el.classList.remove('rw-applied'); }
      b.closest('.ia-sugg').remove();
    });
    var w = document.createElement('div'); w.className = 'ia-sugg'; w.innerHTML = html;
    var block = w.querySelector('.ia'); if (!block) return null;
    block.dataset.rw = para;
    (el.tagName === 'LI' ? el.closest('ul,ol') : el).after(w);
    el.classList.remove('rw-pending'); el.removeAttribute('title');
    typeset(w); IA.markAwaiting();
    if (!quiet) { w.scrollIntoView({ block: 'nearest' }); IA.toast('Claude suggested a rewrite'); }
    return block;
  };
  function collapse(block, on) {
    block.classList.toggle('ia-collapsed', on);
    var head = IA.own(block, '.ia-head'), b = head && head.querySelector('.ia-expand');
    if (!b && head) { b = document.createElement('button'); b.type = 'button'; b.className = 'ia-expand'; head.appendChild(b); }
    if (b) { b.textContent = on ? 'Show ▸' : 'Hide ▾'; b.setAttribute('aria-expanded', String(!on)); }
  }
  IA.collapse = collapse;
  IA.applyRewrite = function (block, decision) {
    var el = document.querySelector('[data-para="' + block.dataset.rw + '"]'); if (!el) return;
    if (el._orig === undefined) el._orig = el.innerHTML;
    if (decision === 'approve' && block.querySelector('.ia-ed[data-edited]')) {
      collapse(block, true);   // the author's own wording: the panel re-renders the card from the committed text
    } else if (decision === 'approve') {
      var pane = block.querySelector('.ia-newview');
      var ps = pane.querySelectorAll(':scope > p');
      el.innerHTML = ps.length === 1 ? ps[0].innerHTML : pane.innerHTML;
      el.classList.add('rw-applied'); collapse(block, true);
    } else {
      el.innerHTML = el._orig; el.classList.remove('rw-applied');
      if (decision === 'reject') collapse(block, true);
    }
  };

  /* ── checklists ────────────────────────────────────────── */
  function ckUpdate(block) {
    var items = IA.all(block, '.ia-ck'), d = items.filter(function (i) { return i.dataset.state === 'done'; }).length;
    var bar = block.querySelector('.ia-prog div'); if (bar) bar.style.width = (items.length ? 100 * d / items.length : 0) + '%';
    var s = IA.own(block, '.ia-status');
    if (s) { s.textContent = d + ' / ' + items.length + ' done'; s.classList.add('on'); s.classList.remove('seen'); }
  }
  IA.tick = function (id, item, state, quiet) {
    var block = blockById(id); if (!block || block.dataset.iaKind !== 'checklist') return false;
    var items = IA.all(block, '.ia-ck'), li = null;
    if (/^\d+$/.test(String(item))) li = items[+item - 1];
    else { var q = String(item).toLowerCase(); li = items.filter(function (i) { return i.dataset.src.toLowerCase().indexOf(q) >= 0; })[0]; }
    if (!li) return false;
    li.dataset.state = state;
    if (!quiet) { li.classList.remove('flash'); void li.offsetWidth; li.classList.add('flash'); }
    ckUpdate(block); return true;
  };
  document.addEventListener('click', function (e) {
    var t = e.target.nodeType === 1 ? e.target : e.target.parentElement;
    if (!t) return;
    var box = t.closest('.ia-box');
    if (box) {
      var li = box.closest('.ia-ck'), block = li.closest('.ia');
      var st = li.dataset.state === 'done' ? 'todo' : 'done';
      li.dataset.state = st; ckUpdate(block);
      IA.send({ type: 'check', id: block.dataset.iaId, title: block.dataset.title || '', item: +li.dataset.n,
                text: li.dataset.src, state: st }, block);
      return;
    }
    var exp = t.closest('.ia-expand');
    if (exp) { var bl = exp.closest('.ia'); collapse(bl, !bl.classList.contains('ia-collapsed')); return; }
    var col = t.closest('.ia.ia-collapsed');
    if (col && t.closest('.ia-head')) collapse(col, false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !e.target.closest || !e.target.closest('.ia-ckadd')) return;
    e.stopPropagation();
    var inp = e.target, block = inp.closest('.ia'), v = inp.value.trim(); if (!v) return;
    if (IA.send({ type: 'check_add', id: block.dataset.iaId, title: block.dataset.title || '', text: v }, block)) inp.value = '';
  }, true);

  /* ── restore + init ────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    rewritables().forEach(function (el, i) { el.dataset.para = i; el.tabIndex = 0; });
    var saved = window._iaSaved || {};
    Object.keys(saved.suggestions || {}).forEach(function (para) {
      var sg = saved.suggestions[para], block = IA.suggest(para, sg.html, true);
      var ev = block && (saved.decisions || {})[block.dataset.iaId];
      if (ev) { IA.markDecision(block, ev); if (ev.seq) IA._bySeq[ev.seq] = { el: block, type: ev.type }; }
    });
    (saved.pending_rw || []).forEach(function (p) {
      var el = document.querySelector('[data-para="' + p + '"]'); if (el) el.classList.add('rw-pending');
    });
    IA.all(document, '.ia-checklist').forEach(ckUpdate);
    (saved.ticks || []).forEach(function (t) { IA.tick(t[0], t[1], t[2], true); });
    (saved.replies || []).forEach(function (r) { IA.reply(r[0], r[1], r[2], true); });
    IA.markAwaiting();
    IA.seen(saved.cursor || 0, true);
  });
})();
</script>
"""

# third chunk: in-place edits (revised badges, stale pins, scroll restore)
HEAD += r"""
<style>
  .ia-revnote { padding: 6px 14px; font-size: 13px; color: #6b4e00; background: #fbf3dc; border-bottom: 1px solid #ecdcae; }
  .ia-restored { box-shadow: 0 0 0 2px #f3d98b; }
  .ia-pin.old { opacity: .45; background: #8a96a3; outline: 2px dashed #8a96a3; outline-offset: 1px; }
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
  var IA = window.IA, saved = window._iaSaved || {};
  setTimeout(function () { if (IA.restoreDrafts) IA.restoreDrafts(); }, 0);
  Object.keys(saved.revised || {}).forEach(function (id) {
    var b = IA.all(document, '.ia[data-ia-id]').filter(function (x) { return x.dataset.iaId === id; }).pop();
    if (!b) return;
    var s = IA.own(b, '.ia-status');
    if (s && s.classList.contains('wait')) s.textContent = 'revised · awaiting you';
    var n = document.createElement('div'); n.className = 'ia-revnote';
    n.textContent = 'Revised after your earlier response: ' + saved.revised[id];
    var head = IA.own(b, '.ia-head'); if (head) head.after(n);
  });
  Object.keys(saved.old_pins || {}).forEach(function (id) {
    var view = IA.all(document, '.ia-view').filter(function (b) { return b.dataset.iaId === id; })[0];
    if (!view) return;
    saved.old_pins[id].forEach(function (p) {
      var page = IA.all(view, '.ia-page').filter(function (x) { return +x.dataset.page === p.page; })[0];
      if (!page) return;
      var pz = page.querySelector('.ia-pinnable[data-which="' + p.pin_which + '"]') || page.querySelector('.ia-pinnable');
      var pin = IA.placePin(pz, p.x, p.y, p.n); pin.classList.add('old'); pin.title = 'Before revision: ' + p.comment;
    });
  });
  if (saved.scroll) {
    var y = saved.scroll, go = function () { window.scrollTo(0, y); };
    go(); setTimeout(go, 50);
    try { MathJax.startup.promise.then(function () { go(); setTimeout(go, 400); }); } catch (e) {}
    IA.toast('Updated in place');
  }
});
</script>
"""

# fourth chunk: editable proposals (diff "New" text, choice options): plain-text editor, live word diff,
# edited -> sent for review -> Claude replied -> approve my version
HEAD += r"""
<style>
  .ia-edhint { padding: 6px 0 0; }
  .ia-ed { padding: 10px 0 12px; }
  .ia-ed .ia-edta { width: 100%; box-sizing: border-box; font: 400 15px/1.7 system-ui, 'Segoe UI', sans-serif; padding: 12px 14px;
    border: 1px solid #c9d3de; border-radius: 8px; background: #fff; color: #18222d; resize: vertical; min-height: 9.5em;
    white-space: pre-wrap; outline: none; }
  .ia-ed .ia-edta[hidden] { display: none; }
  .ia-ed .ia-edrich { box-sizing: border-box; width: 100%; font: 400 15.5px/1.8 'Source Serif 4', Georgia, 'Times New Roman', serif; padding: 12px 14px;
    border: 1px solid #c9d3de; border-radius: 8px; background: #fff; color: #18222d; min-height: 9.5em; white-space: pre-wrap; outline: none; }
  .ia-ed .ia-edrich:focus { border-color: #2b5d8a; box-shadow: 0 0 0 3px rgba(43, 93, 138, .16); }
  .ia-ed[data-edited] .ia-edrich { border-color: #b58a1c; }
  .ia-ed[data-src] .ia-edrich { display: none; }
  .ia-ed[data-src] .ia-edta { display: block; }
  .ia-chip { display: inline-block; font: 500 13px/1.45 system-ui, sans-serif; padding: 0 6px; margin: 0 1px; border-radius: 5px;
    background: #e6eef7; color: #2b5d8a; user-select: all; cursor: default; white-space: nowrap; }
  .ia-chip.ref { background: #eceff3; color: #52606f; }
  .ia-chip.fmt { background: none; color: inherit; padding: 0; font: inherit; }
  .ia-chip.math { background: #f3f6f9; color: #18222d; padding: 0 5px; font-size: 15px; }
  .ia-ed .ia-edta:focus { border-color: #2b5d8a; box-shadow: 0 0 0 3px rgba(43, 93, 138, .16); }
  .ia-ed[data-edited] .ia-edta { border-color: #b58a1c; }
  .ia-edbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; flex-wrap: wrap; }
  .ia-edsteps { display: flex; align-items: center; font: 12.5px system-ui, sans-serif; color: #8996a5; min-height: 28px; }
  .ia-edsteps[hidden] { display: flex; visibility: hidden; }
  .ia-edsteps span { padding: 0; background: none; border: 0; border-radius: 0; }
  .ia-edsteps span + span::before { content: "\203A"; margin: 0 9px; color: #b9c4d0; }
  .ia-edsteps span.done { color: #7a5a00; }
  .ia-edsteps span.now { color: #18222d; font-weight: 600; box-shadow: none; }
  .ia-ed.seen .ia-edsteps span[data-s="2"].done::after { content: " \2713"; }
  .ia-edsteps span[data-s="3"].done { color: #14532d; }
  .ia-edsteps span[data-s="4"].ready { color: #14532d; font-weight: 700; }
  .ia-ed .ia-edlive { margin-top: 12px; padding: 10px 14px 12px; background: #f4f7fa; border: 0; border-radius: 8px; font: 400 14px/1.7 system-ui, 'Segoe UI', sans-serif; }
  .ia-ed .ia-edlive::before { content: attr(data-cap); display: block; font: 600 11.5px system-ui, sans-serif; letter-spacing: .02em;
    text-transform: uppercase; color: #6b7a8b; padding: 0 0 4px; }
  .ia-edactions { display: flex; gap: 8px; margin: 0; }
  .ia-edalt { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-top: 10px; }
  .ia-edalt > div { border: 1px solid #c3cbd5; border-radius: 6px; background: #fff; padding: 6px 10px 8px; }
  .ia-edalt > div.claude { border-left: 3px solid #2b5d8a; }
  .ia-edalt b { font: 600 12px system-ui, sans-serif; color: #3f536a; }
  .ia-btn[disabled] { opacity: .45; cursor: not-allowed; }
  .ia-oedit { float: right; font: 12px system-ui, sans-serif; padding: 1px 8px; }
  .ia-opt:not(.editing) .ia-ed { display: none; }
  .ia-opt.editing .ia-obody { display: none; }
</style>
<script>
(function () {
  var IA = window.IA, $all = IA.all;
  var TOK = /\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\s+|[^\s$]+|\$/g;
  function norm(s) { return String(s == null ? '' : s).replace(/\r\n?/g, '\n').replace(/\s+$/, ''); }

  /* word diff of plain text, same tokens as the Python side; returns a DocumentFragment of text / <del> / <ins> */
  function diffFrag(a, b) {
    var A = norm(a).match(TOK) || [], B = norm(b).match(TOK) || [], n = A.length, m = B.length, f = document.createDocumentFragment();
    function add(tag, toks) {
      var t = toks.join(''); if (!t) return;
      if (!tag) { f.appendChild(document.createTextNode(t)); return; }
      var e = document.createElement(tag); e.textContent = t; f.appendChild(e);
    }
    if (n + m === 0) return f;
    if (n * m > 4e6) { add('del', A); add('ins', B); return f; }
    var W = m + 1, dp = new Uint16Array((n + 1) * W);
    for (var i = n - 1; i >= 0; i--) for (var j = m - 1; j >= 0; j--)
      dp[i * W + j] = A[i] === B[j] ? dp[(i + 1) * W + j + 1] + 1 : Math.max(dp[(i + 1) * W + j], dp[i * W + j + 1]);
    if (2 * dp[0] / (n + m) < 0.45) { add('del', A); f.appendChild(document.createTextNode('\n\n')); add('ins', B); return f; }
    var i2 = 0, j2 = 0, eq = [], del = [], ins = [];
    function flush() { add(null, eq); add('del', del); add('ins', ins); eq = []; del = []; ins = []; }
    while (i2 < n || j2 < m) {
      if (i2 < n && j2 < m && A[i2] === B[j2]) { if (del.length || ins.length) { add(null, eq); eq = []; add('del', del); add('ins', ins); del = []; ins = []; } eq.push(A[i2]); i2++; j2++; }
      else if (j2 >= m || (i2 < n && dp[(i2 + 1) * W + j2] >= dp[i2 * W + j2 + 1])) del.push(A[i2++]);
      else ins.push(B[j2++]);
    }
    flush(); return f;
  }
  function fill(el, frag) { while (el.firstChild) el.removeChild(el.firstChild); el.appendChild(frag); }

  IA.edFind = function (id, opt) {
    var blocks = $all(document, '.ia[data-ia-id]').filter(function (b) { return b.dataset.iaId === id; });
    var block = blocks[blocks.length - 1]; if (!block) return null;
    return $all(block, '.ia-ed').filter(function (e) {
      return e.closest('.ia') === block && (opt == null ? e.dataset.opt === undefined : e.dataset.opt === opt);
    })[0] || null;
  };
  function key(ed) { var b = ed.closest('.ia'); return 'e:' + b.dataset.iaId + (ed.dataset.opt !== undefined ? ':' + ed.dataset.opt : ''); }
  function stOf(ed) { return ed._st || (ed._st = { sent: null, sending: null, replied: false, alt: null }); }
  function textOf(ed) { return norm(ed.querySelector('textarea').value); }
  /* 0 = as proposed, 1 = edited, 2 = sent for review, 3 = Claude replied (the edited version may be approved) */
  function stepOf(ed) {
    var t = textOf(ed), st = stOf(ed);
    if (t === norm(ed.dataset.orig)) return 0;
    if (st.alt !== null && t === norm(st.alt)) return 3;
    if (st.sent !== null && t === st.sent) return st.replied ? 3 : 2;
    return 1;
  }
  IA.edOpen = function (ed) { var o = ed.closest('.ia-opt'); if (o) o.classList.add('editing'); };

  // grow the box to its text; a box in a hidden tab measures 0, so refit when it becomes visible
  function fit(ta) {
    function go() { ta.style.height = 'auto'; var h = ta.scrollHeight; if (h) ta.style.height = (h + 2) + 'px'; }
    go();
    if (!ta._ro && window.ResizeObserver) {
      ta._w = ta.clientWidth;
      ta._ro = new ResizeObserver(function () { if (ta.clientWidth !== ta._w) { ta._w = ta.clientWidth; if (ta._w) go(); } });
      ta._ro.observe(ta);
    }
  }

  // ── rendered editing view: prose is editable text; citations, references and maths are atomic chips that carry
  // their LaTeX source in data-src, so the textarea (the source of truth) round-trips exactly.
  var CHIP = /\\(citep|citet|citealt|citep\*|cite)\{([^}]*)\}|(?:~)?\\(ref|eqref|cref)\{([^}]*)\}|\$\$[^$]+\$\$|\$[^$\n]+\$|\\textsc\{([^}]*)\}|\\emph\{([^}]*)\}|\\,|~/g;
  function citeLabel(k) {
    var m = /^([A-Za-z]+?)_?((?:19|20)\d\d)/.exec(k.trim());
    return m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' ' + m[2] : k.trim();
  }
  function chipEl(src, m) {
    var s = document.createElement('span'); s.className = 'ia-chip'; s.contentEditable = 'false'; s.dataset.src = src;
    if (m[1]) {
      var parts = m[2].split(',').map(citeLabel);
      s.textContent = m[1] === 'citet' ? parts.join('; ').replace(/ (\d{4})/, ' ($1)') : '(' + parts.join('; ') + ')'; s.title = src;
    } else if (m[3]) { s.className += ' ref'; s.textContent = '▸ ' + m[4]; s.title = src; }
    else if (src === '\\,') { s.className += ' fmt'; s.textContent = ' '; }
    else if (src === '~') { s.className += ' fmt'; s.textContent = ' '; }
    else if (m[5] !== undefined) { s.className += ' fmt'; s.textContent = m[5]; s.style.fontVariant = 'small-caps'; s.title = src; }
    else if (m[6] !== undefined) { s.className += ' fmt'; s.textContent = m[6]; s.style.fontStyle = 'italic'; s.title = src; }
    else { s.className += ' math'; s.textContent = src.indexOf('$$') === 0 ? '\\[' + src.slice(2, -2) + '\\]' : '\\(' + src.slice(1, -1) + '\\)'; s.title = src; }
    return s;
  }
  function richBuild(ed) {
    var box = ed.querySelector('.ia-edrich'), text = ed.querySelector('textarea').value.replace(/^\n/, '');
    box.innerHTML = '';
    var last = 0, m; CHIP.lastIndex = 0;
    while ((m = CHIP.exec(text))) {
      if (m.index > last) box.appendChild(document.createTextNode(text.slice(last, m.index)));
      box.appendChild(chipEl(m[0], m)); last = m.index + m[0].length;
      if (m[0].length === 0) CHIP.lastIndex++;
    }
    if (last < text.length) box.appendChild(document.createTextNode(text.slice(last)));
    try { var maths = box.querySelectorAll('.ia-chip.math'); if (maths.length) MathJax.startup.promise.then(function () { return MathJax.typesetPromise([box]); }); } catch (e) {}
  }
  function richText(box) {
    var out = '';
    (function walk(n) {
      for (var c = n.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3) out += c.nodeValue.replace(/ /g, ' ');
        else if (c.nodeType === 1) {
          if (c.dataset && c.dataset.src !== undefined) out += c.dataset.src;
          else if (c.tagName === 'BR') out += '\n';
          else { if (out && !/\n$/.test(out) && /^(DIV|P)$/.test(c.tagName)) out += '\n'; walk(c); }
        }
      }
    })(box);
    return out;
  }
  function richSync(ed, force) {   // textarea -> rendered view when they differ (reset, restore, Claude's wording)
    var box = ed.querySelector('.ia-edrich'), ta = ed.querySelector('textarea');
    if (force || richText(box) !== ta.value.replace(/^\n/, '')) richBuild(ed);
  }
  function richInput(ed) {         // rendered view -> textarea (the source of truth), then the usual input handling
    var ta = ed.querySelector('textarea'); ta.value = '\n' + richText(ed.querySelector('.ia-edrich'));
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }
  // typing the closing brace or dollar of \citep{..}, \ref{..} or $..$ turns it into a chip at once, caret kept after it
  var TAIL = /(\\(?:citep|citet|citealt|cite)\{[^}]*\}|~?\\(?:ref|eqref|cref)\{[^}]*\}|\$[^$\n]+\$)$/;
  function chipAtCaret(box) {
    var sel = getSelection(); if (!sel.rangeCount || !sel.isCollapsed) return;
    var n = sel.anchorNode; if (!n || n.nodeType !== 3 || !box.contains(n)) return;
    var off = sel.anchorOffset, before = n.nodeValue.slice(0, off), m = TAIL.exec(before); if (!m) return;
    var re = new RegExp(CHIP.source), mm = re.exec(m[0]); if (!mm || mm[0] !== m[0]) return;
    var chip = chipEl(m[0], mm), after = document.createTextNode(n.nodeValue.slice(off));
    n.nodeValue = before.slice(0, m.index); n.parentNode.insertBefore(chip, n.nextSibling); n.parentNode.insertBefore(after, chip.nextSibling);
    var r = document.createRange(); r.setStart(after, 0); r.collapse(true); sel.removeAllRanges(); sel.addRange(r);
    try { if (chip.classList.contains('math')) MathJax.startup.promise.then(function () { return MathJax.typesetPromise([chip]); }); } catch (e) {}
  }
  document.addEventListener('input', function (e) {
    var b = e.target; if (!b || !b.classList || !b.classList.contains('ia-edrich')) return;
    richInput(b.closest('.ia-ed'));
    if (e.inputType === 'insertText' && (e.data === '}' || e.data === '$')) chipAtCaret(b);
  });
  document.addEventListener('keydown', function (e) {
    var b = e.target; if (!b || !b.classList || !b.classList.contains('ia-edrich')) return;
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); var s = b.closest('.ia-ed').querySelector('[data-act=ed-send]'); if (!s.disabled) s.click(); return; }
    if (e.key === 'Enter') { e.preventDefault(); document.execCommand('insertText', false, '\n'); }
  });
  document.addEventListener('paste', function (e) {
    var b = e.target; if (!b || !b.classList || !b.classList.contains('ia-edrich')) return;
    e.preventDefault(); var txt = (e.clipboardData || window.clipboardData).getData('text/plain');
    document.execCommand('insertText', false, txt); setTimeout(function () { var ed = b.closest('.ia-ed'); richInput(ed); richBuild(ed); }, 0);
  });
  document.addEventListener('focusout', function (e) {   // chipify anything typed as raw LaTeX once the box loses focus
    var b = e.target; if (b && b.classList && b.classList.contains('ia-edrich')) richBuild(b.closest('.ia-ed'));
  });
  function render(ed) {
    var ta = ed.querySelector('textarea'), st = stOf(ed), step = stepOf(ed), block = ed.closest('.ia'), isOpt = !!ed.closest('.ia-opt');
    fit(ta); richSync(ed);
    if (step) ed.setAttribute('data-edited', ''); else ed.removeAttribute('data-edited');
    var steps = ed.querySelector('.ia-edsteps'); steps.hidden = !step;
    $all(steps, 'span').forEach(function (sp) {
      var s = +sp.dataset.s;
      sp.classList.toggle('done', s <= step && s < 4);
      sp.classList.toggle('now', s === step);
      sp.classList.toggle('ready', s === 4 && step === 3);
    });
    var live = ed.querySelector('.ia-edlive');
    live.hidden = !step;
    if (step) { live.dataset.cap = isOpt ? 'Your changes to this option' : 'Your changes to the proposed wording'; fill(live, diffFrag(ed.dataset.orig, textOf(ed))); }
    var send = ed.querySelector('[data-act=ed-send]');
    send.disabled = step !== 1;
    send.textContent = st.sent !== null && step === 1 ? 'Send my changes again' : 'Send my version for review';
    send.title = step === 1 ? 'Claude will give an opinion on your wording before anything is merged' :
                 step === 0 ? 'Nothing changed yet' : step === 2 ? 'Sent - waiting for Claude' : 'Claude has replied';
    ed.querySelector('[data-act=ed-reset]').hidden = !step;
    if (!isOpt) {   // diff: the Changes tab and the Approve button follow the edited text
      var pane = block.querySelector('.ia-pane'), body = block.querySelector('.ia-body');
      if (pane && body) {
        if (step && !pane._orig) pane._orig = pane.innerHTML;
        if (step) { var d = document.createElement('div'); d.className = 'ia-src tex2jax_ignore'; fill(d, diffFrag(ed.dataset.base, textOf(ed))); pane.innerHTML = ''; pane.appendChild(d); }
        else if (pane._orig) { pane.innerHTML = pane._orig; pane._orig = null; }
      }
      var ap = IA.own(block, '[data-act=approve]');
      if (ap) {
        ap.textContent = step ? '✓ Approve my version' : '✓ Approve';
        ap.disabled = step === 1 || step === 2;
        ap.title = step === 1 ? 'First press “Send my version for review” and wait for Claude’s opinion' :
                   step === 2 ? 'Waiting for Claude’s opinion on your version' : '';
      }
    } else {
      var opt = ed.closest('.ia-opt'), pk = opt.querySelector('[data-act=pick]');
      opt.dataset.step = step;
      if (pk) {
        var multi = block.dataset.multi === '1';
        pk.disabled = !multi && (step === 1 || step === 2);
        pk.title = pk.disabled ? 'Send your edited option for review first, then wait for Claude' : '';
      }
      var tg = opt.querySelector('.ia-oedit'); if (tg && step) { opt.classList.add('editing'); tg.textContent = 'Hide editor'; tg.setAttribute('aria-expanded', 'true'); }
    }
    renderAlt(ed);
  }
  function renderAlt(ed) {
    var st = stOf(ed), box = ed.querySelector('.ia-edalt');
    box.hidden = st.alt === null; if (st.alt === null) { box.innerHTML = ''; return; }
    box.innerHTML = '';
    var l = document.createElement('div'), r = document.createElement('div'); r.className = 'claude';
    var lh = document.createElement('b'); lh.textContent = 'Your version' + (st.sent !== null ? ' (as sent)' : '');
    var lp = document.createElement('div'); lp.className = 'ia-src tex2jax_ignore'; lp.textContent = st.sent !== null ? st.sent : textOf(ed);
    l.appendChild(lh); l.appendChild(lp);
    var rh = document.createElement('b'); rh.textContent = 'Claude’s alternative (changes to your version)';
    var rp = document.createElement('div'); rp.className = 'ia-src tex2jax_ignore'; fill(rp, diffFrag(st.sent !== null ? st.sent : textOf(ed), st.alt));
    var use = document.createElement('button'); use.type = 'button'; use.className = 'ia-btn ok'; use.dataset.act = 'ed-use'; use.textContent = 'Use Claude’s wording';
    r.appendChild(rh); r.appendChild(rp); r.appendChild(use);
    box.appendChild(l); box.appendChild(r);
  }
  IA.edRefresh = render;
  IA.edRefreshAll = function () { $all(document, '.ia-ed').forEach(render); };

  /* unsent edited text is the draft: saved per keystroke so a re-render can't eat it and `panel.sh edit` is refused */
  function sync(ed, now) {
    var k = key(ed), t = textOf(ed), st = stOf(ed);
    var unsent = t !== norm(ed.dataset.orig) && t !== st.sent;
    clearTimeout(IA._dT[k]);
    var go = function () { IA.postDraft(k, unsent ? ed.querySelector('textarea').value : ''); };
    if (now) go(); else IA._dT[k] = setTimeout(go, 250);
  }
  document.addEventListener('input', function (e) {
    var ta = e.target; if (!ta || !ta.hasAttribute || !ta.hasAttribute('data-ia-edit')) return;
    var ed = ta.closest('.ia-ed'); render(ed); sync(ed, false);
  }, true);
  document.addEventListener('keydown', function (e) {   // Ctrl+Enter in the editor = Send my version for review
    var ta = e.target; if (!ta || !ta.hasAttribute || !ta.hasAttribute('data-ia-edit')) return;
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); var b = ta.closest('.ia-ed').querySelector('[data-act=ed-send]'); if (!b.disabled) b.click(); }
  }, true);

  IA.edUnsent = function (ed) { var st = stOf(ed); st.sending = null; st.sent = null; st.replied = false; render(ed); sync(ed, true); };
  IA.edConfirmed = function (ed) {   // the panel has logged the event
    var st = stOf(ed); if (st.sending === null) return;
    st.sent = st.sending; st.sending = null; st.replied = false; st.alt = null; render(ed); sync(ed, true);
  };
  IA.editReplied = function (id, opt, alt, quiet) {   // Claude answered the sent version (alt = optional alternative wording)
    var ed = IA.edFind(id, opt == null ? null : opt); if (!ed) return;
    var st = stOf(ed); if (st.sent === null) st.sent = textOf(ed);
    st.replied = true; if (alt != null) st.alt = alt;
    render(ed);
  };
  IA.edFinal = function (block, label, text) {   // an approved / picked edited version, restored after a re-render
    var ed = IA.edFind(block.dataset.iaId, label); if (!ed) return;
    ed.querySelector('textarea').value = text;
    var st = stOf(ed); st.sent = norm(text); st.replied = true; render(ed); sync(ed, true);
  };
  IA.edLoad = function (rec) {   // restore a sent edit from the panel's record of it
    var ed = IA.edFind(rec.id, rec.option == null ? null : rec.option); if (!ed) return;
    ed.querySelector('textarea').value = rec.text;
    var st = stOf(ed); st.sent = norm(rec.text); st.replied = !!rec.replied; st.alt = rec.alt == null ? null : rec.alt;
    render(ed);
  };

  IA.edAction = function (b, block) {
    var act = b.dataset.act, ed = b.closest('.ia-ed') || (b.closest('.ia-opt') || block).querySelector('.ia-ed');
    if (act === 'ed-open') {
      var o = b.closest('.ia-opt'), on = !o.classList.contains('editing');
      if (!on && stepOf(ed)) return IA.toast('Reset the option to hide an edited editor', true);
      o.classList.toggle('editing', on); b.textContent = on ? 'Hide editor' : '✎ Edit wording'; b.setAttribute('aria-expanded', String(on));
      if (on) ed.querySelector('textarea').focus();
      render(ed); return;
    }
    var ta = ed.querySelector('textarea'), st = stOf(ed);
    if (act === 'ed-src') { if (ed.hasAttribute('data-src')) { ed.removeAttribute('data-src'); richBuild(ed); b.textContent = 'Source'; } else { ed.setAttribute('data-src', ''); b.textContent = 'Rendered'; var q = ed.querySelector('textarea'); q.hidden = false; fit(q); } return; }
    if (act === 'ed-reset') { ta.value = ed.dataset.orig; render(ed); sync(ed, true); return; }
    if (act === 'ed-use') { ta.value = st.alt; render(ed); sync(ed, false); return; }
    if (act === 'ed-send') {
      if (stepOf(ed) !== 1) return;
      var ev = { type: 'diff_edit', id: block.dataset.iaId, title: block.dataset.title || '', original: ed.dataset.orig, edited: textOf(ed) };
      if (ed.dataset.opt !== undefined) ev.option = ed.dataset.opt; else ev.old = ed.dataset.base;
      var t = IA.own(block, '.ia-foot textarea'); if (t && t.value.trim()) ev.note = t.value.trim();
      if (IA.send(ev, ed)) { st.sending = ev.edited; IA.toast('Sent ✓ — Claude will give an opinion before you approve'); }
    }
  };
  /* a decision on a diff / choice carrying edited text: only after Claude has replied to it */
  IA.edApprove = function (block, ev) {
    var ed = IA.own(block, '.ia-ed'); if (!ed) return true;
    var step = stepOf(ed);
    if (step === 1 || step === 2) { IA.toast(step === 1 ? 'Send your version for review first' : 'Waiting for Claude’s opinion on your version', true); return false; }
    if (step === 3) { ev.edited_text = textOf(ed); ev.proposal_original = ed.dataset.orig; stOf(ed).sent = ev.edited_text; stOf(ed).replied = true; render(ed); sync(ed, true); }
    return true;
  };
  IA.edPick = function (opts, ev) {
    ev.picked_edited = [];
    for (var i = 0; i < opts.length; i++) {
      var ed = opts[i].querySelector('.ia-ed'), step = ed ? stepOf(ed) : 0;
      if (step === 1 || step === 2) { IA.toast('Option ' + opts[i].dataset.label + ': ' + (step === 1 ? 'send your version for review first' : 'waiting for Claude’s opinion'), true); return false; }
      ev.picked_edited.push(step === 3);
      if (step === 3) { ev.picked_text[i] = textOf(ed); }
    }
    return true;
  };

  document.addEventListener('DOMContentLoaded', function () {
    var saved = window._iaSaved || {};
    Object.keys(saved.edits || {}).forEach(function (k) { IA.edLoad(saved.edits[k]); });
    IA.edRefreshAll();
  });
  window.addEventListener('load', IA.edRefreshAll);   // textareas size themselves once fonts and layout are in
})();
</script>
"""

# fourth chunk: :::gallery multi-image viewer
HEAD += r"""
<style>
  .ia-gallery:focus { outline: 2px solid #2b5d8a; outline-offset: -2px; }
  .ia-gbar { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; padding: 6px 14px 0; }
  .ia-gbar:last-of-type { padding-bottom: 6px; }
  .ia-gbar .ia-sp { flex: 1; }
  .ia-gbar button.on { background: #2b5d8a; border-color: #2b5d8a; color: #fff; }
  .ia-gcols button b { font-family: 'JetBrains Mono', monospace; margin-right: 2px; }
  .ia-gstage { padding: 8px 14px; }
  .ia-gstage .ia-gcap { font: 600 13px system-ui, sans-serif; margin: 2px 0 6px; color: #1a212a; }
  .ia-gstage img { width: 100%; display: block; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.25); }
  .ia-gsingle img { max-height: 78vh; object-fit: contain; }
  .ia-ggrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); gap: 10px; }
  .ia-gmatrix { display: grid; gap: 6px; align-items: start; }
  .ia-gmatrix .ia-gh { font: 600 11px system-ui, sans-serif; color: #3f536a; }
  .ia-gtile { cursor: pointer; border: 2px solid transparent; border-radius: 4px; padding: 2px; }
  .ia-gtile:hover { border-color: #9aa9ba; }
  .ia-gtile.on { border-color: #2b5d8a; }
  .ia-gtile.star { box-shadow: 0 0 0 3px #e0b000; }
  .ia-gtile figcaption { font: 12px system-ui, sans-serif; margin-bottom: 2px; }
  .ia-gmissing { padding: 30px; text-align: center; color: #777; background: #fff; }
</style>
<script>
(function () {
  var $all = function (r, s) { return Array.prototype.slice.call(r.querySelectorAll(s)); };
  var active = null;
  function st(g) { return { row: +g.dataset.row, col: +g.dataset.col, alt: g.dataset.alt === '1', mode: g.dataset.mode }; }
  function cell(g, r, c, alt) {
    var s = g.querySelector('.ia-grow[data-row="' + r + '"] [data-c="' + c + '"]');
    if (!s) return '';
    return (alt && s.dataset.alt) ? s.dataset.alt : s.dataset.src;
  }
  function rowsOf(g) { return $all(g, '.ia-grow'); }
  function cols(g) { return g.dataset.cols.split('|'); }
  function img(src) { return src ? '<img src="' + src + '" draggable="false" loading="lazy">' : '<div class="ia-gmissing">no image</div>'; }
  function draw(g) {
    var s = st(g), C = cols(g), R = rowsOf(g), stage = g.querySelector('.ia-gstage'), stars = g._stars || {};
    var rt = R[s.row].dataset.title, alt = s.alt ? ' · ' + (g.dataset.altName || 'alternate') : '';
    $all(g, '[data-act=grow]').forEach(function (b) { b.classList.toggle('on', +b.dataset.row === s.row); });
    $all(g, '[data-act=gcol]').forEach(function (b) { b.classList.toggle('on', +b.dataset.col === s.col); });
    $all(g, '[data-act=gmode]').forEach(function (b) { b.classList.toggle('on', b.dataset.mode === s.mode); });
    $all(g, '[data-act=galt]').forEach(function (b) { b.classList.toggle('on', s.alt); });
    var h = '';
    if (s.mode === 'single') {
      h = '<div class="ia-gsingle"><div class="ia-gcap">' + esc(rt) + ' — ' + s.col + ' ' + esc(C[s.col]) + alt
        + (stars[s.row] === s.col ? ' ★' : '') + '</div>' + img(cell(g, s.row, s.col, s.alt)) + '</div>';
    } else if (s.mode === 'grid') {
      h = '<div class="ia-gcap">' + esc(rt) + alt + '</div><div class="ia-ggrid">' + C.map(function (c, j) {
        return '<figure class="ia-gtile' + (j === s.col ? ' on' : '') + (stars[s.row] === j ? ' star' : '') + '" data-r="' + s.row + '" data-c="' + j + '">'
          + '<figcaption><b>' + j + '</b> ' + esc(c) + (stars[s.row] === j ? ' ★' : '') + '</figcaption>' + img(cell(g, s.row, j, s.alt)) + '</figure>';
      }).join('') + '</div>';
    } else {
      h = '<div class="ia-gmatrix" style="grid-template-columns: 9em repeat(' + C.length + ', 1fr)"><span></span>'
        + C.map(function (c, j) { return '<span class="ia-gh">' + j + ' ' + esc(c) + '</span>'; }).join('');
      R.forEach(function (r, i) {
        h += '<span class="ia-gh">' + esc(r.dataset.title) + '</span>' + C.map(function (c, j) {
          return '<figure class="ia-gtile' + (i === s.row && j === s.col ? ' on' : '') + (stars[i] === j ? ' star' : '') + '" data-r="' + i + '" data-c="' + j + '">'
            + img(cell(g, i, j, s.alt)) + '</figure>';
        }).join('');
      });
      h += '</div>';
    }
    stage.innerHTML = h;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function set(g, k, v) { g.dataset[k] = v; draw(g); }
  function pick(g, all) {
    var s = st(g), C = cols(g), R = rowsOf(g), ta = g.querySelector('.ia-foot textarea');
    var rows = all ? R.map(function (_, i) { return i; }) : [s.row];
    g._stars = g._stars || {}; rows.forEach(function (i) { g._stars[i] = s.col; });
    var ev = { type: 'gallery', id: g.dataset.iaId, title: g.dataset.title || '', col: s.col, col_label: C[s.col],
               rows: rows.map(function (i) { return R[i].dataset.title; }), alt_shown: s.alt,
               note: ta ? ta.value.trim() : '' };
    if (IA.send(ev, g)) {
      var stx = g.querySelector('.ia-status'); if (stx) { stx.textContent = '★ ' + s.col + ' ' + C[s.col] + (all ? ' (all rows)' : ' for ' + R[s.row].dataset.title); stx.classList.add('on'); }
      draw(g);
    }
  }
  document.addEventListener('click', function (e) {
    var t = e.target.nodeType === 1 ? e.target : e.target.parentElement, g = t && t.closest('.ia-gallery');
    if (!g) return;
    active = g;
    var b = t.closest('[data-act]');
    if (b) {
      var a = b.dataset.act;
      if (a === 'grow') set(g, 'row', b.dataset.row);
      else if (a === 'gcol') set(g, 'col', b.dataset.col);
      else if (a === 'gmode') set(g, 'mode', b.dataset.mode);
      else if (a === 'galt') set(g, 'alt', g.dataset.alt === '1' ? '0' : '1');
      else if (a === 'gpick') pick(g, false);
      else if (a === 'gpickall') pick(g, true);
      else return;
      e.stopPropagation(); return;
    }
    var tile = t.closest('.ia-gtile');
    if (tile) { g.dataset.row = tile.dataset.r; g.dataset.col = tile.dataset.c; set(g, 'mode', 'single'); e.stopPropagation(); }
  }, true);
  document.addEventListener('mouseover', function (e) {
    var t = e.target.nodeType === 1 ? e.target : e.target.parentElement, g = t && t.closest && t.closest('.ia-gallery');
    active = g || null;   // keys drive a gallery only while the pointer is over it
  });
  document.addEventListener('keydown', function (e) {
    var tg = e.target;
    if (!active || !document.body.contains(active) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (tg && (tg.tagName === 'TEXTAREA' || tg.tagName === 'INPUT' || tg.isContentEditable)) return;
    var g = active, s = st(g), nc = cols(g).length, nr = rowsOf(g).length, k = e.key, done = true;
    if (/^[0-9]$/.test(k) && +k < nc) set(g, 'col', k);
    else if (k === 'ArrowRight') set(g, 'col', (s.col + 1) % nc);
    else if (k === 'ArrowLeft') set(g, 'col', (s.col + nc - 1) % nc);
    else if (k === 'ArrowDown') set(g, 'row', (s.row + 1) % nr);
    else if (k === 'ArrowUp') set(g, 'row', (s.row + nr - 1) % nr);
    else if (k === 'd' && g.querySelector('[data-act=galt]')) set(g, 'alt', s.alt ? '0' : '1');
    else if (k === 'g') set(g, 'mode', s.mode === 'grid' ? 'single' : 'grid');
    else if (k === 'm') set(g, 'mode', s.mode === 'matrix' ? 'single' : 'matrix');
    else if (k === 's') set(g, 'mode', 'single');
    else done = false;
    if (done) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener('DOMContentLoaded', function () { $all(document, '.ia-gallery').forEach(draw); });
})();
</script>
"""
