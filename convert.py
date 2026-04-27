#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.31",
#   "markdown>=3.6",
#   "pymdown-extensions>=10.7",
#   "Pygments>=2.17",
#   "Pillow>=10.2",
#   "xhtml2pdf>=0.2.15",
#   "pypandoc-binary>=1.13",
#   "python-docx>=1.1",
# ]
# ///
"""
md_to_pdf_docx — one-shot converter (clean A4 layout)

What this build adds vs the previous version:
- A real cover page parsed from the Markdown (# Title + > subtitle).
- A dedicated Table of Contents page that auto-builds from ##/### headings.
- Every level-2 section ("## N. ...") starts on a NEW page.
- Diagrams + their caption are kept on the same page; oversized diagrams shrink
  to fit the remaining page real-estate (-pdf-keep-in-frame-mode: shrink).
- Subsections (### N.M ...) use a thin blue accent bar.
- Tables, lists, and code blocks avoid mid-row / mid-block page breaks.
- Page footer with "Page X of Y" on every page.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from PIL import Image

# ---------- A4 geometry --------------------------------------------------
A4_CONTENT_WIDTH_MM = 170
A4_CONTENT_HEIGHT_MM = 250
DPI = 150
PX_PER_MM = DPI / 25.4
MAX_DIAGRAM_W_PX = int(A4_CONTENT_WIDTH_MM * PX_PER_MM)
MAX_DIAGRAM_H_PX = int(A4_CONTENT_HEIGHT_MM * PX_PER_MM)

MERMAID_INK = "https://mermaid.ink/img/pako:{token}?type=png&bgColor=white"
MERMAID_FENCE = re.compile(r"^```mermaid\s*\n(.*?)\n```", re.MULTILINE | re.DOTALL)


@dataclass
class Diagram:
    index: int
    code: str
    caption: str
    png_path: Path


# ---------- Mermaid rendering -------------------------------------------

def _pako_encode(text: str) -> str:
    payload = (
        '{"code":' + _json_str(text) + ',"mermaid":{"theme":"default"}}'
    ).encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15)
    compressed = compressor.compress(payload) + compressor.flush()
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


def _render_via_mermaid_ink(code: str, out_png: Path) -> bool:
    url = MERMAID_INK.format(token=_pako_encode(code))
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=25)
            if r.status_code == 200 and r.content[:8].startswith(b"\x89PNG"):
                out_png.write_bytes(r.content)
                return True
            print(f"  mermaid.ink HTTP {r.status_code} (attempt {attempt + 1})")
        except requests.RequestException as e:
            print(f"  mermaid.ink error: {e} (attempt {attempt + 1})")
        time.sleep(1.2 * (attempt + 1))
    return False


def _render_via_mmdc(code: str, out_png: Path) -> bool:
    mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd") or shutil.which("mmdc.exe")
    if not mmdc:
        return False
    src = out_png.with_suffix(".mmd")
    src.write_text(code, encoding="utf-8")
    try:
        subprocess.run(
            [mmdc, "-i", str(src), "-o", str(out_png), "-b", "white",
             "-w", str(MAX_DIAGRAM_W_PX), "-s", "2"],
            check=True, capture_output=True, shell=False,
        )
        return out_png.exists()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        msg = getattr(e, "stderr", b"")
        if msg:
            print(f"  mmdc failed: {msg.decode(errors='ignore')[:200]}")
        else:
            print(f"  mmdc failed: {e}")
        return False
    finally:
        src.unlink(missing_ok=True)


def render_mermaid(code: str, out_png: Path) -> bool:
    if _render_via_mermaid_ink(code, out_png):
        return True
    print("  -> falling back to local mmdc (Node.js mermaid-cli)...")
    return _render_via_mmdc(code, out_png)


# ---------- A4 fit validation -------------------------------------------

def fit_to_a4(png: Path) -> tuple[int, int]:
    with Image.open(png) as im:
        im = im.convert("RGBA")
        w, h = im.size
        scale = min(MAX_DIAGRAM_W_PX / w, MAX_DIAGRAM_H_PX / h, 1.0)
        if scale < 1.0:
            new = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.resize(new, Image.LANCZOS)
            w, h = new
        im.save(png, "PNG", optimize=True)
        return w, h


# ---------- Markdown pre-processing -------------------------------------

def extract_caption_after(md: str, span_end: int) -> str:
    tail = md[span_end:span_end + 400]
    m = re.match(r"\s*\*Figure[^\n]*\*", tail)
    if not m:
        return ""
    # Strip whitespace first, then asterisks, then any leftover whitespace.
    return m.group(0).strip().strip("*").strip()


def preprocess(md_text: str, diagrams_dir: Path) -> tuple[str, list[Diagram]]:
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    diagrams: list[Diagram] = []

    def replace(match: re.Match) -> str:
        code = match.group(1).strip()
        idx = len(diagrams) + 1
        digest = hashlib.sha1(code.encode("utf-8")).hexdigest()[:10]
        png = diagrams_dir / f"diagram_{idx:02d}_{digest}.png"

        caption = extract_caption_after(md_text, match.end())
        print(f"[diagram {idx}] {caption or '(no caption)'}")

        if not png.exists():
            ok = render_mermaid(code, png)
            if not ok:
                print(f"  ! failed to render diagram {idx}; embedding raw code block instead.")
                return match.group(0)
        w, h = fit_to_a4(png)
        print(f"  rendered {png.name} ({w}x{h}px)")

        diagrams.append(Diagram(idx, code, caption, png))
        rel = png.relative_to(diagrams_dir.parent).as_posix()
        return f"![{caption or f'Diagram {idx}'}]({rel})"

    new_md = MERMAID_FENCE.sub(replace, md_text)
    return new_md, diagrams


# ---------- Cover & TOC parsing -----------------------------------------

@dataclass
class CoverInfo:
    title: str
    subtitle: str   # bold blue tagline
    italic_lines: list[str]
    info_lines: list[tuple[str, str]]  # (label, value)


def parse_cover(md_text: str) -> tuple[CoverInfo, str]:
    """Pull the first H1 + its blockquote into a cover-info struct.
    Returns (cover, remaining_md_with_those_chunks_removed).
    """
    lines = md_text.splitlines()
    out: list[str] = []
    title = ""
    subtitle = ""
    italic_lines: list[str] = []
    info_lines: list[tuple[str, str]] = []

    i = 0
    n = len(lines)
    consumed_intro = False

    while i < n:
        line = lines[i]
        if not consumed_intro and not title and line.lstrip().startswith("# ") and not line.lstrip().startswith("## "):
            title = line.lstrip()[2:].strip()
            # strip trailing " — Technical Documentation" decoration
            title = re.sub(r"\s*[—-]\s*Technical Documentation\s*$", "", title)
            i += 1
            # Eat blank lines
            while i < n and lines[i].strip() == "":
                i += 1
            # Read blockquote block
            bq: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                bq.append(lines[i].lstrip()[1:].strip())
                i += 1
            # First non-empty bq line = subtitle; rest = italic / info
            for raw in bq:
                if not raw:
                    continue
                if not subtitle:
                    subtitle = raw
                    continue
                m = re.match(r"\*\*([^*]+?)\*\*\s*[:：]?\s*(.+)", raw)
                if m:
                    label = m.group(1).rstrip(":：").strip()
                    info_lines.append((label, m.group(2).strip()))
                else:
                    italic_lines.append(raw)
            consumed_intro = True
            continue
        out.append(line)
        i += 1

    return (
        CoverInfo(title=title or "Document",
                  subtitle=subtitle,
                  italic_lines=italic_lines,
                  info_lines=info_lines),
        "\n".join(out),
    )


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "section"


def build_toc_html(md_text: str) -> str:
    """Build a clean, single-page TOC.

    Strategy that matches the reference layout:
      - Always include level-2 ("##") headings as numbered entries.
      - Include level-3 ("###") children ONLY when their parent level-2
        title contains the words "Core Flows" (those flow names are useful
        navigation aids; everything else would just bloat the TOC).
      - Skip the original document's "## Table of Contents" block.
    """
    items: list[tuple[int, str, str, str]] = []  # (level, parent_title, text, slug)
    in_fence = False
    current_l2 = ""
    for line in md_text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = m.group(2).strip()
        if text.lower().startswith("table of contents"):
            continue
        if level == 2:
            current_l2 = text
            items.append((2, "", text, slugify(text)))
        elif level == 3:
            if "core flows" in current_l2.lower():
                items.append((3, current_l2, text, slugify(text)))

    if not items:
        return ""

    rows: list[str] = []
    rows.append('<div class="toc-wrap">')
    rows.append('<h1 class="toc-h1">Table of Contents</h1>')
    rows.append('<table class="toc-table" cellpadding="0" cellspacing="0">')
    for level, _parent, text, _slug in items:
        cls = "toc-l2" if level == 2 else "toc-l3"
        rows.append(
            f'<tr><td class="{cls}">{text}</td></tr>'
        )
    rows.append('</table></div>')
    return "\n".join(rows)


def strip_inline_toc_block(md_text: str) -> str:
    """Remove the original '## Table of Contents' + its numbered list so we
    can replace it with the auto-generated, styled TOC page."""
    lines = md_text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip().lower().startswith("## table of contents"):
            # Skip until the next ## heading or "---" rule
            i += 1
            while i < n:
                l = lines[i].lstrip()
                if l.startswith("## ") or l.startswith("---"):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# ---------- HTML / CSS --------------------------------------------------

CSS_A4 = """
@page {
  size: a4 portrait;
  margin: 18mm 16mm 20mm 16mm;
  @frame footer {
    -pdf-frame-content: footerContent;
    bottom: 8mm; left: 16mm; right: 16mm; height: 8mm;
  }
}
@page cover {
  size: a4 portrait;
  margin: 0;
}
html { font-size: 10pt; }
body { font-family: Helvetica, Arial, sans-serif; color: #2c3540; line-height: 1.45; }

/* ---------- COVER ---------- */
.cover-wrap {
  page-break-after: always;
  text-align: center;
}
.cover-table {
  width: 100%;
  border: 0;
  border-collapse: collapse;
}
.cover-table td {
  border: 0;
  text-align: center;
  padding: 1.5pt 0;
}
.cover-spacer-top { height: 60mm; line-height: 60mm; }
.cover-spacer-md  { height: 12mm; line-height: 12mm; }
.cover-title    { font-size: 26pt; color: #1a3a5c; line-height: 1.25; }
.cover-subtitle { font-size: 14pt; color: #3a72c7; padding-top: 4mm !important; }
.cover-italic-line { font-size: 10.5pt; color: #7f8a98; line-height: 1.6; }
.cover-info-line   { font-size: 10.5pt; color: #2c3540; line-height: 1.9; }

/* ---------- TOC ---------- */
.toc-wrap {
  padding: 4mm 2mm 0 2mm;
  page-break-after: always;
}
.toc-h1 {
  font-size: 26pt;
  color: #1a3a5c;
  border-bottom: 2.4pt solid #1a3a5c;
  padding-bottom: 6pt;
  margin: 0 0 9mm 0;
  font-weight: bold;
}
.toc-table {
  width: 100%;
  border: 0;
  border-collapse: collapse;
  font-size: 10.5pt;
}
.toc-table td {
  border: 0;
  padding: 2.5pt 0;
  text-align: left;
}
.toc-l2 { color: #2c3540; }
.toc-l3 {
  color: #5a6470;
  font-size: 10pt;
  padding-left: 11mm !important;
}
.toc-table a { color: inherit; text-decoration: none; }

/* ---------- BODY HEADINGS ---------- */
h1 {
  font-size: 22pt;
  color: #1a3a5c;
  border-bottom: 2pt solid #1a3a5c;
  padding-bottom: 4pt;
  -pdf-keep-with-next: true;
}
h2 {
  font-size: 17pt;
  color: #1a3a5c;
  border-bottom: 1.2pt solid #1a3a5c;
  padding-bottom: 4pt;
  margin: 6mm 0 4mm 0;
  font-weight: bold;
  -pdf-keep-with-next: true;
}
h3 {
  font-size: 12.5pt;
  color: #1a3a5c;
  border-left: 3pt solid #3a72c7;
  padding-left: 6pt;
  margin: 5mm 0 2mm 0;
  font-weight: bold;
  -pdf-keep-with-next: true;
}
h4 {
  font-size: 10.5pt;
  color: #244062;
  margin: 3mm 0 1mm 0;
  -pdf-keep-with-next: true;
}

/* ---------- PARAGRAPHS / LISTS ---------- */
p { margin: 0 0 5pt 0; orphans: 3; widows: 3; }
ul, ol { margin: 3pt 0 6pt 6mm; padding: 0; }
li { margin: 2pt 0; page-break-inside: avoid; }

/* ---------- FIGURES (DIAGRAM + CAPTION) ---------- */
table.figure {
  width: 100%;
  border: 0;
  border-collapse: collapse;
  margin: 2mm 0 3mm 0;
  page-break-inside: avoid;
}
table.figure td {
  border: 0;
  padding: 0;
  text-align: center;
}
table.figure td.figure-img img { max-width: 168mm; }
table.figure td.figure-caption {
  font-size: 9pt;
  color: #7f8a98;
  padding-top: 3pt;
  font-style: italic;
}

/* ---------- TABLES ---------- */
table {
  border-collapse: collapse;
  width: 100%;
  margin: 6pt 0 10pt 0;
  font-size: 9.5pt;
  table-layout: fixed;
  word-wrap: break-word;
  -pdf-word-wrap: CJK;
  page-break-inside: auto;
}
table, th, td { border: 0.4pt solid #d8dde3; }
th, td {
  padding: 5pt 7pt;
  vertical-align: top;
  text-align: left;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  -pdf-word-wrap: CJK;
}
th {
  background-color: #eef3f9;
  color: #1a3a5c;
  font-weight: bold;
  border-bottom: 1pt solid #1a3a5c;
}
tr { page-break-inside: avoid; }
tr:nth-child(even) td { background-color: #fafbfc; }

td code, th code {
  font-size: 8.5pt;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  -pdf-word-wrap: CJK;
  white-space: normal;
}

/* ---------- INLINE CODE (pink/red on soft red) ---------- */
code {
  font-family: "Courier New", Courier, monospace;
  background-color: #fde8e8;
  color: #c0392b;
  padding: 1pt 4pt;
  font-size: 9pt;
  border-radius: 2pt;
}

/* ---------- CODE BLOCKS (light theme — reliable in xhtml2pdf) ---------- */
pre {
  background-color: #f4f6fa;
  color: #1f2a44;
  border: 0.4pt solid #d8dde3;
  border-left: 3pt solid #3a72c7;
  padding: 8pt 11pt;
  font-size: 8.5pt;
  font-family: "Courier New", Courier, monospace;
  white-space: pre-wrap;
  word-wrap: break-word;
  page-break-inside: avoid;
  margin: 4pt 0 8pt 0;
}
pre, pre * {
  background-color: #f4f6fa !important;
  color: #1f2a44 !important;
}
pre code, pre code * {
  background-color: transparent !important;
  color: #1f2a44 !important;
  padding: 0 !important;
  font-size: 8.5pt !important;
}

blockquote {
  border-left: 3pt solid #facc15;
  margin: 6pt 0;
  padding: 4pt 9pt;
  color: #4b5563;
  background-color: #fffbeb;
  font-size: 9.5pt;
  page-break-inside: avoid;
}

em { color: #5a6470; font-style: italic; }
strong { color: #1a3a5c; }
hr { border: 0; border-top: 0.5pt solid #d8dde3; margin: 10pt 0; }
"""

FOOTER_HTML = """
<div id="footerContent" style="font-size:8.5pt;color:#6b7280;text-align:right;font-family:Helvetica,Arial,sans-serif;">
  <span style="float:left;color:#9ca3af;">{title_short}</span>
  Page <pdf:pagenumber/> of <pdf:pagecount/>
</div>"""

HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>{footer}{cover}{toc}{body}</body></html>"""


_PRE_CODE_RE = re.compile(
    r'(<pre><code[^>]*>)([\s\S]*?)(</code></pre>)', re.IGNORECASE
)


def _preserve_code_newlines(html: str) -> str:
    """xhtml2pdf collapses newlines inside <pre><code>...</code></pre> into
    single spaces. Replace each newline with an explicit <br/> so each
    code line lands on its own row in the rendered PDF."""
    def repl(m: re.Match) -> str:
        opener, body, closer = m.group(1), m.group(2), m.group(3)
        # Preserve indentation: convert leading spaces on each line to &nbsp;.
        lines = body.split("\n")
        out: list[str] = []
        for line in lines:
            # Count leading spaces
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            out.append("&nbsp;" * indent + stripped)
        return opener + "<br/>".join(out) + closer
    return _PRE_CODE_RE.sub(repl, html)


_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_INLINE_CODE_IN_CELL_RE = re.compile(
    r"(<(?:td|th)[^>]*>)([\s\S]*?)(</(?:td|th)>)", re.IGNORECASE
)
_CODE_TAG_RE = re.compile(r"<code>([^<]+)</code>", re.IGNORECASE)
_BREAK_AFTER = set("/._-:?&=,")


def _segment_long_token(token: str, every: int = 14) -> list[str]:
    if len(token) <= every:
        return [token]
    parts: list[str] = []
    buf: list[str] = []
    run = 0
    for ch in token:
        buf.append(ch)
        run += 1
        if (ch in _BREAK_AFTER and run >= 4) or run >= every:
            parts.append("".join(buf))
            buf = []
            run = 0
    if buf:
        parts.append("".join(buf))
    return parts


def _wrap_code_for_table(text: str) -> str:
    segments = _segment_long_token(text)
    if len(segments) == 1:
        return f"<code>{segments[0]}</code>"
    return "".join(f"<code>{seg}</code><span></span>" for seg in segments)


def _make_table_cells_wrappable(html: str) -> str:
    def fix_cell(m: re.Match) -> str:
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)

        def fix_code(cm: re.Match) -> str:
            inner = cm.group(1)
            if len(inner) <= 18:
                return cm.group(0)
            return _wrap_code_for_table(inner)

        body = _CODE_TAG_RE.sub(fix_code, body)
        return open_tag + body + close_tag

    def fix_table(tm: re.Match) -> str:
        return _INLINE_CODE_IN_CELL_RE.sub(fix_cell, tm.group(0))

    return _TABLE_RE.sub(fix_table, html)


# ---------- Wrap each diagram + its caption into one keep-together block ---

# Markdown collapses `image\n*caption*` into a single paragraph. Match the
# common forms: image + em in the same <p> (with or without <br/>), or split
# across two <p> tags.
_FIG_SAMEP_RE = re.compile(
    r'<p>\s*(<img[^>]+/?>)\s*(?:<br\s*/?>\s*)?\s*<em>\s*(Figure[^<]+?)\s*</em>\s*</p>',
    re.IGNORECASE,
)
_FIG_PARA_RE = re.compile(
    r'<p>\s*(<img[^>]+/?>)\s*</p>\s*<p>\s*<em>\s*(Figure[^<]+?)\s*</em>\s*</p>',
    re.IGNORECASE,
)


def _wrap_figures(html: str) -> str:
    def repl(m: re.Match) -> str:
        img, cap = m.group(1), m.group(2).strip().rstrip(".")
        # Use a single-cell table so xhtml2pdf treats image + caption as
        # one keep-together block and the caption sits centred under the
        # image instead of running inline beside it.
        return (
            '<table class="figure" cellpadding="0" cellspacing="0">'
            f'<tr><td class="figure-img">{img}</td></tr>'
            f'<tr><td class="figure-caption"><i>{cap}.</i></td></tr>'
            '</table>'
        )

    html = _FIG_PARA_RE.sub(repl, html)
    html = _FIG_SAMEP_RE.sub(repl, html)
    return html


# ---------- Anchor injection so TOC links land on headings ---------------
_HEADING_TAG_RE = re.compile(r'<(h[23])>([^<]+)</\1>', re.IGNORECASE)


def _inject_heading_anchors(html: str) -> str:
    def repl(m: re.Match) -> str:
        tag, text = m.group(1).lower(), m.group(2)
        slug = slugify(re.sub(r"<[^>]+>", "", text))
        return f'<{tag} id="{slug}">{text}</{tag}>'
    return _HEADING_TAG_RE.sub(repl, html)


# ---------- Cover HTML ---------------------------------------------------

def render_cover(cover: CoverInfo) -> str:
    """Render cover as a table with one <tr> per line (xhtml2pdf collapses
    nested <br/> tags inside table cells, so we explode each line)."""
    rows: list[str] = []

    rows.append(
        '<tr><td class="cover-spacer-top">&nbsp;</td></tr>'
    )
    rows.append(
        f'<tr><td class="cover-title"><b>{cover.title}</b></td></tr>'
    )
    if cover.subtitle:
        rows.append(
            f'<tr><td class="cover-subtitle">{cover.subtitle}</td></tr>'
        )
    rows.append('<tr><td class="cover-spacer-md">&nbsp;</td></tr>')
    for ln in cover.italic_lines:
        rows.append(f'<tr><td class="cover-italic-line"><i>{ln}</i></td></tr>')
    rows.append('<tr><td class="cover-spacer-md">&nbsp;</td></tr>')
    for lbl, val in cover.info_lines:
        rows.append(
            '<tr><td class="cover-info-line">'
            f'<b>{lbl}:</b> {val}'
            '</td></tr>'
        )

    body = "\n".join(rows)
    return (
        '<div class="cover-wrap">'
        '<table class="cover-table" cellpadding="0" cellspacing="0">'
        f'{body}'
        '</table>'
        '</div>'
    )


# ---------- Markdown -> HTML pipeline -----------------------------------

def md_to_html(md_text: str, base_dir: Path, title: str) -> str:
    cover, body_md = parse_cover(md_text)
    body_md = strip_inline_toc_block(body_md)
    toc_html = build_toc_html(body_md)

    import markdown
    # Note: we deliberately omit pymdownx.superfences here. Superfences
    # injects Pygments syntax-highlighting <span> classes whose default
    # palette renders as near-white text on our dark <pre> background.
    # Plain fenced_code keeps the code as <pre><code>...</code></pre>
    # so our CSS controls the entire colour scheme.
    html_body = markdown.markdown(
        body_md,
        extensions=[
            "extra", "tables", "fenced_code", "sane_lists", "toc",
            "pymdownx.tilde", "pymdownx.tasklist", "pymdownx.magiclink",
        ],
        extension_configs={
            "pymdownx.tasklist": {"custom_checkbox": True},
            "toc": {"permalink": False},
        },
    )
    html_body = _preserve_code_newlines(html_body)
    html_body = _make_table_cells_wrappable(html_body)
    html_body = _wrap_figures(html_body)
    html_body = _inject_heading_anchors(html_body)

    return HTML_SHELL.format(
        title=title,
        css=CSS_A4,
        footer=FOOTER_HTML.format(title_short=cover.title or title),
        cover=render_cover(cover),
        toc=toc_html,
        body=html_body,
    )


def html_to_pdf(html: str, base_dir: Path, out_pdf: Path) -> None:
    from xhtml2pdf import pisa

    def link_callback(uri: str, rel: str) -> str:
        if uri.startswith(("http://", "https://", "data:")):
            return uri
        candidate = (base_dir / uri).resolve()
        return str(candidate)

    with open(out_pdf, "wb") as fh:
        result = pisa.CreatePDF(html, dest=fh, link_callback=link_callback, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s) while building {out_pdf.name}")


# ---------- DOCX --------------------------------------------------------

# Pandoc uses the styles from a reference DOCX as the base for every paragraph,
# heading, table, and code block. We build one programmatically so the Word
# document mirrors the PDF (same navy palette, blue-bar h3, light-blue table
# headers, light-grey code, centered figures, pink inline code).

_REF_RGB_NAVY      = (26, 58, 92)     # #1A3A5C — h1/h2/h3 colour
_REF_RGB_BLUE      = (58, 114, 199)   # #3A72C7 — subtitle / accent
_REF_RGB_BODY      = (44, 53, 64)     # #2C3540 — body text
_REF_RGB_GREY      = (127, 138, 152)  # #7F8A98 — captions, italic intro
_REF_RGB_PINK_BG   = (253, 232, 232)  # #FDE8E8 — inline code bg
_REF_RGB_PINK_FG   = (192, 57, 43)    # #C0392B — inline code text
_REF_RGB_CODE_BG   = (244, 246, 250)  # #F4F6FA — code block bg
_REF_RGB_TBLHEAD   = (238, 243, 249)  # #EEF3F9 — table header bg
_REF_RGB_BORDER    = (216, 221, 227)  # #D8DDE3 — table borders


def _build_reference_docx(out_path: Path) -> None:
    """Create a reference.docx whose styles match the PDF's palette.

    Pandoc 3.x writes DOCX with these style ids (no spaces): Heading1, Heading2,
    Heading3, SourceCode (paragraph), VerbatimChar (run), ImageCaption,
    CaptionedFigure, FirstParagraph, BodyText, Compact, Title, Subtitle.
    We define / override those exact ids so pandoc's pStyle / rStyle references
    bind to our styling.
    """
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Page setup: A4, narrow margins.
    for section in doc.sections:
        section.page_height = Cm(29.7)
        section.page_width  = Cm(21.0)
        section.top_margin  = Cm(2.0)
        section.bottom_margin = Cm(2.2)
        section.left_margin = Cm(1.8)
        section.right_margin = Cm(1.8)

    def get_or_add_style(name: str, kind: int):
        try:
            return doc.styles[name]
        except KeyError:
            return doc.styles.add_style(name, kind)

    def set_font(s, *, size=None, color=None, bold=None, italic=None, name=None):
        if s is None: return
        f = s.font
        if name is not None: f.name = name
        if size is not None: f.size = Pt(size)
        if color is not None: f.color.rgb = RGBColor(*color)
        if bold is not None: f.bold = bold
        if italic is not None: f.italic = italic

    def add_pPr_border(s, side, sz_8ths_pt, color_hex, space="1"):
        pPr = s.element.get_or_add_pPr()
        pBdr = pPr.find(qn("w:pBdr"))
        if pBdr is None:
            pBdr = OxmlElement("w:pBdr")
            pPr.append(pBdr)
        elem = OxmlElement(f"w:{side}")
        elem.set(qn("w:val"), "single")
        elem.set(qn("w:sz"), str(sz_8ths_pt))
        elem.set(qn("w:space"), space)
        elem.set(qn("w:color"), color_hex)
        pBdr.append(elem)

    def set_pPr_shading(s, fill_hex):
        pPr = s.element.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill_hex)
        pPr.append(shd)

    def set_rPr_shading(s, fill_hex):
        rpr = s.element.get_or_add_rPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill_hex)
        rpr.append(shd)

    # ---- Body / Normal.
    s = doc.styles["Normal"]
    set_font(s, name="Helvetica", size=10, color=_REF_RGB_BODY)
    s.paragraph_format.space_after = Pt(4)

    # ---- Title (cover H1).
    s = get_or_add_style("Title", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=26, color=_REF_RGB_NAVY, bold=True)
    s.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s.paragraph_format.space_after = Pt(4)

    # ---- Subtitle (cover tagline).
    s = get_or_add_style("Subtitle", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=14, color=_REF_RGB_BLUE)
    s.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s.paragraph_format.space_after = Pt(10)

    # ---- Heading1 (markdown `#` — only the cover doc title in our case).
    s = get_or_add_style("Heading1", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=22, color=_REF_RGB_NAVY, bold=True)
    s.paragraph_format.space_before = Pt(0)
    s.paragraph_format.space_after = Pt(6)
    add_pPr_border(s, "bottom", 16, "1A3A5C", space="1")

    # ---- Heading2 (markdown `##` — section headings).
    s = get_or_add_style("Heading2", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=17, color=_REF_RGB_NAVY, bold=True)
    s.paragraph_format.space_before = Pt(14)
    s.paragraph_format.space_after = Pt(4)
    s.paragraph_format.keep_with_next = True
    add_pPr_border(s, "bottom", 12, "1A3A5C", space="1")

    # ---- Heading3 (markdown `###` — subsection with blue left bar).
    s = get_or_add_style("Heading3", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=12, color=_REF_RGB_NAVY, bold=True)
    s.paragraph_format.space_before = Pt(10)
    s.paragraph_format.space_after = Pt(3)
    s.paragraph_format.left_indent = Cm(0.25)
    s.paragraph_format.keep_with_next = True
    add_pPr_border(s, "left", 24, "3A72C7", space="6")

    # ---- VerbatimChar (inline code — pink fg on soft pink bg).
    s = get_or_add_style("VerbatimChar", WD_STYLE_TYPE.CHARACTER)
    set_font(s, name="Courier New", size=9, color=_REF_RGB_PINK_FG)
    set_rPr_shading(s, "FDE8E8")

    # ---- SourceCode (block code — light grey bg, blue left bar).
    s = get_or_add_style("SourceCode", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Courier New", size=8.5, color=_REF_RGB_BODY)
    s.paragraph_format.space_before = Pt(4)
    s.paragraph_format.space_after = Pt(8)
    s.paragraph_format.left_indent = Cm(0.3)
    set_pPr_shading(s, "F4F6FA")
    add_pPr_border(s, "left", 24, "3A72C7", space="6")

    # ---- ImageCaption (centered italic grey).
    s = get_or_add_style("ImageCaption", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=9, color=_REF_RGB_GREY, italic=True)
    s.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s.paragraph_format.space_before = Pt(2)
    s.paragraph_format.space_after = Pt(8)

    # ---- CaptionedFigure (the image's containing paragraph — center it).
    s = get_or_add_style("CaptionedFigure", WD_STYLE_TYPE.PARAGRAPH)
    s.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    s.paragraph_format.space_before = Pt(6)
    s.paragraph_format.space_after = Pt(2)

    # ---- FirstParagraph (paragraph after a heading).
    s = get_or_add_style("FirstParagraph", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=10, color=_REF_RGB_BODY)
    s.paragraph_format.space_after = Pt(4)

    # ---- BodyText.
    s = get_or_add_style("BodyText", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=10, color=_REF_RGB_BODY)

    # ---- Compact (pandoc tight-list paragraphs).
    s = get_or_add_style("Compact", WD_STYLE_TYPE.PARAGRAPH)
    set_font(s, name="Helvetica", size=10, color=_REF_RGB_BODY)
    s.paragraph_format.space_after = Pt(2)

    # ---- TableGrid (default for tables) — light blue header.
    try:
        tg = doc.styles["Table Grid"]
        tg.font.name = "Helvetica"
        tg.font.size = Pt(9.5)
        tg.font.color.rgb = RGBColor(*_REF_RGB_BODY)
    except KeyError:
        pass

    doc.save(str(out_path))


# Pandoc inserts a centered Figure caption automatically from the image's alt
# text via implicit_figures. Our markdown also has a `*Figure N — ...*` italic
# line right under the image (the PDF reads it via its own caption regex). For
# the DOCX, that italic line ends up as a *second* caption that pushes the
# layout around. Strip it before pandoc sees it.
_DOCX_CAPTION_LINE_RE = re.compile(
    r'^\s*\*Figure\s+\d+\s+[—-][^\n]*\*\s*$', re.MULTILINE
)


def _strip_caption_lines_for_docx(md_text: str) -> str:
    return _DOCX_CAPTION_LINE_RE.sub('', md_text)


def md_to_docx(md_path: Path, out_docx: Path) -> None:
    import pypandoc

    # Build the reference.docx style template next to the output file.
    ref_docx = out_docx.with_name("_reference.docx")
    _build_reference_docx(ref_docx)

    # Build a docx-tuned copy of the markdown (no duplicate Figure captions).
    docx_md = md_path.with_suffix(".docx.md")
    docx_md.write_text(
        _strip_caption_lines_for_docx(md_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    extra = [
        "--standalone",
        "--toc",
        "--toc-depth=3",
        "--from=gfm+yaml_metadata_block+implicit_figures",
        "--resource-path=" + str(md_path.parent),
        "--reference-doc=" + str(ref_docx),
        # Disable Pandoc's syntax highlighter — its KeywordTok / StringTok
        # token styles override SourceCode's colour, so all code lines
        # would otherwise render in a rainbow palette. We want the whole
        # block in one body colour, matching the PDF.
        "--no-highlight",
    ]
    pypandoc.convert_file(
        str(docx_md), to="docx",
        outputfile=str(out_docx),
        format="gfm",
        extra_args=extra,
    )

    # Tidy up: remove the temp md / reference once DOCX is built.
    docx_md.unlink(missing_ok=True)
    ref_docx.unlink(missing_ok=True)


# ---------- Validation report -------------------------------------------

def validate(diagrams: list[Diagram]) -> None:
    print("\n=== A4 fit validation ===")
    for d in diagrams:
        with Image.open(d.png_path) as im:
            w, h = im.size
        w_mm = w / PX_PER_MM
        h_mm = h / PX_PER_MM
        ok_w = "OK " if w <= MAX_DIAGRAM_W_PX else "WIDE"
        ok_h = "OK " if h <= MAX_DIAGRAM_H_PX else "TALL"
        print(f"  [{ok_w}/{ok_h}] diagram {d.index:02d}  {w}x{h}px  ({w_mm:.0f}x{h_mm:.0f}mm)  {d.png_path.name}")
    print()


# ---------- Main --------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown -> PDF + DOCX with rendered Mermaid (A4-safe).")
    ap.add_argument("input", type=Path, help="Path to the source .md file.")
    ap.add_argument("--out", type=Path, default=None, help="Output directory (default: <input>_build).")
    ap.add_argument("--pdf-only", action="store_true")
    ap.add_argument("--docx-only", action="store_true")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 2

    out_dir = args.out or args.input.with_name(args.input.stem + "_build")
    out_dir.mkdir(parents=True, exist_ok=True)
    diagrams_dir = out_dir / "diagrams"

    print(f"-> reading   {args.input}")
    md_text = args.input.read_text(encoding="utf-8")

    print(f"-> rendering mermaid -> PNG into {diagrams_dir}")
    processed_md, diagrams = preprocess(md_text, diagrams_dir)

    processed_md_path = out_dir / (args.input.stem + ".processed.md")
    processed_md_path.write_text(processed_md, encoding="utf-8")
    print(f"-> wrote     {processed_md_path}")

    validate(diagrams)

    title = args.input.stem.replace("_", " ").title()

    pdf_path = out_dir / (args.input.stem + ".pdf")
    docx_path = out_dir / (args.input.stem + ".docx")

    if not args.docx_only:
        print("-> building PDF (xhtml2pdf, A4) ...")
        html = md_to_html(processed_md, base_dir=out_dir, title=title)
        (out_dir / (args.input.stem + ".preview.html")).write_text(html, encoding="utf-8")
        html_to_pdf(html, out_dir, pdf_path)
        print(f"   OK {pdf_path}")

    if not args.pdf_only:
        print("-> building DOCX (pandoc) ...")
        md_to_docx(processed_md_path, docx_path)
        print(f"   OK {docx_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
