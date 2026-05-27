"""Diagram extraction.

For each question flagged has_diagram=1, locate the diagram region on its source page(s)
via a focused Claude vision call, then crop the source PDF to that region and save as
vector SVG. The text/tables surrounding the diagram are NOT preserved — the SVG contains
only the diagram itself, ready to embed into the generated practice paper.

Per question:
  1. Render the source page as a PNG at DPI for the vision call
  2. Ask Claude (Sonnet 4.6) for the normalized bbox of the diagram on this page
  3. Convert normalized bbox → PDF coordinates
  4. Use PyMuPDF: insert page into a fresh doc, set cropbox, write SVG via get_svg_image
  5. Save as assets/diagrams/{question_id}.svg; update questions.diagram_path

Idempotent: existing SVG files are reused unless --force is passed.

CLI:
  python -m pipeline.extract_diagrams --year 2023 --paper 1
  python -m pipeline.extract_diagrams --year 2023 --paper 1 --force
  python -m pipeline.extract_diagrams --question 2023-p1-q3-a
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

import fitz
from anthropic import Anthropic
from dotenv import load_dotenv

from pipeline.db import DEFAULT_SUBJECT, REPO_ROOT, SUBJECTS, SubjectSpec, connect, subject_spec
from pipeline.extract_questions import (
    ANTHROPIC_MODEL_IDS,
    _client,
    render_page_png,
)
from pipeline.spend import (
    BudgetExceeded,
    check_budget,
    estimate_cost,
    log_call,
)

load_dotenv(REPO_ROOT / ".env")

DIAGRAMS_DIR = REPO_ROOT / "assets" / "diagrams"
DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)

DIAGRAM_MODEL = "claude-sonnet-4-6"
DIAGRAM_DPI = 150  # lower than question-extraction DPI; bbox doesn't need pixel-precise input

# Padding (in normalised page coordinates) added around the model's bbox to avoid
# clipping axis labels or tick marks.
BBOX_PADDING = 0.005


DIAGRAM_PROMPT_TEMPLATE = """You identify the bounding box of a SPECIFIC diagram on a single page of a VCE {display_name} exam.

# What counts as a "diagram"
A diagram is a NON-TEXT visual element:
- A coordinate plane (axes with tick marks, possibly with a function graph drawn on it, points, regions shaded, etc.)
- A geometric figure (triangle, circle, polygon, intersecting lines, etc.)
- A tree diagram, Venn diagram, or other schematic
- A statistical plot

A diagram is NOT:
- A printed table (rows of numbers / text — those are tables, treated as vector text in the output)
- The right-side "DO NOT WRITE IN THIS AREA" stripe along the page edge
- Horizontal answer-space lines that students write on (these run across most of the bottom of the page)
- Page numbers, headers, "TURN OVER" marks, or the running title bar

# Specifically you are looking for
The diagram associated with the question described below. Find ONLY that diagram. If the page has multiple diagrams, return the one that matches the question.

# Output
Return a tight bounding box covering ALL parts of the diagram (axes, axis labels, tick marks, drawn curves, labelled points, captions like "track 1" / "track 2"). Coordinates are normalised 0..1, with (0,0) at the top-left corner of the page and (1,1) at the bottom-right.

If the diagram is not present on this page (model thought it was but it isn't), set found=false.

Call the record_diagram_bbox tool exactly once."""


DIAGRAM_TOOL: dict[str, Any] = {
    "name": "record_diagram_bbox",
    "description": "Record the tight bounding box of the diagram for the specified question.",
    "input_schema": {
        "type": "object",
        "required": ["found"],
        "properties": {
            "found": {
                "type": "boolean",
                "description": "True if the diagram is visible on this page, false otherwise.",
            },
            "bbox_normalised": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[x0, y0, x1, y1] in normalised page coords (0..1), top-left origin. Required when found=true.",
            },
            "description": {
                "type": "string",
                "description": "One-sentence description of what the diagram shows (for the audit view).",
            },
        },
    },
}


def find_diagram_bbox(
    spec: SubjectSpec,
    source_id: int,
    pdf_path: Path,
    year: int,
    paper: int,
    page: int,
    question_id: str,
    question_prompt: str,
) -> Optional[dict]:
    """Returns {bbox: [x0,y0,x1,y1] in PDF coords, description: str} or None if not found."""
    png = render_page_png(pdf_path, page, dpi=DIAGRAM_DPI)
    b64 = base64.standard_b64encode(png).decode("ascii")

    user_msg = (
        f"Page {page} of {year} {spec.display_name} Paper {paper}. "
        f"Find the diagram that belongs to question {question_id}. "
        f"The question text reads: {question_prompt[:400]}"
    )

    diagram_prompt = DIAGRAM_PROMPT_TEMPLATE.replace("{display_name}", spec.display_name)

    t0 = time.time()
    try:
        resp = _client().messages.create(
            model=ANTHROPIC_MODEL_IDS[DIAGRAM_MODEL],
            max_tokens=1024,
            system=[{"type": "text", "text": diagram_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[DIAGRAM_TOOL],
            tool_choice={"type": "tool", "name": "record_diagram_bbox"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": b64},
                        },
                        {"type": "text", "text": user_msg},
                    ],
                }
            ],
        )
    except Exception as e:
        log_call(
            call_type="extract_diagram", model=DIAGRAM_MODEL,
            input_tokens=0, cached_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=int((time.time() - t0) * 1000),
            source_id=source_id, page=page, question_id=question_id,
            ok=False, error_message=str(e),
        )
        return None

    latency_ms = int((time.time() - t0) * 1000)
    usage = resp.usage
    input_t = usage.input_tokens
    out_t = usage.output_tokens
    cached_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    cached_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = estimate_cost(
        DIAGRAM_MODEL,
        input_tokens=input_t, cached_read_tokens=cached_r,
        cached_write_tokens=cached_w, output_tokens=out_t,
    )

    payload: dict = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_diagram_bbox":
            payload = dict(block.input)
            break

    log_call(
        call_type="extract_diagram", model=DIAGRAM_MODEL,
        input_tokens=input_t, cached_tokens=cached_r + cached_w,
        output_tokens=out_t, cost_usd=cost, latency_ms=latency_ms,
        source_id=source_id, page=page, question_id=question_id,
        ok=bool(payload), error_message=None if payload else "no tool_use block",
    )

    if not payload.get("found"):
        return None
    bbox_n = payload.get("bbox_normalised") or []
    if len(bbox_n) != 4:
        return None

    # Apply small padding then convert to PDF coords.
    x0n, y0n, x1n, y1n = bbox_n
    x0n = max(0.0, x0n - BBOX_PADDING)
    y0n = max(0.0, y0n - BBOX_PADDING)
    x1n = min(1.0, x1n + BBOX_PADDING)
    y1n = min(1.0, y1n + BBOX_PADDING)
    if x1n <= x0n or y1n <= y0n:
        return None

    doc = fitz.open(pdf_path)
    pw, ph = doc[0].rect.width, doc[0].rect.height
    doc.close()
    return {
        "bbox_pdf": [x0n * pw, y0n * ph, x1n * pw, y1n * ph],
        "bbox_normalised": [x0n, y0n, x1n, y1n],
        "description": payload.get("description") or "",
    }


# "DO NOT WRITE IN THIS AREA" stripes appear on both sides of each question page.
# VCAA uses two page-width families:
#   - 623.62pt: Methods 2019+ question pages — wider custom format
#   - 595.28pt: standard A4 — all Specialist papers, Methods 2016–2018, and the
#     formula-sheet pages at the back of every modern Methods paper
# Sidebar text band measurements (taken from Methods 2019+ Q-pages and from
# Specialist 2023 P1 page 2): the right sidebar text starts around x=570 on A4
# pages, so we clamp to x=568 to leave a 2pt margin clear of sidebar text.
# Width-based (not subject-based) dispatch so future formats slot in cleanly
# and Methods 2016–2018 pages also get the correct A4 clamps.
_SIDEBAR_BY_WIDTH: dict[int, tuple[int, int]] = {
    624: (50, 580),   # Methods 2019+ question pages (page width 623.62pt)
    595: (50, 568),   # standard A4 (Specialist 2016+, Methods 2016–2018, formula sheets)
}


def _sidebar_x_for(page_width: float) -> tuple[int, int]:
    """(left_end, right_start) for the page's sidebars, dispatched by width."""
    rounded = int(round(page_width))
    if rounded in _SIDEBAR_BY_WIDTH:
        return _SIDEBAR_BY_WIDTH[rounded]
    # Fall back to A4 defaults — safer (slightly narrower) for any unfamiliar page.
    return _SIDEBAR_BY_WIDTH[595]
# How far outside the model's rough bbox we search for prose/labels when refining.
REFINE_MARGIN_PT = 100
# A text span is classified as PROSE (question text bracketing the diagram) when
# either its rendered character length OR its horizontal span on the page is large.
# Diagram labels — even multi-token ones like "g(x) = (1/2)f(2-x)" or "inverse of g_1" —
# stay under both thresholds; question paragraph text reliably exceeds at least one.
LABEL_CHAR_THRESHOLD = 25
PROSE_SPAN_WIDTH_PT = 250
PNG_DPI = 300


def _refine_bbox_via_drawings(pdf_path: Path, page: int, rough_bbox: list,
                                 *, y_upper_bound: Optional[float] = None) -> Optional[tuple[list, dict]]:
    """Use PyMuPDF vector drawings to find the true visual extent of the diagram.

    Strategy:
      1. Get all vector drawings on the page (axes, curves, dashed lines, points, fills).
      2. Filter to drawings whose center is inside the model's rough bbox (with slack)
         and whose area is small relative to the rough bbox (excludes the page-frame
         border and the "DO NOT WRITE" sidebar fills).
      3. Drop horizontal full-width rules (working-line answer rules — height=0 and
         width > 60% of content area).
      4. Take the union of kept drawings — this is the tight visual diagram bbox.
      5. Expand to include short text labels (≤ LABEL_CHAR_THRESHOLD chars OR axis
         tick numbers) that sit within ±5pt of the y-range and within ±60pt of the
         x-range. Question text and prose stay outside the y-band so they don't
         intrude.
      6. Clamp to the [sidebar_left+5, sidebar_right-5] band (per-page via _sidebar_x_for).

    Returns (refined_bbox, debug_info) or None if no diagram drawings were found
    (in which case the caller should fall back to the legacy text-bracketed refiner).
    """
    rx0, ry0, rx1, ry1 = rough_bbox
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        page_w = pg.rect.width
        page_h = pg.rect.height
        drawings = pg.get_drawings()
        text_dict = pg.get_text("dict")
    finally:
        src.close()

    sidebar_left, sidebar_right = _sidebar_x_for(page_w)
    rough_area = max(1.0, (rx1 - rx0) * (ry1 - ry0))
    SLACK = 30           # pt — slack around the rough bbox when filtering drawings
    CONTENT_WIDTH = sidebar_right - sidebar_left

    # Collect prose-block y-bands so we can exclude drawings that overlap them.
    # PyMuPDF blocks group entire paragraphs even when full of CID-math fragments,
    # so this catches math-text lines whose fraction bars are vector drawings —
    # those bars must NOT count toward the diagram's vertical extent.
    src2 = fitz.open(pdf_path)
    try:
        pg2 = src2.load_page(page - 1)
        prose_blocks_raw = pg2.get_text("blocks")
    finally:
        src2.close()
    prose_y_bands: list[tuple[float, float]] = []
    for x0b, y0b, x1b, y1b, text, _bno, _btype in prose_blocks_raw:
        if x1b <= sidebar_left or x0b >= sidebar_right:
            continue
        stripped = (text or "").strip().replace("\n", " ")
        if not stripped:
            continue
        block_w = x1b - x0b
        if len(stripped) > LABEL_CHAR_THRESHOLD or block_w > PROSE_SPAN_WIDTH_PT:
            prose_y_bands.append((y0b, y1b))

    def _in_prose_band(cy: float) -> bool:
        for py0, py1 in prose_y_bands:
            if py0 - 1 <= cy <= py1 + 1:
                return True
        return False

    keep: list[tuple[float, float, float, float]] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        dx0, dy0, dx1, dy1 = r.x0, r.y0, r.x1, r.y1
        # Exclude page chrome (full-content-area borders/fills).
        if (dx1 - dx0) * (dy1 - dy0) > 0.6 * rough_area * 4:
            continue
        # Exclude sidebar drawings entirely.
        if dx1 <= sidebar_left or dx0 >= sidebar_right:
            continue
        # Drop very wide, height-0 horizontal rules — these are answer-space working lines.
        if (dy1 - dy0) < 1 and (dx1 - dx0) > 0.6 * CONTENT_WIDTH:
            continue
        # Drop drawings whose centre falls inside a prose block — these are typesetting
        # strokes (fraction bars, underlines on \mathbf or "show that") inside question
        # text, NOT diagram elements.
        cx, cy = (dx0 + dx1) / 2, (dy0 + dy1) / 2
        if _in_prose_band(cy):
            continue
        # Centre-of-drawing must be inside the rough bbox (with slack).
        if not (rx0 - SLACK <= cx <= rx1 + SLACK and ry0 - SLACK <= cy <= ry1 + SLACK):
            continue
        # Hard y upper-bound (used for MC stem diagrams to avoid spilling into the
        # option-panel region below — see refine_bbox docstring).
        if y_upper_bound is not None and cy > y_upper_bound:
            continue
        keep.append((dx0, dy0, dx1, dy1))

    if not keep:
        return None

    x0 = min(b[0] for b in keep)
    y0 = min(b[1] for b in keep)
    x1 = max(b[2] for b in keep)
    y1 = max(b[3] for b in keep)

    # Expand to include short text labels within the diagram's y-band. These are
    # axis labels (x, y), tick numbers (1000, 2000, …), point coordinates ((20, 700)),
    # and axis-name labels like "t (weeks)" / "P (number of individuals)".
    Y_EXPAND_MARGIN = 12  # accept labels up to 12pt above/below the drawings y-range
    X_EXPAND_MARGIN = 80  # accept labels up to 80pt outside the drawings x-range
    labels_added = 0
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                tx0, ty0, tx1, ty1 = span["bbox"]
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                # Skip prose (long text) — that's question text, not a label.
                if len(txt) > 40:
                    continue
                # Skip sidebar.
                if tx1 <= sidebar_left or tx0 >= sidebar_right:
                    continue
                # Must be within the diagram's y-band plus a small margin.
                if ty1 < y0 - Y_EXPAND_MARGIN or ty0 > y1 + Y_EXPAND_MARGIN:
                    continue
                # And not wildly far from the drawings horizontally.
                if tx1 < x0 - X_EXPAND_MARGIN or tx0 > x1 + X_EXPAND_MARGIN:
                    continue
                # Expand bbox to fit this label.
                x0 = min(x0, tx0 - 2)
                y0 = min(y0, ty0 - 2)
                x1 = max(x1, tx1 + 2)
                y1 = max(y1, ty1 + 2)
                labels_added += 1

    # Drawings can capture fraction bars / underscores from math-text BELOW the actual
    # diagram (e.g. 2022 P2 Q2's "r(t) = 1700 sin(πt/80) + 2500" sits below the diagram
    # but its fraction bar is a vector stroke that leaks into the drawings union).
    # Clip the refined bbox to STRICTLY ABOVE the first prose line below the drawings
    # area. Prose = line containing >25 chars total OR spanning >250pt horizontally.
    drawings_y_max = max(b[3] for b in keep)
    drawings_y_min = min(b[1] for b in keep)
    # PyMuPDF's get_text("blocks") groups text into paragraph-like blocks even when
    # the line is full of CID-encoded math fragments. We use blocks (not spans or
    # lines) to find prose anchors that bracket the diagram vertically.
    src2 = fitz.open(pdf_path)
    try:
        pg2 = src2.load_page(page - 1)
        prose_blocks = pg2.get_text("blocks")
    finally:
        src2.close()
    prose_below_y: Optional[float] = None
    prose_above_y: Optional[float] = None
    for x0b, y0b, x1b, y1b, text, _bno, _btype in prose_blocks:
        if x1b <= sidebar_left or x0b >= sidebar_right:
            continue
        stripped = (text or "").strip().replace("\n", " ")
        if not stripped:
            continue
        block_w = x1b - x0b
        is_prose = len(stripped) > LABEL_CHAR_THRESHOLD or block_w > PROSE_SPAN_WIDTH_PT
        if not is_prose:
            continue
        # Block sits BELOW the drawings cluster → cap y1; block sits ABOVE → cap y0.
        if y0b > drawings_y_max + 4:
            if prose_below_y is None or y0b < prose_below_y:
                prose_below_y = y0b
        elif y1b < drawings_y_min - 4:
            if prose_above_y is None or y1b > prose_above_y:
                prose_above_y = y1b
    if prose_below_y is not None:
        y1 = min(y1, prose_below_y - 4)
    if prose_above_y is not None:
        y0 = max(y0, prose_above_y + 4)

    # Clamp to safe content area (avoid both sidebars).
    x0 = max(x0, sidebar_left + 3)
    x1 = min(x1, sidebar_right - 3)

    debug = {
        "method": "drawings",
        "original_bbox": list(rough_bbox),
        "drawings_kept": len(keep),
        "labels_added": labels_added,
        "prose_below_y": prose_below_y,
        "prose_above_y": prose_above_y,
        "refined_bbox": [x0, y0, x1, y1],
    }
    return [x0, y0, x1, y1], debug


def refine_bbox(pdf_path: Path, page: int, rough_bbox: list,
                *, y_upper_bound: Optional[float] = None) -> tuple[list, dict]:
    """Tighten the model's rough bbox.

    Primary strategy uses PyMuPDF vector drawings (axes, curves, paths) to find the
    true visual extent of the diagram. Diagram shapes are always vector — question
    text never is — so this is far more reliable than text-classification heuristics.

    Falls back to the legacy text-bracketed refiner when no diagram drawings are
    found (rare; mostly happens when the diagram is a rasterised embedded image).

    `y_upper_bound`: optional hard ceiling on the refined bbox's y_max. Used when
    cropping the STEM diagram of an MC-with-diagram-options question — the topmost
    option letter's y position is passed in so the stem-diagram bbox cannot spill
    into the option-panel region below.
    """
    via_drawings = _refine_bbox_via_drawings(pdf_path, page, rough_bbox,
                                              y_upper_bound=y_upper_bound)
    if via_drawings is not None:
        return via_drawings
    # ─── legacy text-bracketed fallback ───
    rx0, ry0, rx1, ry1 = rough_bbox
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        text_dict = pg.get_text("dict")
        sidebar_left, sidebar_right = _sidebar_x_for(pg.rect.width)
    finally:
        src.close()

    sx0, sy0 = rx0 - REFINE_MARGIN_PT, ry0 - REFINE_MARGIN_PT
    sx1, sy1 = rx1 + REFINE_MARGIN_PT, ry1 + REFINE_MARGIN_PT

    raw_labels: list[tuple[float, float, float, float]] = []  # (x0,y0,x1,y1)
    prose: list[tuple[float, float, float, float]] = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bx0, by0, bx1, by1 = span["bbox"]
                # Exclude spans that are entirely inside either sidebar stripe
                # (rotated "DO NOT WRITE" letters on left or right edge).
                if bx1 <= sidebar_left or bx0 >= sidebar_right:
                    continue
                if bx1 < sx0 or bx0 > sx1 or by1 < sy0 or by0 > sy1:
                    continue  # outside search region
                txt = span.get("text", "").strip()
                if not txt:
                    continue
                # Prose if either too long (multi-clause sentences) or too wide on the
                # page (paragraph lines span across the whole content area; diagram
                # labels stay clustered).
                span_w = bx1 - bx0
                is_prose = len(txt) > LABEL_CHAR_THRESHOLD or span_w > PROSE_SPAN_WIDTH_PT
                bucket = prose if is_prose else raw_labels
                bucket.append((bx0, by0, bx1, by1))

    # Any short span sharing a vertical band with a prose span is part of that prose
    # (e.g. 'b.', 'x', '(', ')', '1.' tokens on the same row as "Find the values of").
    # Re-classify those out of the labels set.
    Y_TOLERANCE = 4
    def _in_prose_band(y0: float, y1: float) -> bool:
        for _, py0, _, py1 in prose:
            if not (y1 < py0 - Y_TOLERANCE or y0 > py1 + Y_TOLERANCE):
                return True
        return False

    labels = [s for s in raw_labels if not _in_prose_band(s[1], s[3])]

    mid_y = (ry0 + ry1) / 2

    # Vertical bracket from prose
    prose_above = [s for s in prose if s[3] <= mid_y]
    prose_below = [s for s in prose if s[1] >= mid_y]

    diagram_y0 = max(s[3] for s in prose_above) + 4 if prose_above else ry0
    diagram_y1 = min(s[1] for s in prose_below) - 4 if prose_below else ry1

    # If the prose-derived band is degenerate, fall back to the rough bbox.
    if diagram_y0 >= diagram_y1:
        diagram_y0, diagram_y1 = ry0, ry1

    # Horizontal range from labels within the (now refined) vertical band.
    # Critical: only EXPAND the model's rough bbox using labels — never SHRINK it.
    # The model's bbox typically includes axes and curves that extend beyond text labels;
    # if we replaced x-range with just the label cluster, large diagrams (e.g. 2023 P2 Q5)
    # got severely clipped.
    diagram_x0, diagram_x1 = rx0, rx1
    labels_in_band = [s for s in labels if s[3] > diagram_y0 - 5 and s[1] < diagram_y1 + 5]
    if labels_in_band:
        # Expand y range to fully contain labels (axis-tip labels may sit just outside).
        diagram_y0 = min(diagram_y0, min(s[1] for s in labels_in_band) - 2)
        diagram_y1 = max(diagram_y1, max(s[3] for s in labels_in_band) + 2)
        # Widen x to include labels that sit outside the model's bbox.
        diagram_x0 = min(diagram_x0, min(s[0] for s in labels_in_band) - 6)
        diagram_x1 = max(diagram_x1, max(s[2] for s in labels_in_band) + 6)

    # Clamp to exclude both sidebar zones (left or right "DO NOT WRITE" stripe).
    diagram_x0 = max(diagram_x0, sidebar_left + 5)
    diagram_x1 = min(diagram_x1, sidebar_right - 5)

    debug = {
        "original_bbox": list(rough_bbox),
        "prose_above_count": len(prose_above),
        "prose_below_count": len(prose_below),
        "labels_in_band_count": len(labels_in_band),
        "refined_bbox": [diagram_x0, diagram_y0, diagram_x1, diagram_y1],
    }
    return [diagram_x0, diagram_y0, diagram_x1, diagram_y1], debug


def save_diagram_png(pdf_path: Path, page: int, bbox_pdf: list, out_path: Path,
                     dpi: int = PNG_DPI) -> None:
    """Crop the source page to bbox_pdf and write the region as a 300 DPI PNG.

    PNGs are visually indistinguishable from vector at normal viewing/print zoom and
    avoid PyMuPDF SVG quirks with CID-encoded fonts. Per the project rules raster is
    fine specifically inside diagram regions; text/tables are vector via LaTeX
    rendering elsewhere in the pipeline."""
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        rect = fitz.Rect(*bbox_pdf)
        pix = pg.get_pixmap(clip=rect, dpi=dpi)
        pix.save(str(out_path))
    finally:
        src.close()


# ─── manual annotation helpers ────────────────────────────────────────

AUDIT_DRAWING_THRESHOLD = 8  # pages with >= this many diagram-like drawings, but no
                              # question flagged has_diagram=1, are surfaced as audit
                              # warnings in /admin/diagrams.


def _filter_diagram_drawings(page) -> list:
    """Return the subset of page drawings that look like diagram strokes.

    Same filter the vision-refiner uses: drop chrome, sidebars, working-line rules.
    Useful both for audit-warning detection and (in principle) for future tooling.
    """
    page_w = page.rect.width
    sidebar_left, sidebar_right = _sidebar_x_for(page_w)
    CONTENT_WIDTH = sidebar_right - sidebar_left
    page_area = page_w * page.rect.height
    kept = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None:
            continue
        dw, dh = r.x1 - r.x0, r.y1 - r.y0
        # Chrome (full-page-area borders/fills).
        if dw * dh > 0.5 * page_area:
            continue
        # Sidebar drawings.
        if r.x1 <= sidebar_left or r.x0 >= sidebar_right:
            continue
        # Working-line answer rules: hairline horizontals running across most of content.
        if dh < 1 and dw > 0.6 * CONTENT_WIDTH:
            continue
        kept.append(r)
    return kept


def crop_from_manual_bbox(question_id: str, page: int,
                           bbox_normalised: list) -> dict:
    """Crop a question's source page using a user-drawn normalised bbox.

    bbox_normalised is [x0, y0, x1, y1] in 0..1, top-left origin (canvas convention).
    Writes the cropped PNG to assets/diagrams/<question_id>.png and updates the DB
    (diagram_path + source_bbox with method='manual'). No API call.
    """
    conn = connect()
    try:
        q = conn.execute(
            """
            select q.id, q.source_id, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ?
            """,
            (question_id,),
        ).fetchone()
    finally:
        conn.close()
    if q is None:
        raise RuntimeError(f"question {question_id} not found")

    pdf_path = REPO_ROOT / q["pdf_path"]
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        pw, ph = pg.rect.width, pg.rect.height
    finally:
        src.close()

    x0n, y0n, x1n, y1n = bbox_normalised
    if not (0 <= x0n < x1n <= 1 and 0 <= y0n < y1n <= 1):
        raise RuntimeError(f"invalid bbox_normalised {bbox_normalised}")
    bbox_pdf = [x0n * pw, y0n * ph, x1n * pw, y1n * ph]

    out_path = DIAGRAMS_DIR / f"{question_id}.png"
    save_diagram_png(pdf_path, page, bbox_pdf, out_path)

    rel = str(out_path.relative_to(REPO_ROOT))
    conn = connect()
    try:
        conn.execute(
            "update questions set has_diagram = 1, diagram_path = ?, source_bbox = ? "
            "where id = ?",
            (rel, json.dumps({
                "method": "manual",
                "page": page,
                "bbox_pdf": bbox_pdf,
                "bbox_normalised": bbox_normalised,
            }), question_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"question_id": question_id, "status": "manual", "path": rel,
            "bbox_pdf": bbox_pdf}


def crop_option_from_manual_bbox(question_id: str, option_label: str, page: int,
                                  bbox_normalised: list) -> dict:
    """Crop one MC option panel (A/B/C/D/E) from a user-drawn bbox.

    Same shape as `crop_from_manual_bbox` but writes to <qid>-opt-<letter>.png and
    updates the corresponding entry in `mc_options_md` JSON, setting `path`,
    `bbox` (normalised), `bbox_pdf`, and `bbox_source = 'manual'`. No API call."""
    label = option_label.upper()
    if label not in {"A", "B", "C", "D", "E"}:
        raise RuntimeError(f"invalid option label {option_label!r}")
    idx = ord(label) - ord("A")

    conn = connect()
    try:
        q = conn.execute(
            """
            select q.id, q.mc_options_md, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ? and q.is_mc = 1
            """,
            (question_id,),
        ).fetchone()
    finally:
        conn.close()
    if q is None:
        raise RuntimeError(f"MC question {question_id} not found")
    if not q["mc_options_md"]:
        raise RuntimeError(f"{question_id} has no mc_options_md")
    opts = json.loads(q["mc_options_md"])
    if idx >= len(opts):
        raise RuntimeError(f"{question_id} only has {len(opts)} options; can't set {label}")
    if opts[idx].get("kind") != "diagram":
        raise RuntimeError(f"{question_id} option {label} is not kind=diagram")

    pdf_path = REPO_ROOT / q["pdf_path"]
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        pw, ph = pg.rect.width, pg.rect.height
    finally:
        src.close()

    x0n, y0n, x1n, y1n = bbox_normalised
    if not (0 <= x0n < x1n <= 1 and 0 <= y0n < y1n <= 1):
        raise RuntimeError(f"invalid bbox_normalised {bbox_normalised}")
    bbox_pdf = [x0n * pw, y0n * ph, x1n * pw, y1n * ph]

    out_path = DIAGRAMS_DIR / f"{question_id}-opt-{label}.png"
    save_diagram_png(pdf_path, page, bbox_pdf, out_path)
    rel = str(out_path.relative_to(REPO_ROOT))

    opts[idx]["bbox"] = list(bbox_normalised)
    opts[idx]["bbox_pdf"] = bbox_pdf
    opts[idx]["bbox_source"] = "manual"
    opts[idx]["path"] = rel

    conn = connect()
    try:
        conn.execute(
            "update questions set mc_options_md = ? where id = ?",
            (json.dumps(opts), question_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"question_id": question_id, "option": label, "status": "manual",
            "path": rel, "bbox_pdf": bbox_pdf}


def find_audit_pages(source_id: int) -> list:
    """Pages on this source that have lots of diagram-like drawings but no
    question on the page is flagged has_diagram=1. UI surfaces these so the
    user can decide if a diagram was missed by the extractor.

    Filters out: (a) pages in sources.skipped_pages (formula sheets / cover /
    blank pages), (b) drawings whose centre falls inside a prose block — those
    are math-typesetting strokes (fraction bars, sqrt vinculums, brackets),
    not diagram strokes.

    Returns a list of {page, drawings_count, questions: [...]} dicts.
    """
    conn = connect()
    try:
        row = conn.execute(
            "select id, pdf_path, skipped_pages from sources where id = ?", (source_id,),
        ).fetchone()
        if row is None:
            return []
        qrows = conn.execute(
            """
            select id, question_number, part, source_page_start, source_page_end,
                   has_diagram, prompt_md
            from questions where source_id = ?
            order by source_page_start, question_number, part
            """,
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    pdf_path = REPO_ROOT / row["pdf_path"]
    skipped: set[int] = set()
    if row["skipped_pages"]:
        try:
            skipped = set(json.loads(row["skipped_pages"]))
        except (json.JSONDecodeError, ValueError):
            skipped = set()
    pages_with_flagged: set[int] = set()
    page_questions: dict[int, list[dict]] = {}
    for q in qrows:
        for p in range(q["source_page_start"], q["source_page_end"] + 1):
            page_questions.setdefault(p, []).append(dict(q))
            if q["has_diagram"]:
                pages_with_flagged.add(p)

    audits = []
    doc = fitz.open(pdf_path)
    try:
        for p in range(1, doc.page_count + 1):
            if p in skipped or p in pages_with_flagged:
                continue
            page = doc.load_page(p - 1)
            sidebar_left, sidebar_right = _sidebar_x_for(page.rect.width)
            kept_all = _filter_diagram_drawings(page)
            # Keep only "curve-like" drawings (non-zero area). Excludes pure
            # horizontal/vertical lines, which are mostly table grid lines and
            # math underlines — those inflate counts on pages with embedded tables
            # (2022 P2 Q5's value table on page 24 had ~30 grid line drawings).
            kept = [r for r in kept_all if (r.x1 - r.x0) > 1 and (r.y1 - r.y0) > 1]
            # Reject drawings whose centre is inside a prose block — fraction bars
            # under math typesetting and underlines on bold words are vector strokes
            # but they're typography, not diagram content.
            prose_y_bands: list[tuple[float, float]] = []
            for x0b, y0b, x1b, y1b, text, _bno, _btype in page.get_text("blocks"):
                if x1b <= sidebar_left or x0b >= sidebar_right:
                    continue
                stripped = (text or "").strip().replace("\n", " ")
                if not stripped:
                    continue
                if len(stripped) > LABEL_CHAR_THRESHOLD or (x1b - x0b) > PROSE_SPAN_WIDTH_PT:
                    prose_y_bands.append((y0b, y1b))

            def in_prose(cy: float) -> bool:
                return any(py0 - 1 <= cy <= py1 + 1 for py0, py1 in prose_y_bands)

            real_drawings = [r for r in kept if not in_prose((r.y0 + r.y1) / 2)]
            if len(real_drawings) < AUDIT_DRAWING_THRESHOLD:
                continue
            audits.append({
                "page": p,
                "drawings_count": len(real_drawings),
                "questions": page_questions.get(p, []),
            })
    finally:
        doc.close()
    return audits


# ─── main extract entry point ─────────────────────────────────────────


def extract_one(question_id: str, *, force: bool = False, budget_usd: float = 1.00,
                baseline_usd: float = 0.0) -> dict:
    conn = connect()
    try:
        q = conn.execute(
            """
            select q.*, s.subject as source_subject, s.year, s.paper, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ? and q.has_diagram = 1
            """,
            (question_id,),
        ).fetchone()
    finally:
        conn.close()
    if q is None:
        raise RuntimeError(f"question {question_id} not found or has_diagram=0")
    spec = subject_spec(q["source_subject"])

    # Prefer a manual annotation when one exists — re-crop from the stored bbox,
    # zero API cost. Only fall through to vision if no manual bbox is stored.
    if q["source_bbox"]:
        try:
            sb = json.loads(q["source_bbox"])
        except (json.JSONDecodeError, ValueError):
            sb = None
        if sb and sb.get("method") == "manual" and sb.get("bbox_normalised"):
            return crop_from_manual_bbox(question_id, sb["page"], sb["bbox_normalised"])

    out_path = DIAGRAMS_DIR / f"{question_id}.png"
    if out_path.exists() and not force:
        # PNG already on disk — re-link in DB if missing (happens after a question
        # re-extraction wipes diagram_path but leaves the file).
        if not q["diagram_path"]:
            rel = str(out_path.relative_to(REPO_ROOT))
            conn = connect()
            try:
                conn.execute(
                    "update questions set diagram_path = ? where id = ?",
                    (rel, question_id),
                )
                conn.commit()
            finally:
                conn.close()
        return {"question_id": question_id, "status": "cached", "path": str(out_path)}

    pdf_path = REPO_ROOT / q["pdf_path"]

    # Search each page in the fragment's source range until we find the diagram.
    found: Optional[dict] = None
    found_on_page: Optional[int] = None
    for p in range(q["source_page_start"], q["source_page_end"] + 1):
        check_budget(budget_usd, baseline_usd)
        result = find_diagram_bbox(
            spec, q["source_id"], pdf_path, q["year"], q["paper"], p,
            question_id, q["prompt_md"],
        )
        if result:
            found = result
            found_on_page = p
            break

    if not found:
        return {"question_id": question_id, "status": "not_found", "path": None}

    # MC question + diagram options ⇒ the stem diagram must not spill into the option
    # panels below. Use the topmost A/B/C/D/E label's y as a hard upper bound for the
    # stem diagram's refined bbox.
    y_upper_bound: Optional[float] = None
    if q["is_mc"] and q["mc_options_md"]:
        try:
            opts = json.loads(q["mc_options_md"])
            if any(o.get("kind") == "diagram" for o in opts):
                model_bboxes = [o["bbox"] for o in opts if o.get("kind") == "diagram" and isinstance(o.get("bbox"), list)]
                anchors = _find_option_label_anchors(pdf_path, found_on_page, model_bboxes,
                                                      n_options=len(opts))
                anchor_ys = [a["y_pt"] for a in anchors if a]
                if anchor_ys:
                    y_upper_bound = min(anchor_ys) - 6  # 6pt margin above the top option letter
        except (json.JSONDecodeError, KeyError):
            pass

    refined_bbox, refine_debug = refine_bbox(pdf_path, found_on_page, found["bbox_pdf"],
                                              y_upper_bound=y_upper_bound)
    save_diagram_png(pdf_path, found_on_page, refined_bbox, out_path)

    # Update DB with the relative path + bbox (refined) for traceability.
    rel = str(out_path.relative_to(REPO_ROOT))
    conn = connect()
    try:
        conn.execute(
            "update questions set diagram_path = ?, source_bbox = ? where id = ?",
            (rel, json.dumps({
                "page": found_on_page,
                "bbox_pdf": refined_bbox,
                "bbox_pdf_model": found["bbox_pdf"],
                "refine": refine_debug,
            }), question_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "question_id": question_id,
        "status": "extracted",
        "path": str(out_path),
        "page": found_on_page,
        "description": found["description"],
        "refine": refine_debug,
    }


def _find_option_label_anchors(pdf_path: Path, page: int,
                                model_bboxes: list[list[float]],
                                n_options: int = 5) -> list[Optional[dict]]:
    """For each of the model's normalised bboxes, find the 'A.' / 'B.' etc. text-layer
    anchor that belongs to it. Vision-derived bboxes are systematically off by ~0.05-0.10
    on y; the PDF's text-layer letter positions are exact and let us pin the panel top.

    n_options is 5 for 2016–2023 papers (A–E) and 4 for 2024+ papers (A–D).

    Returns a list of length n_options, one anchor per option:
        {"letter": "A", "x_pt": ..., "y_pt": ..., "bbox_pt": [...]}
    Entries are None for any option whose anchor can't be confidently identified.
    """
    import re as _re
    src = fitz.open(pdf_path)
    try:
        pg = src.load_page(page - 1)
        pw, ph = pg.rect.width, pg.rect.height
        text_dict = pg.get_text("dict")
    finally:
        src.close()

    expected_letters = [chr(ord("A") + i) for i in range(n_options)]
    letter_class = "".join(expected_letters)
    # Collect every isolated single-letter token (e.g. "A.") on this page.
    letter_re = _re.compile(rf"^([{letter_class}])\.$")
    letters: list[dict] = []
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = (span.get("text") or "").strip()
                m = letter_re.match(t)
                if m:
                    bb = span["bbox"]
                    letters.append({"letter": m.group(1),
                                    "x_pt": bb[0], "y_pt": bb[1],
                                    "bbox_pt": list(bb)})

    # Group letters by ID. PyMuPDF iterates blocks in its own order (text-flow heuristic),
    # which on grid-laid-out pages like 2021 P2 page 5 (A,B / C,D / E) emerges as e.g.
    # C, E, D, A, B — so we can't assume document order is A→B→C→D→E.
    by_letter: dict[str, list[dict]] = {L: [] for L in expected_letters}
    for entry in letters:
        by_letter[entry["letter"]].append(entry)
    if any(not by_letter[L] for L in expected_letters):
        return [None] * n_options

    # Build candidate option groups. For each A, the matching B/C/D[/E] is the instance
    # whose y is closest to A's y (within ~250pt — a single MC's option region is at most
    # half a page tall). When each letter appears exactly once, this collapses to the
    # only possible grouping.
    candidate_blocks: list[list[dict]] = []
    for a in by_letter[expected_letters[0]]:
        block = [a]
        for L in expected_letters[1:]:
            same_group = [e for e in by_letter[L] if abs(e["y_pt"] - a["y_pt"]) <= 250]
            if not same_group:
                block = []
                break
            best = min(same_group, key=lambda e: (abs(e["y_pt"] - a["y_pt"]),
                                                   abs(e["x_pt"] - a["x_pt"])))
            block.append(best)
        if len(block) == n_options:
            candidate_blocks.append(block)

    if not candidate_blocks:
        return [None] * n_options

    # Pick the block whose y-centroid is nearest the model's overall option region.
    if model_bboxes:
        model_ys = [(b[1] + b[3]) / 2 for b in model_bboxes if isinstance(b, list) and len(b) == 4]
        if model_ys:
            target_y_norm = sum(model_ys) / len(model_ys)

            def block_y_centroid(blk):
                return sum(e["y_pt"] / ph for e in blk) / len(blk)

            chosen = min(candidate_blocks, key=lambda b: abs(block_y_centroid(b) - target_y_norm))
        else:
            chosen = candidate_blocks[0]
    else:
        chosen = candidate_blocks[0]
    return chosen


def _panel_bboxes_from_anchors(anchors: list[dict], page_w: float, page_h: float) -> list[list[float]]:
    """Given 5 letter-label anchors A..E (with x_pt, y_pt), compute panel bboxes in
    PDF coords. Strategy:
      - Group by column on the x-axis (left column vs right column).
      - For each anchor, panel y_top = letter_y, y_bot = NEXT anchor in the same column,
        OR the next row's anchor minus a small gap (whichever closer), OR page bottom.
      - Panel x extends from letter_x + small offset to either page midpoint (col 1) or
        right margin (col 2)."""
    sidebar_left, sidebar_right = _sidebar_x_for(page_w)
    # Identify column midpoint from the x positions of the letters.
    xs = sorted(a["x_pt"] for a in anchors)
    if len(set(round(x, 0) for x in xs)) > 1:
        col_mid = (xs[0] + xs[-1]) / 2
    else:
        col_mid = page_w / 2

    bboxes: list[list[float]] = []
    by_col: dict[str, list[dict]] = {"L": [], "R": []}
    for a in anchors:
        col = "L" if a["x_pt"] < col_mid else "R"
        by_col[col].append(a)
    for col_list in by_col.values():
        col_list.sort(key=lambda a: a["y_pt"])

    LEFT_X_PAD = 12   # start panel just after the letter label
    BOTTOM_GAP = 4
    TOP_GAP = -2      # include a sliver above the letter for tick marks etc.
    RIGHT_COL_GAP = 8  # gap before the next column's letter starts

    # For each anchor, find the sibling in the OTHER column on the same row (matched by
    # y_pt within a tolerance). That sibling's x_pt - RIGHT_COL_GAP is the panel's right
    # edge for left-column items; for right-column items it's page-right-margin.
    Y_ROW_TOLERANCE = 8

    def _same_row_other_col(anchor: dict) -> Optional[dict]:
        other_col = "R" if anchor["x_pt"] < col_mid else "L"
        for b in by_col[other_col]:
            if abs(b["y_pt"] - anchor["y_pt"]) <= Y_ROW_TOLERANCE:
                return b
        return None

    for a in anchors:
        col = "L" if a["x_pt"] < col_mid else "R"
        col_list = by_col[col]
        idx = col_list.index(a)
        y_top = a["y_pt"] + TOP_GAP
        # Bottom = next anchor in same column - BOTTOM_GAP, OR — if no next in column —
        # use the next row anchor in the OTHER column to know where this row's panels end.
        if idx + 1 < len(col_list):
            y_bot = col_list[idx + 1]["y_pt"] - BOTTOM_GAP
        else:
            # Try the next row by looking across columns.
            other_col = "R" if col == "L" else "L"
            below = [b for b in by_col[other_col] if b["y_pt"] > a["y_pt"]]
            if below:
                y_bot = min(b["y_pt"] for b in below) - BOTTOM_GAP
            else:
                # Last in its column with no row below. Estimate height = max of previous panel heights.
                if idx > 0:
                    prev_height = col_list[idx]["y_pt"] - col_list[idx - 1]["y_pt"]
                    y_bot = a["y_pt"] + prev_height - BOTTOM_GAP
                else:
                    y_bot = min(a["y_pt"] + 160, page_h - 20)
        x_left = a["x_pt"] + LEFT_X_PAD
        # Right edge: just before the other-column sibling's letter for left-column items;
        # right margin (clear of sidebar) for right-column items.
        if col == "L":
            sibling = _same_row_other_col(a)
            x_right = sibling["x_pt"] - RIGHT_COL_GAP if sibling else (page_w / 2 - 4)
        else:
            x_right = sidebar_right - 5
        bboxes.append([x_left, y_top, x_right, y_bot])
    return bboxes


def extract_mc_option_diagrams(question_id: str, *, force: bool = False) -> dict:
    """For an MC question with diagram options (kind='diagram' in mc_options_md),
    crop each option's region from the source page and save as <qid>-opt-<A|B|...>.png.

    Uses bboxes the question-extraction step already produced (normalised page coords)
    PLUS the text-layer positions of the 'A.' / 'B.' etc. labels — which are precise
    and let us correct for the vision-derived bbox's systematic y-drift.
    Makes ZERO API calls.

    Updates mc_options_md JSON with each diagram option's 'path'."""
    conn = connect()
    try:
        q = conn.execute(
            """
            select q.id, q.mc_options_md, q.source_page_start, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ? and q.is_mc = 1
            """,
            (question_id,),
        ).fetchone()
    finally:
        conn.close()
    if q is None:
        raise RuntimeError(f"MC question {question_id} not found")
    if not q["mc_options_md"]:
        return {"question_id": question_id, "status": "no_options", "options": []}

    opts = json.loads(q["mc_options_md"])
    pdf_path = REPO_ROOT / q["pdf_path"]
    page = q["source_page_start"]

    doc = fitz.open(pdf_path)
    try:
        pw, ph = doc[0].rect.width, doc[0].rect.height
    finally:
        doc.close()

    # Try to anchor each option's panel via the PDF text-layer A./B./... positions.
    model_bboxes = [o["bbox"] for o in opts if o.get("kind") == "diagram" and isinstance(o.get("bbox"), list)]
    anchors = _find_option_label_anchors(pdf_path, page, model_bboxes, n_options=len(opts))
    anchor_bboxes_pdf: Optional[list[list[float]]] = None
    if anchors and all(a is not None for a in anchors) and len(anchors) == len(opts):
        anchor_bboxes_pdf = _panel_bboxes_from_anchors(anchors, pw, ph)

    results = []
    changed = False
    for i, o in enumerate(opts):
        label = chr(ord("A") + i)
        if o.get("kind") != "diagram":
            results.append({"label": label, "status": "skipped_text"})
            continue
        out_path = DIAGRAMS_DIR / f"{question_id}-opt-{label}.png"
        if out_path.exists() and not force and o.get("path"):
            results.append({"label": label, "status": "cached", "path": o["path"]})
            continue

        # Prefer the anchor-derived bbox (precise) over the model's (drifts by ~0.1 on y).
        if anchor_bboxes_pdf:
            bbox_pdf = anchor_bboxes_pdf[i]
            source = "anchored"
        else:
            bb = o.get("bbox")
            if not (isinstance(bb, list) and len(bb) == 4):
                results.append({"label": label, "status": "missing_bbox"})
                continue
            x0n, y0n, x1n, y1n = bb
            x0n = max(0.0, x0n - BBOX_PADDING)
            y0n = max(0.0, y0n - BBOX_PADDING)
            x1n = min(1.0, x1n + BBOX_PADDING)
            y1n = min(1.0, y1n + BBOX_PADDING)
            bbox_pdf = [x0n * pw, y0n * ph, x1n * pw, y1n * ph]
            source = "model_normalised"
        save_diagram_png(pdf_path, page, bbox_pdf, out_path)
        rel = str(out_path.relative_to(REPO_ROOT))
        o["path"] = rel
        o["bbox_pdf"] = bbox_pdf
        o["bbox_source"] = source
        changed = True
        results.append({"label": label, "status": "extracted", "path": rel, "source": source})

    if changed:
        conn = connect()
        try:
            conn.execute(
                "update questions set mc_options_md = ? where id = ?",
                (json.dumps(opts), question_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"question_id": question_id, "status": "ok", "options": results}


def extract_paper_diagrams(subject: str, year: int, paper: int, *, force: bool = False,
                           budget_usd: float = 1.00) -> dict:
    from pipeline.spend import total_spend
    baseline_usd = total_spend()

    # Validate that the subject is known; spec is unused here (extract_one looks it up
    # from the question's source row) but the validation gives a clearer early error.
    subject_spec(subject)
    conn = connect()
    try:
        rows = conn.execute(
            """
            select q.id from questions q
            join sources s on s.id = q.source_id
            where s.subject = ? and s.year = ? and s.paper = ? and q.has_diagram = 1
            order by q.question_number, q.part
            """,
            (subject, year, paper),
        ).fetchall()
        # MC questions whose options include diagrams. Detected by inspecting the
        # stored mc_options_md JSON for any entry with kind='diagram'.
        mc_rows = conn.execute(
            """
            select q.id, q.mc_options_md from questions q
            join sources s on s.id = q.source_id
            where s.subject = ? and s.year = ? and s.paper = ? and q.is_mc = 1
              and q.mc_options_md like '%"kind": "diagram"%'
            order by q.question_number
            """,
            (subject, year, paper),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        try:
            res = extract_one(r["id"], force=force, budget_usd=budget_usd, baseline_usd=baseline_usd)
        except BudgetExceeded:
            raise
        results.append(res)
        sys.stderr.write(f"  {res['question_id']}: {res['status']}"
                         + (f" (p{res.get('page')})" if res.get('page') else '')
                         + "\n")

    mc_results = []
    for r in mc_rows:
        res = extract_mc_option_diagrams(r["id"], force=force)
        mc_results.append(res)
        n_extracted = sum(1 for o in res["options"] if o["status"] == "extracted")
        n_cached = sum(1 for o in res["options"] if o["status"] == "cached")
        sys.stderr.write(f"  {r['id']}: {n_extracted} option diagrams extracted, {n_cached} cached\n")

    return {
        "subject": subject, "year": year, "paper": paper,
        "diagrams_total": len(rows),
        "mc_with_diagram_options": len(mc_rows),
        "results": results,
        "mc_option_results": mc_results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--year", type=int, help="year (must be paired with --paper)")
    g.add_argument("--question", type=str, help="single question id, e.g. 2023-p1-q3-a or sp-2023-p1-q3-a")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT, choices=sorted(SUBJECTS.keys()))
    ap.add_argument("--paper", type=int, choices=[1, 2])
    ap.add_argument("--force", action="store_true", help="re-extract even if SVG exists")
    ap.add_argument("--budget", type=float, default=1.00)
    args = ap.parse_args()

    try:
        if args.question:
            # subject is derived from the question's source row in extract_one
            print(json.dumps(extract_one(args.question, force=args.force, budget_usd=args.budget), indent=2))
        else:
            if args.paper is None:
                ap.error("--year requires --paper")
            print(json.dumps(extract_paper_diagrams(args.subject, args.year, args.paper, force=args.force, budget_usd=args.budget), indent=2))
    except BudgetExceeded as e:
        print(f"BUDGET EXCEEDED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
