"""Interactive blocks that talk back to Claude.

Every button here posts a `claudeEvent` message to the GTK side, which appends it to
events.jsonl (see events.py); a Claude session streams that file with `panel.sh watch`.

Fenced (closed by a bare `:::` line):
    :::approve ID Title          content + Approve / Request changes / Reject + note
    :::diff ID [tex] Title       old text, a line `===`, new text -> word diff + same buttons
    :::choice ID [multi] Title   options introduced by `@@ Label` lines -> pick one (or several)
    :::params ID Title           `name = value [min, max, step]` / `{a, b}` / true / "text" lines
    :::checklist ID Title        `- [ ]` / `- [~]` (doing) / `- [x]` items; Claude ticks them live
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
            stack = (f'<div class="ia-stack ia-pinnable" data-which="stack">{img(old)}'
                     f'{img(new).replace("<img ", "<img class=\"b\" ", 1)}'
                     f'<span class="ia-flabel"></span></div>'
                     '<input type="range" class="ia-wipe" min="0" max="100" value="50">')
            out.append(
                f'<div class="ia-page" data-page="{p}">{pl}'
                f'<div class="ia-side"><figure class="ia-pinnable" data-which="old">{img(old)}'
                f'<figcaption>{H.escape(lab_old)}</figcaption></figure>'
                f'<figure class="ia-pinnable" data-which="new">{img(new)}'
                f'<figcaption>{H.escape(lab_new)}</figcaption></figure></div>'
                f'<div class="ia-stackwrap">{stack}</div></div>')
        else:
            out.append(f'<div class="ia-page" data-page="{p}">{pl}'
                       f'<div class="ia-single"><figure class="ia-pinnable" data-which="single">{img(new)}</figure></div></div>')
    out.append("</div>")
    return "".join(out)


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
            tabs = ('<div class="ia-tabs"><button class="on" data-act="tab" data-tab="0">Changes</button>'
                    '<button data-act="tab" data-tab="1">New</button><button data-act="tab" data-tab="2">Old</button></div>')
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
                             f'<div class="ia-olabel">{H.escape(lab)}</div><div class="ia-obody">{md(ob)}</div>'
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
                             f'data-src="{H.escape(mm.group(2), True)}"><span class="ia-box" title="click to toggle"></span>'
                             f'<span class="ia-ckt">{inner}</span></li>')
            return put(_open(kind, bid, title or "Checklist") + '<div class="ia-prog"><div></div></div>'
                       f'<ul class="ia-cklist">{"".join(items)}</ul>'
                       '<div class="ia-ckadd"><input placeholder="Suggest another item… (Enter)"></div></div>')
        if kind == "params":
            bid, _, title = _split_head(kind, head)
            return put(_open(kind, bid, title or "Parameters") + f'<div class="ia-pgrid">{params_form(body)}</div>'
                       '<div class="ia-foot"><textarea rows="1" placeholder="Note (optional)…"></textarea>'
                       '<div class="ia-actions"><button class="ia-btn ok" data-act="send">Send values ▶</button></div></div></div>')
        return m.group(0)

    text = re.sub(r"^:::(approve|diff|choice|params|checklist)\b[ \t]*(.*)\n([\s\S]*?)^:::[ \t]*$",
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
  .ia-side figcaption { font: 12px system-ui; text-align: center; color: #3f536a; }
  .ia-view img { width: 100%; display: block; box-shadow: 0 1px 4px rgba(0,0,0,.25); background: #fff; cursor: crosshair; user-select: none; }
  .ia-stackwrap, .ia-wipe { display: none; }
  .ia-view:not([data-mode=side]):not([data-mode=single]) .ia-side { display: none; }
  .ia-view:not([data-mode=side]):not([data-mode=single]) .ia-stackwrap { display: block; }
  .ia-stack { position: relative; width: calc(100% * var(--z)); isolation: isolate; }
  .ia-stack img.b { position: absolute; top: 0; left: 0; }
  .ia-view[data-mode=wipe] .ia-stack img.b { clip-path: inset(0 0 0 var(--wipe, 50%)); }
  .ia-view[data-mode=wipe] .ia-stack { box-shadow: none; }
  .ia-view[data-mode=wipe] .ia-wipe { display: block; width: calc(100% * var(--z)); margin: 6px 0 0; }
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
  #ia-toast { position: fixed; bottom: 64px; right: 16px; z-index: 60; padding: 6px 14px; border-radius: 16px; background: #2e7d4f;
              color: #fff; font: 13px system-ui; opacity: 0; transition: opacity .2s; pointer-events: none; }
  #ia-toast.show { opacity: 1; } #ia-toast.bad { background: #a33a3a; }
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
  IA._out = {}; IA._bySeq = {}; IA._cursor = 0;
  IA.send = function (ev, el) {
    ev.ts = new Date().toISOString();
    ev.cid = Math.random().toString(36).slice(2, 10);
    var sv = window._iaSaved || {};
    if (sv.card) { ev.card = sv.card; ev.rev = sv.rev; }   // which card revision this click was made on
    try { window.webkit.messageHandlers.claudeEvent.postMessage(ev); }
    catch (e) { toast('Panel not connected', true); return false; }
    IA._out[ev.cid] = { el: el || null, type: ev.type };
    toast('Sent ✓ — waiting for Claude'); return true;
  };
  IA.refused = function (cid, why) {   // the panel rejected a click (card changed under it)
    var o = IA._out[cid]; delete IA._out[cid];
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
    var p = document.createElement('div'); p.className = 'ia-pin'; p.textContent = n;
    p.style.left = (x * 100) + '%'; p.style.top = (y * 100) + '%'; pz.appendChild(p); return p;
  }
  function closePops() { $all(document, '.ia-pop').forEach(function (p) { p.remove(); }); }

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
    var pin = placePin(pz, x, y, n);
    var pop = document.createElement('div'); pop.className = 'ia-pop';
    pop.style.left = Math.min(x * 100, 60) + '%'; pop.style.top = 'calc(' + (y * 100) + '% + 16px)';
    pop.innerHTML = '<textarea rows="3" placeholder="Comment on this spot… (Ctrl+Enter sends)"></textarea>' +
      '<div class="ia-actions"><button class="ia-btn ok">Send</button><button class="ia-btn">Cancel</button></div>';
    pz.appendChild(pop);
    var ta = pop.querySelector('textarea'); ta.focus();
    function go() {
      var c = ta.value.trim(); if (!c) { ta.focus(); return; }
      var ev = base(view);
      ev.type = 'pin'; ev.n = n; ev.page = +page.dataset.page; ev.side = side === 'single' ? 'image' : label; ev.path = path;
      ev.x = Math.round(x * 1000) / 1000; ev.y = Math.round(y * 1000) / 1000; ev.comment = c;
      ev.pin_which = pz.dataset.which;
      if (IA.send(ev, pin)) { pin.title = c; pop.remove(); }
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
  .ia-reply .x { position: absolute; right: 6px; top: 4px; cursor: pointer; color: #3f536a; }
  #ia-chat { position: fixed; left: 12px; right: 12px; bottom: 54px; z-index: 49; max-height: 32vh; overflow: auto; }
  #ia-chat .ia-reply { margin: 6px 0 0; box-shadow: 0 2px 10px rgba(0,0,0,.18); }
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
  .ia-sugg { margin-top: -0.6em; }
  /* checklist */
  .ia-prog { height: 4px; background: #d3dbe4; }
  .ia-prog div { height: 100%; width: 0; background: #2e7d4f; transition: width .4s; }
  .ia-cklist { list-style: none; padding: 8px 16px 4px; margin: 0; }
  .ia-ck { display: flex; gap: 10px; align-items: baseline; padding: 4px 6px; border-radius: 4px; }
  .ia-box { flex: none; width: 15px; height: 15px; border: 1.5px solid #6986a7; border-radius: 4px; cursor: pointer;
            position: relative; top: 3px; box-sizing: border-box; }
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
    if (el.classList.contains('ia-pin')) { el.classList.add('seen'); return; }
    var s = IA.own(el, '.ia-status');
    if (s && s.classList.contains('on') && s.textContent.indexOf('seen') < 0) { s.textContent += ' · seen ✓'; s.classList.add('seen'); }
  }
  IA.sent = function (cid, seq) {
    var o = IA._out[cid]; if (!o) return;
    o.seq = seq; IA._bySeq[seq] = o;
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
  IA.reply = function (id, text, ts, quiet) {
    var d = document.createElement('div'); d.className = 'ia-reply';
    d.innerHTML = '<b>Claude · ' + hhmm(ts) + '</b>';
    var sp = document.createElement('span'); sp.textContent = text; d.appendChild(sp);
    var block = id ? blockById(id) : null;
    if (block) { block.appendChild(d); }
    else {
      var c = document.getElementById('ia-chat');
      if (!c) { c = document.createElement('div'); c.id = 'ia-chat'; document.body.appendChild(c); }
      var x = document.createElement('span'); x.className = 'x'; x.textContent = '✕';
      x.onclick = function () { d.remove(); }; d.appendChild(x); c.appendChild(d);
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
      b = document.createElement('button'); b.id = 'ia-rwbtn'; b.textContent = '✎'; b.title = 'Ask Claude to rewrite this';
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
  var CHIPS = ['Shorter', 'Clearer', 'More rigorous', 'Simpler', 'More formal', 'Fix errors'];
  function openRewrite(el) {
    if (!el) return;
    var old = document.getElementById('ia-rwpop'); if (old) old.remove();
    var r = el.getBoundingClientRect();
    var pop = document.createElement('div'); pop.id = 'ia-rwpop';
    pop.style.left = (Math.max(8, r.left + window.scrollX)) + 'px'; pop.style.top = (r.bottom + window.scrollY + 4) + 'px';
    pop.innerHTML = '<div class="chips">' + CHIPS.map(function (c) { return '<button>' + c + '</button>'; }).join('') + '</div>' +
      '<textarea rows="2" placeholder="How should it change? (Ctrl+Enter sends)"></textarea>' +
      '<div class="ia-actions"><button class="ia-btn ok">Ask for rewrite</button><button class="ia-btn">Cancel</button></div>';
    document.body.appendChild(pop);
    var ta = pop.querySelector('textarea'); ta.focus();
    IA.all(pop, '.chips button').forEach(function (c) {
      c.onclick = function () { ta.value = (ta.value ? ta.value.replace(/\s*$/, '; ') : '') + c.textContent.toLowerCase(); ta.focus(); };
    });
    function go() {
      var ins = ta.value.trim(); if (!ins) { ta.focus(); return; }
      var ev = { type: 'rewrite', para: +el.dataset.para, text: IA.fragText(el).trim(), instruction: ins,
                 context: el.matches('h1,h2,h3,h4') ? el.textContent.trim() : IA.headingBefore(el) };
      if (IA.send(ev, null)) { el.classList.add('rw-pending'); el.title = 'Waiting for Claude’s rewrite'; pop.remove(); }
    }
    var bs = pop.querySelectorAll('.ia-actions button');
    bs[0].onclick = go; bs[1].onclick = function () { pop.remove(); };
    ta.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter' && e.ctrlKey) go();
      if (e.key === 'Escape') pop.remove();
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
  IA.applyRewrite = function (block, decision) {
    var el = document.querySelector('[data-para="' + block.dataset.rw + '"]'); if (!el) return;
    if (el._orig === undefined) el._orig = el.innerHTML;
    if (decision === 'approve') {
      var pane = block.querySelectorAll('.ia-pane')[1];
      var ps = pane.querySelectorAll(':scope > p');
      el.innerHTML = ps.length === 1 ? ps[0].innerHTML : pane.innerHTML;
      el.classList.add('rw-applied'); block.classList.add('ia-collapsed');
    } else {
      el.innerHTML = el._orig; el.classList.remove('rw-applied');
      if (decision === 'reject') block.classList.add('ia-collapsed');
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
    var col = t.closest('.ia.ia-collapsed');
    if (col && t.closest('.ia-head')) col.classList.remove('ia-collapsed');
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !e.target.closest || !e.target.closest('.ia-ckadd')) return;
    e.stopPropagation();
    var inp = e.target, block = inp.closest('.ia'), v = inp.value.trim(); if (!v) return;
    if (IA.send({ type: 'check_add', id: block.dataset.iaId, title: block.dataset.title || '', text: v }, block)) inp.value = '';
  }, true);

  /* ── restore + init ────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    rewritables().forEach(function (el, i) { el.dataset.para = i; });
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
  .ia-pin.old { opacity: .45; background: #8a96a3; outline: 2px dashed #8a96a3; outline-offset: 1px; }
</style>
<script>
document.addEventListener('DOMContentLoaded', function () {
  var IA = window.IA, saved = window._iaSaved || {};
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
