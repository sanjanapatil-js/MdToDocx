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
# ]
# ///
"""
md_to_pdf_docx — one-shot converter

Reads a single Markdown file (the kind produced by `system_prompt.md`),
renders every ```mermaid``` block to a PNG via mermaid.ink (with a local
mmdc fallback), guarantees every diagram fits the A4 content area, then
emits BOTH a PDF and a DOCX in one run.

Usage:
    uv run convert.py documentation.md
    uv run convert.py documentation.md --out build
    uv run convert.py documentation.md --pdf-only
    uv run convert.py documentation.md --docx-only
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

# Force UTF-8 stdout/stderr on Windows so arrows and box glyphs don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from PIL import Image

# ---------- A4 geometry --------------------------------------------------
# A4 = 210 x 297 mm. Margins 20 mm each side -> content = 170 x 257 mm.
# At 96 dpi: 1 mm = 3.7795 px. So content ~= 642 x 971 px.
A4_CONTENT_WIDTH_MM = 170
A4_CONTENT_HEIGHT_MM = 257
DPI = 150  # render diagrams at 150 dpi for crisp PDF
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
    """Encode mermaid source the way mermaid.ink expects (pako/zlib + base64url)."""
    payload = (
        '{"code":' + _json_str(text) + ',"mermaid":{"theme":"default"}}'
    ).encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15)
    compressed = compressor.compress(payload) + compressor.flush()
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def _json_str(s: str) -> str:
    """Inline JSON-string escape (avoid importing json just for this)."""
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
    if not shutil.which("mmdc"):
        return False
    src = out_png.with_suffix(".mmd")
    src.write_text(code, encoding="utf-8")
    try:
        subprocess.run(
            ["mmdc", "-i", str(src), "-o", str(out_png), "-b", "white",
             "-w", str(MAX_DIAGRAM_W_PX), "-s", "2"],
            check=True, capture_output=True,
        )
        return out_png.exists()
    except subprocess.CalledProcessError as e:
        print(f"  mmdc failed: {e.stderr.decode(errors='ignore')[:200]}")
        return False
    finally:
        src.unlink(missing_ok=True)


def render_mermaid(code: str, out_png: Path) -> bool:
    """Render with mermaid.ink first; fall back to local mmdc if installed."""
    if _render_via_mermaid_ink(code, out_png):
        return True
    print("  → falling back to local mmdc (Node.js mermaid-cli)…")
    return _render_via_mmdc(code, out_png)


# ---------- A4 fit validation -------------------------------------------

def fit_to_a4(png: Path) -> tuple[int, int]:
    """Resize PNG so it never exceeds the A4 content area. Returns final WxH px."""
    with Image.open(png) as im:
        im = im.convert("RGBA")
        w, h = im.size
        scale = min(MAX_DIAGRAM_W_PX / w, MAX_DIAGRAM_H_PX / h, 1.0)
        if scale < 1.0:
            new = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.resize(new, Image.LANCZOS)
            w, h = new
        # Pad a 1px transparent border so PDF engines don't crop edges.
        im.save(png, "PNG", optimize=True)
        return w, h


# ---------- Markdown pre-processing -------------------------------------

def extract_caption_after(md: str, span_end: int) -> str:
    """Look one line below the closing fence for a `*Figure N — ...*` caption."""
    tail = md[span_end:span_end + 400]
    m = re.match(r"\s*\*Figure[^\n]*\*", tail)
    return m.group(0).strip("*").strip() if m else ""


def preprocess(md_text: str, diagrams_dir: Path) -> tuple[str, list[Diagram]]:
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    diagrams: list[Diagram] = []

    def replace(match: re.Match) -> str:
        code = match.group(1).strip()
        idx = len(diagrams) + 1
        # Stable filename keyed on content so re-runs are cache-friendly.
        digest = hashlib.sha1(code.encode("utf-8")).hexdigest()[:10]
        png = diagrams_dir / f"diagram_{idx:02d}_{digest}.png"

        caption = extract_caption_after(md_text, match.end())
        print(f"[diagram {idx}] {caption or '(no caption)'}")

        if not png.exists():
            ok = render_mermaid(code, png)
            if not ok:
                print(f"  ⚠ failed to render diagram {idx}; embedding raw code block instead.")
                return match.group(0)  # leave the fenced mermaid block as-is
        w, h = fit_to_a4(png)
        print(f"  rendered {png.name} ({w}x{h}px)")

        diagrams.append(Diagram(idx, code, caption, png))
        # Use forward-slash relative path so pandoc + weasyprint both find it.
        rel = png.relative_to(diagrams_dir.parent).as_posix()
        return f"![{caption or f'Diagram {idx}'}]({rel})"

    new_md = MERMAID_FENCE.sub(replace, md_text)
    return new_md, diagrams


# ---------- HTML / PDF --------------------------------------------------

CSS_A4 = """
@page {
  size: a4 portrait;
  margin: 20mm 18mm 22mm 18mm;
  @frame footer { -pdf-frame-content: footerContent; bottom: 8mm; left: 18mm; right: 18mm; height: 8mm; }
}
html { font-size: 10pt; }
body { font-family: Helvetica, Arial, sans-serif; color: #1a1a1a; line-height: 1.45; }
h1 { font-size: 22pt; border-bottom: 2px solid #222; padding-bottom: 4pt; -pdf-keep-with-next: true; }
h2 { font-size: 16pt; margin-top: 18pt; color: #1a3052; border-bottom: 1px solid #ccc; padding-bottom: 2pt; -pdf-keep-with-next: true; }
h3 { font-size: 13pt; margin-top: 14pt; color: #244062; -pdf-keep-with-next: true; }
h4 { font-size: 11.5pt; margin-top: 12pt; color: #355881; }
p, li { orphans: 3; widows: 3; }
img { max-width: 170mm; margin: 8pt auto; }
.figure { text-align: center; margin: 8pt 0; -pdf-keep-in-frame-mode: shrink; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 8.5pt; table-layout: fixed; word-wrap: break-word; -pdf-word-wrap: CJK; }
table, th, td { border: 0.5pt solid #999; }
th, td { padding: 3pt 5pt; vertical-align: top; text-align: left; word-wrap: break-word; overflow-wrap: anywhere; -pdf-word-wrap: CJK; }
th { background-color: #eef1f5; font-weight: bold; }
td code, th code { font-size: 7.5pt; word-wrap: break-word; overflow-wrap: anywhere; -pdf-word-wrap: CJK; white-space: normal; }
code { font-family: Courier, monospace; background-color: #f3f3f3; padding: 1pt 3pt; font-size: 9pt; }
pre { background-color: #f7f7f9; border: 0.5pt solid #ddd; padding: 6pt 8pt; font-size: 8.5pt; font-family: Courier, monospace; -pdf-keep-in-frame-mode: shrink; white-space: pre-wrap; word-wrap: break-word; }
pre code { background-color: transparent; padding: 0; }
blockquote { border-left: 3pt solid #b6c4d2; margin: 6pt 0; padding: 4pt 8pt; color: #444; background-color: #f6f9fc; }
em { color: #555; font-style: italic; }
hr { border: 0; border-top: 0.5pt solid #ccc; margin: 12pt 0; }
.toc a { text-decoration: none; color: #0b5fff; }
"""

FOOTER_HTML = """
<div id="footerContent" style="font-size:8pt;color:#666;text-align:right;">
  Page <pdf:pagenumber/> of <pdf:pagecount/>
</div>"""

HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>{footer}{body}</body></html>"""


_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_INLINE_CODE_IN_CELL_RE = re.compile(
    r"(<(?:td|th)[^>]*>)([\s\S]*?)(</(?:td|th)>)", re.IGNORECASE
)
_CODE_TAG_RE = re.compile(r"<code>([^<]+)</code>", re.IGNORECASE)
_BREAK_AFTER = set("/._-:?&=,")


def _segment_long_token(token: str, every: int = 14) -> list[str]:
    """Split a long token into wrap-friendly segments at URL-like separators."""
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
    """Render an inline-code body with safe break points for narrow A4 columns.

    We emit each segment inside its own <code> element separated by zero-width <wbr/>
    look-alikes (an empty <span> with no width). Using separate <code> elements
    lets xhtml2pdf treat segment boundaries as wrap points without putting any
    visible glyph in the output.
    """
    segments = _segment_long_token(text)
    if len(segments) == 1:
        return f"<code>{segments[0]}</code>"
    return "".join(f"<code>{seg}</code><span></span>" for seg in segments)


def _make_table_cells_wrappable(html: str) -> str:
    """Inside <td>/<th>, segment long <code> bodies so they wrap on narrow A4 columns."""
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


def md_to_html(md_text: str, base_dir: Path, title: str) -> str:
    import markdown
    html_body = markdown.markdown(
        md_text,
        extensions=[
            "extra", "tables", "fenced_code", "sane_lists", "toc",
            "pymdownx.superfences", "pymdownx.tilde", "pymdownx.tasklist",
            "pymdownx.magiclink",
        ],
        extension_configs={
            "pymdownx.tasklist": {"custom_checkbox": True},
            "toc": {"permalink": False},
        },
    )
    html_body = _make_table_cells_wrappable(html_body)
    return HTML_SHELL.format(title=title, css=CSS_A4, footer=FOOTER_HTML, body=html_body)


def html_to_pdf(html: str, base_dir: Path, out_pdf: Path) -> None:
    """Render HTML to A4 PDF using xhtml2pdf (pure Python, no system libs)."""
    from xhtml2pdf import pisa

    def link_callback(uri: str, rel: str) -> str:
        # Resolve relative image paths against base_dir.
        if uri.startswith(("http://", "https://", "data:")):
            return uri
        candidate = (base_dir / uri).resolve()
        return str(candidate)

    with open(out_pdf, "wb") as fh:
        result = pisa.CreatePDF(html, dest=fh, link_callback=link_callback, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"xhtml2pdf reported {result.err} error(s) while building {out_pdf.name}")


# ---------- DOCX --------------------------------------------------------

def md_to_docx(md_path: Path, out_docx: Path) -> None:
    """Use bundled pandoc (pypandoc-binary) for a clean DOCX with images/tables."""
    import pypandoc
    extra = [
        "--standalone",
        "--toc",
        "--toc-depth=3",
        "--from=gfm+yaml_metadata_block",
        "--resource-path=" + str(md_path.parent),
    ]
    pypandoc.convert_file(
        str(md_path), to="docx",
        outputfile=str(out_docx),
        format="gfm",
        extra_args=extra,
    )


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
    ap = argparse.ArgumentParser(description="Markdown → PDF + DOCX with rendered Mermaid (A4-safe).")
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

    print(f"→ reading   {args.input}")
    md_text = args.input.read_text(encoding="utf-8")

    print(f"→ rendering mermaid → PNG into {diagrams_dir}")
    processed_md, diagrams = preprocess(md_text, diagrams_dir)

    processed_md_path = out_dir / (args.input.stem + ".processed.md")
    processed_md_path.write_text(processed_md, encoding="utf-8")
    print(f"→ wrote     {processed_md_path}")

    validate(diagrams)

    title = args.input.stem.replace("_", " ").title()

    pdf_path = out_dir / (args.input.stem + ".pdf")
    docx_path = out_dir / (args.input.stem + ".docx")

    if not args.docx_only:
        print("→ building PDF (xhtml2pdf, A4) …")
        html = md_to_html(processed_md, base_dir=out_dir, title=title)
        (out_dir / (args.input.stem + ".preview.html")).write_text(html, encoding="utf-8")
        html_to_pdf(html, out_dir, pdf_path)
        print(f"   ✓ {pdf_path}")

    if not args.pdf_only:
        print("→ building DOCX (pandoc) …")
        md_to_docx(processed_md_path, docx_path)
        print(f"   ✓ {docx_path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
