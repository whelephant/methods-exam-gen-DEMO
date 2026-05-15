"""Vision-based question extraction.

Per-page workflow:
  1. Render page → PNG at PAGE_DPI
  2. Send to Claude with a structured tool schema; prompt instructs verbatim LaTeX + page-noise filtering
  3. Quality-check the response (no CID artefacts, marks present where required, etc.)
  4. On failure, escalate to the next tier (Haiku → Sonnet → Opus)
  5. Persist per-page result to pipeline/cache/<year>_p<paper>_page<NNN>.json (idempotent re-runs)

Paper-level workflow:
  - Iterate every page (or every page not already cached)
  - Stitch fragments that share (question_number, part) across continuation pages
  - Insert into `questions` table with deterministic IDs

CLI:
  python -m pipeline.extract_questions --year 2023 --paper 1 --budget 1.00
  python -m pipeline.extract_questions --year 2023 --paper 1 --pages 3      # one page only
  python -m pipeline.extract_questions --year 2023 --paper 1 --force         # ignore cache
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import fitz  # pymupdf
from anthropic import Anthropic
from dotenv import load_dotenv

from pipeline.db import REPO_ROOT, connect
from pipeline.spend import (
    BudgetExceeded,
    MODEL_PRICING,
    check_budget,
    estimate_cost,
    log_call,
)

load_dotenv(REPO_ROOT / ".env")

CACHE_DIR = REPO_ROOT / "pipeline" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PAGE_DPI = 200

# Anthropic model identifiers (full strings the API expects).
ANTHROPIC_MODEL_IDS: dict[str, str] = {
    "claude-haiku-4-5":  "claude-haiku-4-5",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-7":   "claude-opus-4-7",
}

# Sonnet 4.6 is the default first tier. Haiku 4.5 is reliable on page-type classification
# but consistently miss-reads vertically-stacked fractions (e.g. integral bounds like π/3),
# silently producing prompts that pass JSON quality checks but are mathematically wrong.
# Math is core to this corpus, so we pay for Sonnet on extraction. Haiku is opt-in via --cheap.
ESCALATION_TIERS = ("claude-sonnet-4-6",)  # Opus is opt-in via --include-opus


# ───── system prompt + tool schema ───────────────────────────────────

SYSTEM_PROMPT = r"""You are extracting exam questions from a single page of a VCE Mathematical Methods (Units 3 & 4) exam paper. Your output is inserted verbatim into a question bank.

# 1. Classify the page

Identify `page_type` first:
- **"questions"** — contains one or more exam questions or sub-questions
- **"formula_sheet"** — the VCAA Mathematical Methods formula reference sheet at the back of the paper (headings include "Formula sheet", "Mathematical Methods formulas", "END OF FORMULA SHEET"). Always at the end.
- **"instructions"** — cover page, structure-of-the-book, "Instructions" page
- **"back_matter"** — "END OF QUESTION AND ANSWER BOOK" and surrounding empty pages
- **"blank"** — "THIS PAGE IS BLANK" or visually empty

For everything except `"questions"`, return an empty `fragments` array.

# 2. Page noise to ignore

These appear on every page and are NOT question content:
- Page numbers (e.g. "5", "12")
- Running header `20XX MATHMETH EXAM 1` or `20XX MATHMETH EXAM 2`
- Marginal text: "TURN OVER", "do not write in this area" (often as spaced letters), "Section A – continued"
- Continuation notices like "Question 7 – continued" — these tell you the page continues a question from a previous page; do not transcribe them

# 3. Fragment kinds

Each piece of question content on the page is one "fragment":
- **"stem"** — the ORIGINAL preamble of a multi-part question, appearing IMMEDIATELY after the `Question N (X marks)` heading, on the SAME page where part `a` begins. Each question_number has **AT MOST ONE** stem. `marks` is null/omitted. `part` is null/omitted.
- **"sub_stem"** — a later preamble inside the same question that introduces a new function, definition, or context before the next sub-parts. Multiple sub_stems per question are normal (Section B questions often have 2–4). `marks` is null/omitted. `part` is null/omitted. `before_part` is REQUIRED (the label of the first sub-part this preamble precedes, e.g. `"c"`, `"d.i"`).
- **"part"** — a sub-question labelled `a.`, `b.`, `b.i.`, etc. `marks` is an integer (the marks for THIS sub-part). `part` is the label without the trailing dot (e.g. "a", "b.i").
- **"whole"** — a single question with NO sub-parts at all. `marks` is an integer. `part` MUST be omitted.
- **"mc"** — a multiple-choice question (Paper 2 Section A only). `marks` is 1. Set `is_mc` true. The options live in `mc_options`. **2016–2023 papers have 5 options (A–E); 2024+ papers have 4 options (A–D).** Emit the actual count visible on the page. Each option is `{"kind": "text"|"diagram", ...}` — see §6a for the diagram case.

**Critical invariant:** if a question has any sub-parts (a, b, c, b.i, etc.), then EVERY sub-part is `fragment_kind="part"`. `fragment_kind="whole"` is reserved for questions that have no sub-parts at all. Do not mix "whole" and "part" inside the same question_number.

## 3a. Distinguishing fragment kinds

**One rule, applied to every block of prose on the page:**

> Does this block ask the reader to do or answer something? If YES → it is a `part` (or `whole`). If NO → it is a preamble: a `stem` if it's the FIRST preamble of the question (right under `Question N (X marks)`), otherwise a `sub_stem`.

For every `sub_stem`, `before_part` is the label of the very next sub-part that follows the preamble in the document.

**Corollaries** (all follow from the rule above — do not need separate special-case handling, but stated explicitly to anchor edge cases):

- *One diagram per preamble.* Two `shown below` / `shown above` phrases inside one preamble means you missed a split: that's two preambles, the first is `stem` (or `sub_stem`) with diagram 1, the second is a `sub_stem` with `before_part` set to the next sub-part label, with diagram 2. Each has `has_diagram=true`.
- *Lettered label without its own question.* If you see `c.` followed only by `i.` and `ii.` (with no answer-asking text between `c.` and `i.`), then `c.` is a `sub_stem` with `before_part="c.i"`, not a 4-mark part. The marks beside `c.` (if any) are the SUM across `c.i + c.ii`, like the question-header total — they are NOT part marks. Real "part" fragments always have answer-asking text.
- *Continuation pages.* Prose under a `Question N – continued` banner is a `sub_stem` with `before_part = <next sub-part label on this page>`. Never emit it as `stem` — `stem` is only the first preamble of the question.

**Minimal worked example (covers all three cases):**

Section B Question 1 (14 marks), page 10:

    Question 1 (14 marks)
    A rectangular sheet of cardboard ... is cut from each of the corners, as shown in the diagram below.
    [diagram 1: flat sheet]
    The sides ... are folded up ..., as shown in the diagram below.
    [diagram 2: folded box]
    A box is to be made ... with h = 25 cm.
    a. Show that ...                             1 mark
    b. State the domain ...                     1 mark
    c. For h > 0, let P' be ...
       i.  Define the function ...              2 marks
       ii. Determine the maximum ...            2 marks

Correct emission for this question:

| fragment_kind | part     | before_part | marks | has_diagram | prompt summary                |
| ------------- | -------- | ----------- | ----- | ----------- | ----------------------------- |
| stem          | —        | —           | —     | true        | "rectangular sheet ... below" |
| sub_stem      | —        | a           | —     | true        | "folded up ... h = 25 cm"     |
| part          | a        | —           | 1     | false       | "Show that ..."               |
| part          | b        | —           | 1     | false       | "State the domain ..."        |
| sub_stem      | —        | c.i         | —     | false       | "For h > 0, let P' be ..."    |
| part          | c.i      | —           | 2     | false       | "Define the function ..."     |
| part          | c.ii     | —           | 2     | false       | "Determine the maximum ..."   |

Note how the `c` label generates NO row — only `c.i` and `c.ii` are parts; the prose under `c.` becomes a `sub_stem` because it doesn't ask anything.

**Marks at the question header vs. per-part marks:** "Question 1 (14 marks)" gives the SUM of marks across all sub-parts. Each sub-part has its own marks shown at the end of that part (e.g. "1 mark", "2 marks"). For "part" fragments, `marks` is the per-part value, NOT the question total.

If a fragment spans multiple pages, you may see only part of it on the current page. Set `is_complete_on_this_page` accordingly. If the fragment started on a previous page (the page header says "Question N – continued"), set `is_continuation_from_prior_page` true and `prompt_md` to ONLY the text on this page. We stitch across pages later.

# 4. Math formatting

All mathematics goes in LaTeX, delimited:
- Inline: `$f(x) = e^{2x}$`
- Display: `$$\int_0^1 f(x)\,dx$$`

Conventions:
- $e^x$, $\sin(x)$, $\cos(x)$, $\tan(x)$, $\log_e(x)$, $\log_{10}(x)$
- Fractions $\frac{a}{b}$
- Sets $\mathbb{R}$, $\mathbb{Q}$, $\mathbb{Z}$, $\mathbb{N}$
- Greek $\pi$, $\theta$, $\mu$, $\sigma$, $\hat{p}$
- Domain notation $f: [0, 2\pi] \to \mathbb{R}$
- ASCII hyphen `-` for the minus sign (not Unicode U+2212)

**Never include `(cid:NN)` artefacts** in prompt_md. If you see one in the underlying text, look at the visual glyph on the page and write the correct symbol.

## 4a. Reading stacked / vertical fractions (critical — common failure mode)

VCAA source PDFs render fractions as **vertically stacked** characters: numerator above a horizontal bar, denominator below. You MUST read both the numerator and the denominator. Common cases:

- **Integral bounds.** `$\int_0^{\pi/3}$` in the source PDF looks like `∫` with `0` at bottom-right and a small `π` stacked above `3` at top-right. Do not just read the `π`. The bound is `\pi/3`, not `\pi`. The same applies to `\pi/2`, `\pi/4`, `\pi/6`, `2\pi/3`, `3\pi/4`, etc.
- **Standalone fractions in prose.** `\frac{dy}{dx}`, `\frac{x^2-x}{e^x}`, `\frac{1}{2}`, `\frac{\pi}{3}`.
- **Coefficients written vertically.** Watch for any two numbers/symbols separated by a thin horizontal line.

A common failure is to capture only the top of a stacked fraction and silently drop the denominator. Before finalising any prompt_md containing an integral, fraction, or limit, ask: "did I include both the top AND the bottom of every stacked element on the page?"

**Example.** For a page showing `Evaluate ∫₀^{π/3} sin(x) dx.` (1 mark) — with π and 3 visually stacked at the upper bound and 0 at the lower bound:

CORRECT: `Evaluate $\int_0^{\pi/3} \sin(x)\,dx$.`
WRONG:   `Evaluate $\int_0^{\pi} \sin(x)\,dx$.`   ← missed the `/3`
WRONG:   `Evaluate $\int_0^{3} \sin(x)\,dx$.`     ← missed the `\pi`

# 5. Verbatim

Transcribe the question text EXACTLY as it appears. Do not paraphrase. Do not fix typos. Do not abbreviate. Include all stated domains, restrictions, "where $x \in \mathbb{R}$" clauses, etc.

# 6. Diagrams

Set `has_diagram` true ONLY for non-text visual elements:
- Coordinate-plane function graphs
- Geometric figures (triangles, circles, polygons)
- Tree diagrams, Venn diagrams, statistical plots

Tables of numbers/text are **not** diagrams — transcribe them as markdown tables (`| x | 0 | 1 |` etc.).

If a fragment has a diagram but the diagram contains text labels we can reproduce in LaTeX (axes labels, function names), still mark `has_diagram` true — we will extract the diagram as a vector image separately.

## 6a. MC options: text vs. diagram

Each entry of `mc_options` is `{"kind": "text"|"diagram", ...}`:

- **`"kind": "text"`** — the option is text or math (the common case). Field `"md"` holds the option content as LaTeX-aware markdown. Examples:
  - `{"kind": "text", "md": "$A = -\\tfrac{1}{2},\\ P = \\tfrac{\\pi}{3}$"}` (Q1-style algebra)
  - `{"kind": "text", "md": "$k \\in \\mathbb{R} \\setminus \\{-5, 4\\}$"}` (Q4-style set notation)
  - `{"kind": "text", "md": "0.81773"}` (Q13-style numeric)
- **`"kind": "diagram"`** — the option is a diagram (graph, geometric figure). Field `"bbox"` is the option's bounding box in **normalised page coordinates** `[x0, y0, x1, y1]` with values in `[0, 1]`, where `(0,0)` is top-left of the page. Tight enough to capture the diagram including any tick marks and axis labels but excluding the `A.` / `B.` etc. option labels. Example:
  - `{"kind": "diagram", "bbox": [0.12, 0.45, 0.48, 0.55]}`

**Rule of thumb:** if reading the option requires looking at a picture (axes, curves, polygons), it's a diagram. If it's an equation, number, or interval that can be transcribed in LaTeX, it's text. ALL options in a given MC must be either ALL text or ALL diagram — VCAA never mixes. The number of options is 5 (A–E) for 2016–2023 papers and 4 (A–D) for 2024+ papers.

## 6b. Pseudocode

Some MC questions (added under the 2023+ algorithms content) include a pseudocode block — keywords like `Define`, `For`, `If`, `Then`, `Else`, `Return`, `EndFor`. Reproduce these as a **fenced code block** in `prompt_md`:

```
Define newton(f(x), df(x), x0)
    For i from 1 to 3
        If df(x0) = 0 Then
            Return "Error: Division by zero"
        Else
            x0 ← x0 - f(x0) ÷ df(x0)
    EndFor
    Return x0
```

Preserve the exact indentation and the `←` assignment arrow. Pseudocode goes in `prompt_md`, NOT in the options.

# 7. Worked example

For a Paper 1 page showing `Question 1 (4 marks)` with sub-parts `a.` (2 marks, asking to differentiate $y = \frac{x^2 - x}{e^x}$) and `b.` (2 marks, asking to find $f'(\frac{\pi}{4})$ for $f(x) = \sin(x)e^{2x}$), the correct output is:

```json
{
  "page_type": "questions",
  "page_summary": "Question 1 (4 marks): differentiation via quotient and product rules.",
  "fragments": [
    {
      "fragment_kind": "part",
      "question_number": 1,
      "part": "a",
      "marks": 2,
      "prompt_md": "Let $y = \\dfrac{x^2 - x}{e^x}$.\n\nFind and simplify $\\dfrac{dy}{dx}$.",
      "is_continuation_from_prior_page": false,
      "is_complete_on_this_page": true,
      "is_mc": false,
      "has_diagram": false
    },
    {
      "fragment_kind": "part",
      "question_number": 1,
      "part": "b",
      "marks": 2,
      "prompt_md": "Let $f(x) = \\sin(x)e^{2x}$.\n\nFind $f'\\!\\left(\\dfrac{\\pi}{4}\\right)$.",
      "is_continuation_from_prior_page": false,
      "is_complete_on_this_page": true,
      "is_mc": false,
      "has_diagram": false
    }
  ]
}
```

Notice: **both** sub-parts use `fragment_kind: "part"` because Q1 has sub-parts. Do not use `"whole"` for any sub-part. Do not set `part` on a `"whole"` fragment.

# 8. Call the tool

Call `record_page_extraction` exactly once. Do not write any prose response."""


EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_page_extraction",
    "description": "Record the structured extraction of one exam-paper page.",
    "input_schema": {
        "type": "object",
        "required": ["page_type", "page_summary", "fragments"],
        "properties": {
            "page_type": {
                "type": "string",
                "enum": ["questions", "formula_sheet", "instructions", "back_matter", "blank"],
                "description": "What kind of page this is.",
            },
            "page_summary": {
                "type": "string",
                "description": "One sentence describing what's on this page, for the human audit view.",
            },
            "fragments": {
                "type": "array",
                "description": "All question content on this page. Empty for non-questions pages.",
                "items": {
                    "type": "object",
                    "required": [
                        "fragment_kind",
                        "question_number",
                        "prompt_md",
                        "is_continuation_from_prior_page",
                        "is_complete_on_this_page",
                        "is_mc",
                        "has_diagram",
                    ],
                    "properties": {
                        "fragment_kind": {
                            "type": "string",
                            "enum": ["stem", "sub_stem", "part", "whole", "mc"],
                        },
                        "section": {
                            "type": "string",
                            "enum": ["A", "B"],
                            "description": "Paper 2 section. Omit for Paper 1.",
                        },
                        "question_number": {"type": "integer"},
                        "part": {
                            "type": "string",
                            "description": "Sub-part label without trailing dot (e.g. 'a', 'b.i'). Omit for stems, sub_stems, and 'whole' fragments.",
                        },
                        "before_part": {
                            "type": "string",
                            "description": "REQUIRED for fragment_kind='sub_stem'. The part label this sub_stem immediately precedes, e.g. 'c' or 'd.i'.",
                        },
                        "marks": {
                            "type": "integer",
                            "description": "Marks for this part/whole. Omit for stems and continuations.",
                        },
                        "prompt_md": {
                            "type": "string",
                            "description": "Verbatim question text, LaTeX-in-$..$, markdown for any tables. ONLY the content on THIS page if the fragment continues.",
                        },
                        "mc_options_md": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "DEPRECATED — use mc_options instead. Kept for backwards-compat with text-only options.",
                        },
                        "mc_options": {
                            "type": "array",
                            "description": "MC options in order, starting from A. 2016–2023 papers have 5 options (A–E); 2024+ papers have 4 options (A–D). Emit the actual count visible on the page — do NOT pad with empties or fabricate. Each entry is text (kind=text + md) or diagram (kind=diagram + bbox).",
                            "minItems": 4,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "required": ["kind"],
                                "properties": {
                                    "kind": {"type": "string", "enum": ["text", "diagram"]},
                                    "md": {"type": "string", "description": "Option content as LaTeX-aware markdown. Required when kind=text."},
                                    "bbox": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 4,
                                        "maxItems": 4,
                                        "description": "Normalised page coords [x0,y0,x1,y1] in [0,1]. Required when kind=diagram.",
                                    },
                                },
                            },
                        },
                        "is_continuation_from_prior_page": {"type": "boolean"},
                        "is_complete_on_this_page": {"type": "boolean"},
                        "is_mc": {"type": "boolean"},
                        "has_diagram": {"type": "boolean"},
                    },
                },
            },
        },
    },
}


# ───── helpers ────────────────────────────────────────────────────────

def render_page_png(pdf_path: Path, page_num_1based: int, dpi: int = PAGE_DPI) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_num_1based - 1)
        return page.get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def _prompt_hash() -> str:
    """Stable hash of the prompt + schema + tier list. Used to invalidate cache when these change."""
    h = hashlib.sha256()
    h.update(SYSTEM_PROMPT.encode())
    h.update(json.dumps(EXTRACTION_TOOL, sort_keys=True).encode())
    h.update(",".join(ESCALATION_TIERS).encode())
    return h.hexdigest()[:12]


def _cache_path(year: int, paper: int, page: int) -> Path:
    return CACHE_DIR / f"{year}_p{paper}_page{page:03d}.json"


# ───── quality checks ─────────────────────────────────────────────────

CID_RE = __import__("re").compile(r"\(cid:\d+\)")


def quality_check(result: dict) -> tuple[bool, Optional[str]]:
    """Return (ok, reason_if_bad). Reasons drive review_queue entries and tier escalation."""
    if not isinstance(result, dict):
        return False, "result is not a dict"
    pt = result.get("page_type")
    if pt not in ("questions", "formula_sheet", "instructions", "back_matter", "blank"):
        return False, f"bad page_type: {pt!r}"

    fragments = result.get("fragments", [])
    if pt != "questions" and fragments:
        return False, f"non-questions page ({pt}) returned {len(fragments)} fragments"

    if not isinstance(fragments, list):
        return False, f"fragments is not a list (got {type(fragments).__name__})"

    for i, f in enumerate(fragments):
        if not isinstance(f, dict):
            return False, f"fragment[{i}]: not a dict (got {type(f).__name__}: {str(f)[:80]!r})"
        prompt = f.get("prompt_md", "")
        if CID_RE.search(prompt):
            return False, f"fragment[{i}]: CID artefact in prompt_md"

        qn = f.get("question_number")
        if qn is None or (isinstance(qn, int) and qn < 1):
            return False, f"fragment[{i}]: missing or invalid question_number ({qn!r})"

        kind = f.get("fragment_kind")
        complete = f.get("is_complete_on_this_page", True)
        cont = f.get("is_continuation_from_prior_page", False)
        part = f.get("part")

        # part/kind consistency
        if kind == "whole" and part:
            return False, f"fragment[{i}]: kind=whole but part='{part}' set (should be kind=part)"
        if kind == "part" and not part:
            return False, f"fragment[{i}]: kind=part but no part label"
        if kind == "stem" and part:
            return False, f"fragment[{i}]: kind=stem but part='{part}' set"
        if kind == "sub_stem" and part:
            return False, f"fragment[{i}]: kind=sub_stem but part='{part}' set (use before_part instead)"
        if kind == "sub_stem" and not f.get("before_part"):
            return False, f"fragment[{i}]: kind=sub_stem but missing before_part"

        # marks present when expected. Redacted questions (withdrawn post-exam by VCAA's
        # Independent Review process) carry no marks — the prompt is the redaction notice
        # itself, identifiable by the standard "has been redacted" phrasing.
        prompt_lower = (prompt or "").lower()
        is_redacted = ("has been redacted" in prompt_lower
                       or "this question has been redacted" in prompt_lower)
        if kind in ("part", "whole", "mc") and complete and not cont and not is_redacted:
            if "marks" not in f or f.get("marks") is None:
                return False, f"fragment[{i}]: missing marks on complete {kind}"

        # MC sanity
        if kind == "mc":
            if not f.get("is_mc"):
                return False, f"fragment[{i}]: fragment_kind=mc but is_mc false"
            opts = f.get("mc_options") or f.get("mc_options_md") or []
            if len(opts) not in (4, 5):
                return False, f"fragment[{i}]: MC needs 4 (2024+) or 5 (2016–2023) options, got {len(opts)}"
            if f.get("mc_options"):
                kinds_seen = set()
                for j, o in enumerate(opts):
                    if not isinstance(o, dict):
                        return False, f"fragment[{i}].mc_options[{j}]: not a dict"
                    ok = o.get("kind")
                    if ok not in ("text", "diagram"):
                        return False, f"fragment[{i}].mc_options[{j}]: bad kind {ok!r}"
                    kinds_seen.add(ok)
                    if ok == "text" and not o.get("md"):
                        return False, f"fragment[{i}].mc_options[{j}]: kind=text but no md"
                    if ok == "diagram":
                        bb = o.get("bbox")
                        if not (isinstance(bb, list) and len(bb) == 4 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bb)):
                            return False, f"fragment[{i}].mc_options[{j}]: kind=diagram needs bbox=[x0,y0,x1,y1] in [0,1]"
                if len(kinds_seen) != 1:
                    return False, f"fragment[{i}]: MC options mix text and diagram kinds ({kinds_seen}) — VCAA never mixes"

        # Stems / sub_stems shouldn't have marks
        if kind in ("stem", "sub_stem") and f.get("marks") is not None:
            return False, f"fragment[{i}]: {kind} has marks={f['marks']}"

        # Balanced $ delimiters (very rough — counts standalone $ pairs)
        # Exclude $$..$$ display-math pairs (each pair is two $$)
        cleaned = prompt.replace("$$", "")
        if cleaned.count("$") % 2 != 0:
            return False, f"fragment[{i}]: unbalanced $ delimiters"

    # Cross-fragment invariant: within one question_number, fragments are either
    # one "whole" alone, or any combination of "stem" + "sub_stem"s + "part"s
    # (no mixing whole with anything else). Multiple sub_stems are fine; at most one stem
    # is enforced within this page's fragments (across-page stems are caught at stitch time).
    by_qnum: dict[int, list[str]] = {}
    for f in fragments:
        if f.get("fragment_kind") in ("stem", "sub_stem", "part", "whole", "mc"):
            by_qnum.setdefault(f.get("question_number"), []).append(f.get("fragment_kind"))
    for qn, kinds in by_qnum.items():
        if "whole" in kinds and len(kinds) > 1:
            return False, f"question {qn}: mixed 'whole' with other fragment kinds ({kinds})"
        if kinds.count("stem") > 1:
            return False, f"question {qn}: more than one 'stem' on this page ({kinds.count('stem')}; should be at most 1)"

    return True, None


# ───── Anthropic call ─────────────────────────────────────────────────

_client_singleton: Optional[Anthropic] = None


def _client() -> Anthropic:
    global _client_singleton
    if _client_singleton is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set (check .env)")
        _client_singleton = Anthropic()
    return _client_singleton


@dataclass
class CallResult:
    ok: bool
    payload: dict
    model: str
    input_tokens: int
    cached_read_tokens: int
    cached_write_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    error: Optional[str] = None


def call_one_tier(model_short: str, png_bytes: bytes, year: int, paper: int, page: int) -> CallResult:
    """Single API call against one tier. Caller decides whether to escalate."""
    model_full = ANTHROPIC_MODEL_IDS[model_short]
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")

    t0 = time.time()
    try:
        resp = _client().messages.create(
            model=model_full,
            max_tokens=4096,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}
            ],
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_page_extraction"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"This is page {page} of the {year} Mathematical Methods Paper {paper}. Extract.",
                        },
                    ],
                }
            ],
        )
    except Exception as e:
        return CallResult(False, {}, model_short, 0, 0, 0, 0, 0.0, int((time.time() - t0) * 1000), error=str(e))

    latency_ms = int((time.time() - t0) * 1000)
    usage = resp.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cached_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cached_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = estimate_cost(
        model_short,
        input_tokens=input_tokens,
        cached_read_tokens=cached_read,
        cached_write_tokens=cached_write,
        output_tokens=output_tokens,
    )

    payload: dict = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_page_extraction":
            payload = dict(block.input)
            break

    return CallResult(
        ok=bool(payload),
        payload=payload,
        model=model_short,
        input_tokens=input_tokens,
        cached_read_tokens=cached_read,
        cached_write_tokens=cached_write,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        error=None if payload else "no tool_use block",
    )


def extract_page(
    source_id: int,
    pdf_path: Path,
    year: int,
    paper: int,
    page: int,
    *,
    budget_usd: float,
    use_cache: bool = True,
    include_opus: bool = False,
) -> dict:
    """Extract one page with tier escalation. Returns the accepted payload."""
    cache_file = _cache_path(year, paper, page)
    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if cached.get("_prompt_hash") == _prompt_hash():
            return cached["payload"]

    png = render_page_png(pdf_path, page)
    tiers = list(ESCALATION_TIERS)
    if include_opus:
        tiers.append("claude-opus-4-7")

    last_reason: Optional[str] = None
    for model in tiers:
        check_budget(budget_usd)
        cr = call_one_tier(model, png, year, paper, page)
        ok_payload, reason = quality_check(cr.payload) if cr.ok else (False, cr.error)

        log_call(
            call_type="extract_question",
            model=model,
            input_tokens=cr.input_tokens,
            cached_tokens=cr.cached_read_tokens + cr.cached_write_tokens,
            output_tokens=cr.output_tokens,
            cost_usd=cr.cost_usd,
            latency_ms=cr.latency_ms,
            source_id=source_id,
            page=page,
            prompt_hash=_prompt_hash(),
            ok=ok_payload,
            error_message=reason,
        )

        if ok_payload:
            cache_file.write_text(json.dumps({
                "_prompt_hash": _prompt_hash(),
                "_model": model,
                "_cost_usd": cr.cost_usd,
                "payload": cr.payload,
            }, indent=2))
            return cr.payload

        last_reason = reason
        sys.stderr.write(f"  [page {page}] {model} failed: {reason} — escalating\n")

    # All tiers failed; record into review queue and raise.
    with connect() as conn:
        conn.execute(
            "insert into review_queue (source_id, page, reason, detail) values (?, ?, ?, ?)",
            (source_id, page, "extraction_failed_all_tiers", last_reason or ""),
        )
        conn.commit()
    raise RuntimeError(f"page {page}: all tiers failed; last reason: {last_reason}")


# ───── paper-level orchestration ──────────────────────────────────────

def _make_qid(year: int, paper: int, question_number: int, part: Optional[str],
              section: Optional[str] = None) -> str:
    """Deterministic question id.

    Section B and Paper 1 use the legacy format `<year>-p<paper>-q<n>[-<part>]`.
    Section A (MC) is prefixed with `sA` to disambiguate from Section B numbering —
    e.g. 2023 P2's Section A Q1 and Section B Q1 are distinct questions that share
    `question_number=1`. The unique constraint on questions is section-aware (migration 004)
    so the underlying row can also be disambiguated by section + qnum without needing
    the id prefix, but the id prefix makes the row's section unambiguous in URLs/logs."""
    if section == "A":
        base = f"{year}-p{paper}-sA-q{question_number}"
    else:
        base = f"{year}-p{paper}-q{question_number}"
    if part:
        return f"{base}-{part.replace('.', '-')}"
    return base


def _stitch(per_page: list[tuple[int, dict]]) -> tuple[list[dict], list[dict]]:
    """Merge fragments that continue across pages and reconcile missing/inconsistent
    question_numbers using the most-recent open question.

    Monotonicity invariant: in any single Methods paper, question numbers appear in strictly
    non-decreasing order and never skip. So:
      - A qnum lower than `last_open_qnum` is wrong (backwards = misidentified continuation).
      - A qnum that jumps past `max_qnum_seen + 1` is wrong (forward = misidentified continuation).
    Both cases get auto-corrected to `last_open_qnum` and logged for review.

    Returns (good_fragments, review_items). review_items are fragments we couldn't
    confidently place at all.
    """
    out: list[dict] = []
    review: list[dict] = []
    last_open_qnum: Optional[int] = None  # most-recent question number we've seen content for
    max_qnum_seen: int = 0
    last_section: Optional[str] = None  # tracks transition A→B (or unsectioned→A) so we reset
                                         # the monotonicity counters; Section A's Q20 doesn't
                                         # block Section B's Q1 from starting fresh.
    # Per-page correction map: when one fragment on a page is reconciled (e.g. qnum=2→1),
    # remember the substitution so later fragments on the SAME page that report the same
    # wrong qnum get corrected too. Common case: page 11 of 2023 P2 has four fragments
    # all mis-labelled as Q2 by the model; only the first (cont=true) is caught by the
    # monotonicity check — siblings (cont=false) need this propagation.
    page_corrections: dict[int, dict[int, int]] = {}

    for page_num, page_result in per_page:
        if page_result.get("page_type") != "questions":
            continue

        for frag in page_result.get("fragments", []):
            f = dict(frag)
            f["_page_start"] = page_num
            f["_page_end"] = page_num
            cur_section = frag.get("section")
            # The model omits `section` on continuation pages (the page banner reads
            # "SECTION B – continued" but the model treats the field as optional). Fill
            # in from the prior fragment so all Section B continuation rows carry section='B'.
            if cur_section is None and last_section is not None:
                cur_section = last_section
            f["_section"] = cur_section
            # Section transitions reset the monotonicity counters — Paper 2 has Section A
            # numbered Q1..Q20 and Section B numbered Q1..Q5, so Q1 in §B is NOT a backwards
            # jump from §A's Q20.
            if cur_section != last_section and last_section is not None and cur_section is not None:
                last_open_qnum = None
                max_qnum_seen = 0
            last_section = cur_section if cur_section is not None else last_section
            kind = f.get("fragment_kind")
            qnum = f.get("question_number")
            # For sub_stems, fold before_part into the part slot so the DB sees a unique
            # (question_number, part) tuple per sub_stem (part='pre-c', 'pre-e', etc.).
            # The renderer pulls these out by the 'pre-' prefix.
            if kind == "sub_stem" and f.get("before_part") and not f.get("part"):
                f["part"] = "pre-" + str(f["before_part"])
            part = f.get("part")
            cont = bool(f.get("is_continuation_from_prior_page"))

            # ---- reconcile question_number ----
            reconciled_reason: Optional[str] = None
            # First, check page-level correction map. If a sibling on this page was already
            # reconciled from the same wrong qnum, apply the same correction.
            if qnum is not None and qnum in page_corrections.get(page_num, {}):
                corrected = page_corrections[page_num][qnum]
                f["_reconciled_qnum_from"] = qnum
                f["question_number"] = corrected
                reconciled_reason = (
                    f"qnum {qnum} corrected to {corrected} (sibling on page {page_num} "
                    f"was already corrected from the same wrong qnum)"
                )
                qnum = corrected
            elif qnum is None:
                if last_open_qnum is not None:
                    f["question_number"] = last_open_qnum
                    reconciled_reason = f"filled missing qnum from prior context = {last_open_qnum}"
                else:
                    f["_review_reason"] = "missing question_number and no prior open question"
                    f["_review_page"] = page_num
                    review.append(f)
                    continue
            elif last_open_qnum is not None and qnum < last_open_qnum:
                # Backwards jump — Methods papers never go back in question numbering.
                # Almost certainly the model misidentified the question on a continuation page.
                reconciled_reason = (
                    f"qnum {qnum} < last_open {last_open_qnum} (backwards is impossible in a Methods paper) — "
                    f"corrected to {last_open_qnum}"
                )
                f["_reconciled_qnum_from"] = qnum
                f["question_number"] = last_open_qnum
            elif last_open_qnum is not None and qnum > max_qnum_seen + 1:
                # Forward jump past max+1 — Methods papers don't skip numbers (1, 2, 3, ..., 9).
                reconciled_reason = (
                    f"qnum {qnum} skips past max_seen+1={max_qnum_seen + 1} — "
                    f"corrected to last_open {last_open_qnum}"
                )
                f["_reconciled_qnum_from"] = qnum
                f["question_number"] = last_open_qnum
            elif cont and last_open_qnum is not None and qnum != last_open_qnum:
                # Model says continuation but disagrees on qnum (and the jump is plausible-looking).
                reconciled_reason = (
                    f"qnum disagrees with prior open: model said {qnum}, "
                    f"prior open was {last_open_qnum} — corrected to {last_open_qnum}"
                )
                f["question_number"] = last_open_qnum
                f["_reconciled_qnum_from"] = qnum
            qnum = f["question_number"]

            # ---- try to merge into the most-recent matching open same-part fragment ----
            if cont:
                merged = False
                for existing in reversed(out):
                    if (existing.get("question_number") == qnum
                            and existing.get("part") == part
                            and existing.get("fragment_kind") == kind
                            and not existing.get("_closed", False)):
                        existing["prompt_md"] = (existing.get("prompt_md", "") + "\n" + f.get("prompt_md", "")).strip()
                        existing["_page_end"] = page_num
                        if f.get("is_complete_on_this_page"):
                            existing["_closed"] = True
                        if existing.get("marks") is None and f.get("marks") is not None:
                            existing["marks"] = f["marks"]
                        if not existing.get("has_diagram") and f.get("has_diagram"):
                            existing["has_diagram"] = True
                        merged = True
                        break
                if merged:
                    last_open_qnum = qnum
                    continue
                # No matching open same-part fragment. This is a new fragment that
                # happens to share its question_number with a prior page — fine, just append.

            if reconciled_reason:
                f["_reconciliation_note"] = reconciled_reason
            # Record the page-level correction so subsequent siblings on this page get the same fix.
            if "_reconciled_qnum_from" in f:
                page_corrections.setdefault(page_num, {})[f["_reconciled_qnum_from"]] = qnum
            if f.get("is_complete_on_this_page"):
                f["_closed"] = True
            out.append(f)
            last_open_qnum = qnum
            max_qnum_seen = max(max_qnum_seen, qnum)

    return out, review


def insert_questions(source_id: int, year: int, paper: int, fragments: list[dict],
                     extraction_models: dict[int, str]) -> int:
    """Replace all questions for this source with the given fragments. Returns count inserted.

    We delete-then-insert (within a single transaction) so re-runs never leave stale rows from
    prior extractions whose IDs no longer appear in the current pass — e.g. a question that was
    mis-numbered Q9.d in one run and corrected to Q7.d in the next would otherwise leave both
    rows in the table."""
    # Delete-before-insert only for the sections actually represented in this
    # fragment batch — so re-extracting just Section A pages doesn't wipe Section B.
    sections_to_replace: set[Optional[str]] = set()
    for f in fragments:
        sections_to_replace.add(f.get("_section"))
    if not sections_to_replace:
        sections_to_replace = {None}

    conn = connect()
    try:
        # extraction_log.question_id has no on-delete-cascade (intentional — audit log outlives
        # questions). Null the refs for the questions we're about to delete so the delete
        # isn't blocked by stale tagging-phase log rows.
        for sec in sections_to_replace:
            if sec is None:
                conn.execute(
                    "update extraction_log set question_id = null "
                    "where question_id in (select id from questions where source_id = ? and section is null)",
                    (source_id,),
                )
                conn.execute(
                    "delete from questions where source_id = ? and section is null",
                    (source_id,),
                )
            else:
                conn.execute(
                    "update extraction_log set question_id = null "
                    "where question_id in (select id from questions where source_id = ? and section = ?)",
                    (source_id, sec),
                )
                conn.execute(
                    "delete from questions where source_id = ? and section = ?",
                    (source_id, sec),
                )
        n = 0
        for f in fragments:
            qnum = f["question_number"]
            part = f.get("part")
            section = f.get("_section")
            qid = _make_qid(year, paper, qnum, part, section=section)
            kind = f.get("fragment_kind")
            is_mc = 1 if (kind == "mc" or f.get("is_mc")) else 0
            # Persist options in unified dict form: [{"kind":"text","md":...}, ...] or
            # [{"kind":"diagram","bbox":[...],"path":null}, ...]. Legacy `mc_options_md`
            # (array of strings) is normalised to the text-kind dict shape on insert.
            mc_opts_unified: Optional[list[dict]] = None
            if f.get("mc_options"):
                mc_opts_unified = []
                for o in f["mc_options"]:
                    if o["kind"] == "text":
                        mc_opts_unified.append({"kind": "text", "md": o.get("md", "")})
                    else:
                        mc_opts_unified.append({"kind": "diagram", "bbox": o["bbox"], "path": None})
            elif f.get("mc_options_md"):
                mc_opts_unified = [{"kind": "text", "md": s} for s in f["mc_options_md"]]
            mc_opts_json = json.dumps(mc_opts_unified) if mc_opts_unified else None

            # MC questions with diagram options need careful has_diagram handling:
            #  - Q15-style (only diagram options, no stem diagram): suppress has_diagram
            #    so the paper-level extractor doesn't try to crop "one" diagram and
            #    arbitrarily pick a single option panel.
            #  - Q7-style (stem diagram "shown below" + diagram options): KEEP has_diagram
            #    so the stem diagram gets cropped alongside the option panels.
            # The signal is whether the prompt explicitly references a diagram "below" /
            # "above" the question text — the model's own `has_diagram` flag is unreliable
            # in the presence of diagram options (it sometimes sets it for option panels
            # alone, sometimes misses the stem diagram).
            has_diagram_flag = 1 if f.get("has_diagram") else 0
            if mc_opts_unified and any(o.get("kind") == "diagram" for o in mc_opts_unified):
                prompt_lc = (f.get("prompt_md") or "").lower()
                stem_diagram_hint = any(
                    phrase in prompt_lc
                    for phrase in ("shown below", "graph below", "diagram below",
                                   "figure below", "shown above", "graph above")
                )
                has_diagram_flag = 1 if stem_diagram_hint else 0
            page_start = f["_page_start"]
            page_end = f["_page_end"]
            # Use the model from the first page the fragment touched.
            model = extraction_models.get(page_start)

            conn.execute(
                """
                insert into questions
                  (id, source_id, section, question_number, part, marks, prompt_md,
                   is_mc, mc_options_md, has_diagram, source_page_start, source_page_end,
                   extraction_model)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (id) do update set
                  source_id          = excluded.source_id,
                  section            = excluded.section,
                  question_number    = excluded.question_number,
                  part               = excluded.part,
                  marks              = excluded.marks,
                  prompt_md          = excluded.prompt_md,
                  is_mc              = excluded.is_mc,
                  mc_options_md      = excluded.mc_options_md,
                  has_diagram        = excluded.has_diagram,
                  source_page_start  = excluded.source_page_start,
                  source_page_end    = excluded.source_page_end,
                  extraction_model   = excluded.extraction_model
                """,
                (qid, source_id, section, qnum, part, f.get("marks"), f.get("prompt_md", ""),
                 is_mc, mc_opts_json, has_diagram_flag, page_start, page_end,
                 model),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def extract_paper(
    year: int,
    paper: int,
    *,
    budget_usd: float = 1.00,
    pages: Optional[list[int]] = None,
    force: bool = False,
    include_opus: bool = False,
) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "select id, pdf_path from sources where year = ? and paper = ?",
            (year, paper),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"no source for {year} paper {paper}")
    source_id = row["id"]
    pdf_path = REPO_ROOT / row["pdf_path"]

    doc = fitz.open(pdf_path)
    n_pages = doc.page_count
    doc.close()

    target_pages = pages or list(range(1, n_pages + 1))

    per_page_results: list[tuple[int, dict]] = []
    extraction_models: dict[int, str] = {}
    skipped: list[int] = []

    for p in target_pages:
        try:
            payload = extract_page(
                source_id, pdf_path, year, paper, p,
                budget_usd=budget_usd, use_cache=not force, include_opus=include_opus,
            )
        except BudgetExceeded as e:
            sys.stderr.write(f"BUDGET EXCEEDED on page {p}: {e}\n")
            raise
        per_page_results.append((p, payload))
        # Record model from cache file (most recent cache write).
        cf = _cache_path(year, paper, p)
        if cf.exists():
            extraction_models[p] = json.loads(cf.read_text()).get("_model", "")
        if payload.get("page_type") != "questions":
            skipped.append(p)
        sys.stderr.write(
            f"  page {p:>2}: {payload.get('page_type'):<14} "
            f"{len(payload.get('fragments', [])):>2} fragments  "
            f"({payload.get('page_summary', '')[:70]})\n"
        )

    # Persist skipped pages on the source row.
    if pages is None:
        conn = connect()
        try:
            conn.execute(
                "update sources set skipped_pages = ? where id = ?",
                (json.dumps(skipped), source_id),
            )
            conn.commit()
        finally:
            conn.close()

    fragments, review_items = _stitch(per_page_results)
    n_inserted = insert_questions(source_id, year, paper, fragments, extraction_models)

    # Log reconciliation notes + unplaceable fragments to review_queue.
    # Clear prior review entries for this source so we don't accumulate stale ones.
    conn = connect()
    try:
        conn.execute(
            "delete from review_queue where source_id = ? and reason in ('reconciled_qnum', 'unplaceable_fragment') and resolved = 0",
            (source_id,),
        )
        for f in fragments:
            if f.get("_reconciliation_note"):
                qid = _make_qid(year, paper, f["question_number"], f.get("part"),
                                section=f.get("_section"))
                conn.execute(
                    "insert into review_queue (question_id, source_id, page, reason, detail) values (?, ?, ?, ?, ?)",
                    (qid, source_id, f["_page_start"], "reconciled_qnum", f["_reconciliation_note"]),
                )
        for f in review_items:
            conn.execute(
                "insert into review_queue (source_id, page, reason, detail) values (?, ?, ?, ?)",
                (source_id, f.get("_review_page"), "unplaceable_fragment",
                 f"{f.get('_review_reason')} — fragment: kind={f.get('fragment_kind')} part={f.get('part')} marks={f.get('marks')} prompt={(f.get('prompt_md') or '')[:120]!r}"),
            )
        # Safety net: flag stems/sub_stems whose prompt mentions two diagrams.
        # See CLAUDE.md "One diagram per stem/sub_stem fragment". The extraction
        # prompt now teaches the model to split these, but the human reviewer
        # should still be alerted if the split was missed.
        conn.execute(
            "delete from review_queue where source_id = ? and reason = 'possible_missed_substem_diagram' and resolved = 0",
            (source_id,),
        )
        # Count forward ("...below") and backward ("...above") references separately.
        # Two diagrams in one preamble means two FORWARD references (or two backward
        # refs), not one of each — "shown below ... shown above" describes the same
        # diagram from two sides (e.g. 2020 P1 Q8 stem) and should not flag.
        below_re = re.compile(r"(?:shown|graph|diagram|figure)\s+below", re.IGNORECASE)
        above_re = re.compile(r"(?:shown|graph|diagram|figure)\s+above", re.IGNORECASE)
        for f in fragments:
            if f.get("fragment_kind") not in ("stem", "sub_stem", "mc"):
                continue
            text = f.get("prompt_md") or ""
            n_below = len(below_re.findall(text))
            n_above = len(above_re.findall(text))
            if max(n_below, n_above) >= 2:
                qid = _make_qid(year, paper, f["question_number"], f.get("part"),
                                section=f.get("_section"))
                conn.execute(
                    "insert into review_queue (question_id, source_id, page, reason, detail) values (?, ?, ?, ?, ?)",
                    (qid, source_id, f.get("_page_start"),
                     "possible_missed_substem_diagram",
                     f"{f.get('fragment_kind')} contains {n_below} forward + {n_above} backward diagram references — verify whether a sub_stem split was missed. prompt: {text[:200]!r}"),
                )
        # Safety net: flag parts whose marks equal the sum of their .i/.ii sub-parts.
        # e.g. 2021 P1 Q9.c was emitted as a 4-mark "part c" but the actual sub-parts
        # are c.i (2 marks) + c.ii (2 marks). The "part c" is really a sub_stem.
        conn.execute(
            "delete from review_queue where source_id = ? and reason = 'possible_misclassified_substem' and resolved = 0",
            (source_id,),
        )
        parts_by_key: dict[tuple, dict] = {}
        kids_marks_by_parent: dict[tuple, int] = {}
        for f in fragments:
            if f.get("fragment_kind") != "part" or not f.get("part") or f.get("marks") is None:
                continue
            key = (f.get("_section"), f["question_number"])
            label = f["part"]
            if "." in label:
                parent_letter = label.split(".", 1)[0]
                kids_marks_by_parent[(key, parent_letter)] = (
                    kids_marks_by_parent.get((key, parent_letter), 0) + (f["marks"] or 0)
                )
            elif len(label) == 1:
                parts_by_key[(key, label)] = f
        for (key, letter), part_f in parts_by_key.items():
            kids = kids_marks_by_parent.get((key, letter))
            if kids is not None and kids == part_f["marks"]:
                qid = _make_qid(year, paper, part_f["question_number"], part_f["part"],
                                section=part_f.get("_section"))
                conn.execute(
                    "insert into review_queue (question_id, source_id, page, reason, detail) values (?, ?, ?, ?, ?)",
                    (qid, source_id, part_f.get("_page_start"),
                     "possible_misclassified_substem",
                     f"part '{letter}' has marks={part_f['marks']} equal to sum of {letter}.i/.ii sub-parts ({kids}). Likely a sub_stem misread as a part — should be sub_stem with before_part='{letter}.i', marks=null."),
                )
        conn.commit()
    finally:
        conn.close()

    # Summarise spend for this paper.
    conn = connect()
    try:
        spent = conn.execute(
            "select coalesce(sum(cost_usd), 0.0) from extraction_log where source_id = ? and call_type='extract_question'",
            (source_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "source_id": source_id,
        "pages_processed": len(per_page_results),
        "skipped_pages": skipped,
        "fragments": len(fragments),
        "questions_inserted": n_inserted,
        "spend_usd": spent,
    }


# ───── CLI ────────────────────────────────────────────────────────────

def _parse_pages(s: str) -> list[int]:
    out: list[int] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--paper", type=int, required=True, choices=[1, 2])
    ap.add_argument("--budget", type=float, default=1.00, help="USD cap, raises BudgetExceeded if total spend hits this")
    ap.add_argument("--pages", type=_parse_pages, help="restrict to specific pages, e.g. '3' or '3-5,9'")
    ap.add_argument("--force", action="store_true", help="ignore on-disk cache")
    ap.add_argument("--include-opus", action="store_true", help="include claude-opus-4-7 as a higher tier")
    ap.add_argument("--cheap", action="store_true", help="prepend Haiku 4.5 as the first tier (warning: silently miss-reads stacked fractions)")
    args = ap.parse_args()

    if args.cheap:
        # Mutate module-level tier list before extract_paper runs.
        global ESCALATION_TIERS
        ESCALATION_TIERS = ("claude-haiku-4-5",) + ESCALATION_TIERS

    try:
        summary = extract_paper(
            args.year, args.paper,
            budget_usd=args.budget,
            pages=args.pages,
            force=args.force,
            include_opus=args.include_opus,
        )
    except BudgetExceeded as e:
        print(f"BUDGET EXCEEDED: {e}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
