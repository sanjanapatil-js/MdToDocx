# MD → PDF + DOCX (with Mermaid, A4-safe)

A small toolkit for the workflow:

> "Manager drops a doc → I need a clean **module-wise technical documentation**
> back as one PDF and one DOCX, with Mermaid diagrams that actually render and
> never overflow the A4 page."

It has two halves:

1. **`system_prompt.md`** — paste this as the **system prompt** on any AI
   platform (Claude, ChatGPT, Gemini, Groq, Llama, …). Then attach the source
   PDF / SRS / notes. The AI returns **one big `documentation.md`** file
   (title, table of contents, all sections, Mermaid diagrams inline,
   `[Extended]` tags on anything not in the source).
2. **`convert.py`** — feed that single Markdown file to the script. It
   renders every Mermaid block to PNG, validates each PNG against A4
   geometry, and emits **both** a PDF and a DOCX in **one run**.

---

## What this is

A reproducible pipeline for high-quality technical documentation that always
includes diagrams. The PDF you saw (`Attendance_Module_Documentation.pdf`)
is the *target shape* — title page, numbered sections, role matrices, ER
diagrams, sequence flows, state machine, sample code, scenarios, summary
checklist. This toolkit gets you the same shape for **any** module from any
source material.

## Why we made this

- Manual MD → PDF/DOCX kept dropping Mermaid diagrams or rendering them as
  microscopic blurs. This pipeline **pre-renders Mermaid to PNG** (via
  `mermaid.ink`, with a local `mmdc` fallback) and **resizes each PNG so it
  fits the 170 mm × 257 mm A4 content area at 150 dpi**.
- Pandoc-only DOCX is great but not always available; weasyprint-only PDF
  doesn't handle Mermaid. We use the right tool for each output and share a
  single pre-processed Markdown source between them.
- Both outputs come from **the same Markdown** so PDF and DOCX never drift.

---

## Install

You need **Python 3.10+**. Pick one of the install paths below.

### Option A — `uv` (recommended, fastest)

[`uv`](https://github.com/astral-sh/uv) is a modern Python launcher.

```bash
# install uv once (Windows PowerShell):
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# or on macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The script declares its own dependencies inline (PEP 723), so you can just:

```bash
uv run convert.py path\to\documentation.md
```

`uv` will create an isolated venv on the first run, install everything from
the script header, and execute. Subsequent runs are cached and instant.

### Option B — plain `pip` + venv

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
python convert.py path\to\documentation.md
```

### Option C — `uv pip` (uses `pyproject.toml`)

```bash
uv venv
uv pip install -e .
uv run python convert.py path\to\documentation.md
```

### Optional system dependencies

| Component        | Needed for                          | How to install                                                       |
|------------------|--------------------------------------|----------------------------------------------------------------------|
| `mermaid.ink`    | default Mermaid renderer (HTTP)     | nothing — public service, just needs internet                        |
| `mmdc` (Node.js) | offline Mermaid fallback             | `npm i -g @mermaid-js/mermaid-cli`                                   |

There are **no system libraries** to install — PDF is built with `xhtml2pdf`
(pure Python) and DOCX with the bundled `pypandoc-binary`. Works on Windows,
macOS and Linux out of the box.

---

## How to run

### 1. Generate the Markdown (using `system_prompt.md`)

1. Open your AI platform of choice.
2. Paste the **entire contents of `system_prompt.md`** as the system /
   first message.
3. Attach (or paste) the source SRS / BRD / notes for the module you want
   documented.
4. The AI returns a single Markdown file. Save it as e.g.
   `Attendance_Module_Documentation.md` next to `convert.py`.

### 2. Convert to PDF + DOCX

```bash
uv run convert.py Attendance_Module_Documentation.md
```

Output (default):

```
Attendance_Module_Documentation_build/
├── diagrams/
│   ├── diagram_01_<hash>.png
│   ├── diagram_02_<hash>.png
│   └── …
├── Attendance_Module_Documentation.processed.md   # MD with PNG refs
├── Attendance_Module_Documentation.preview.html   # styled HTML preview
├── Attendance_Module_Documentation.pdf            ← final PDF (A4)
└── Attendance_Module_Documentation.docx           ← final DOCX
```

### Useful flags

```bash
uv run convert.py doc.md --out build              # custom output dir
uv run convert.py doc.md --pdf-only               # skip DOCX
uv run convert.py doc.md --docx-only              # skip PDF
```

### What the script validates

For every diagram it prints a line like:

```
[OK /OK ] diagram 03  1004x612px  (170x104mm)  diagram_03_<hash>.png
[WIDE/OK ] diagram 07  1280x540px  (217x91mm)   diagram_07_<hash>.png   <- gets auto-resized
```

`WIDE` / `TALL` rows are auto-resampled with Lanczos so they fit the A4
content area before being embedded.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `mermaid.ink HTTP 5xx` repeatedly | Install Node + `npm i -g @mermaid-js/mermaid-cli`; the script auto-falls back. |
| `xhtml2pdf` warns about a CSS property | Safe to ignore — xhtml2pdf has limited CSS support; the document still renders. |
| DOCX missing TOC | Open it in Word once and press F9 on the TOC field, or right-click → Update field. |
| Diagram PNG looks blurry in DOCX | Mermaid source has too many nodes; split into 2 diagrams (per `system_prompt.md` §4). |
| Tables overflow horizontally | Same — table has too many columns; the converter does not auto-rotate; trim columns or move detail into prose. |

---

## File layout

```
.
├── system_prompt.md      # paste into AI to generate the .md
├── convert.py            # MD -> PDF + DOCX (uv-runnable, PEP 723)
├── requirements.txt      # for plain pip installs
├── pyproject.toml        # for `uv pip install -e .`
└── README.md             # this file
```

## Commands cheat-sheet

```bash
# zero-setup, with uv:
uv run convert.py documentation.md

# with pip + venv:
pip install -r requirements.txt
python convert.py documentation.md

# only one of the two outputs:
uv run convert.py documentation.md --pdf-only
uv run convert.py documentation.md --docx-only

# pick an output dir:
uv run convert.py documentation.md --out ./dist
```
