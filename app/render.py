"""Practice-paper PDF rendering.

Pipeline: query DB → group questions → render Jinja HTML with KaTeX → Playwright
prints to PDF. Zero API cost.

The HTML template lives at app/templates/practice_paper.html. Diagrams embed as
<img src="/diagrams/...png"> and answer images as <img src="/answers/...png">,
served from the same FastAPI instance, so Playwright must navigate to a real URL
(not a data URI) for those to resolve. We use a small in-process HTTP server-less
path by writing the HTML to a temp file alongside symlinks to the assets dirs;
simpler: just point Playwright at the live FastAPI URL during generation.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from playwright.sync_api import sync_playwright

from pipeline.db import REPO_ROOT, connect

# Markdown renderer for prompt_md → HTML. Math regions are stashed before processing
# so markdown-it doesn't mangle them (e.g. interpret `_` inside `$x_1$` as italics).
# KaTeX auto-render in the browser handles the math after Playwright loads the page.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": False}).enable("table")
_MATH_PATTERNS = [
    re.compile(r"\$\$.+?\$\$", re.DOTALL),
    re.compile(r"\\\[.+?\\\]", re.DOTALL),
    re.compile(r"\\\(.+?\\\)", re.DOTALL),
    re.compile(r"\$[^$\n]+?\$"),
]


# Patterns indicating examiner-report prose lost its inline math (MathType OLE objects
# that python-docx drops when extracting text). When any of these match, the prose is
# unsafe to render — it'll have phrases like "the domain of p is and the domain of q is ."
# that look broken to students. We suppress such commentary entirely.
_MATH_GAP_PATTERNS = [
    re.compile(r"\s{2,}"),                     # 2+ consecutive spaces (where math used to be)
    re.compile(r"\bis\s+[.,]"),                 # "... is ." or "... is ,"
    re.compile(r"\bas\s+[.,]"),                 # "... as ." / "... as ,"
    re.compile(r"\bof\s+[.,]"),                 # "... of ." / "... of ,"
    re.compile(r"^[,;]\s"),                     # commentary starts with stray comma/semicolon
    re.compile(r"\bwhere\s+[.,]"),              # "where  ." pattern (math placeholder)
]


_FRAGMENTARY_PREFIX = re.compile(r"^\s*(and|or|but|so|then|where|thus|hence)\b\s*[.,]?\s*$", re.IGNORECASE)
_FRAGMENTARY_LEAD = re.compile(r"^\s*(and|or|but|so|then|thus|hence)\b\s+", re.IGNORECASE)


def _has_math_gaps(text: Optional[str]) -> bool:
    """Return True if the prose looks like math was dropped from it."""
    if not text or text == "(no commentary)":
        return False
    stripped = text.strip()
    # Pure orphaned conjunctions like "and" or "or" with no other content.
    if _FRAGMENTARY_PREFIX.match(stripped):
        return True
    # Very short fragments are almost always incomplete (real commentary >= 25 chars).
    if len(stripped) < 25:
        return True
    # Commentary that LEADS with a conjunction was probably preceded by dropped math.
    if _FRAGMENTARY_LEAD.match(stripped):
        return True
    return any(p.search(text) for p in _MATH_GAP_PATTERNS)


def _parse_mc_answer(s: Optional[str]) -> Optional[dict]:
    """Return the parsed MC answer dict if `s` is a JSON mc_answer blob, else None."""
    if not s or not s.lstrip().startswith("{"):
        return None
    try:
        d = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(d, dict) and d.get("type") == "mc_answer":
        return d
    return None


def _render_md(text: Optional[str]) -> str:
    if not text:
        return ""
    chunks: list[str] = []

    def stash(m):
        chunks.append(m.group(0))
        return f"@@MATH{len(chunks) - 1}@@"

    processed = text
    for pat in _MATH_PATTERNS:
        processed = pat.sub(stash, processed)
    html = _MD.render(processed)
    for i, chunk in enumerate(chunks):
        html = html.replace(f"@@MATH{i}@@", chunk)
    return html

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class QuestionGroup:
    """A top-level question with its stem (if any), mid-question sub-stems, and ordered sub-parts.

    sub_stems_by_part maps a part label (e.g. 'c', 'e', 'h') to the sub-stem row that should
    render immediately above that part. A Section B question with 3 sub-stems (Q4 of 2023 P2
    tennis-balls is the canonical example) will have 3 entries here.
    """
    source_year: int
    source_paper: int
    question_number: int
    section: Optional[str]
    stem: Optional[dict]                    # row with part=None
    sub_stems_by_part: dict[str, dict]       # 'c' -> sub_stem row
    parts: list[dict]                       # rows ordered by part (excludes sub_stems)
    total_marks: Optional[int]

    @property
    def header(self) -> str:
        if self.total_marks is None:
            return f"Question {self.question_number}"
        return f"Question {self.question_number} ({self.total_marks} mark{'' if self.total_marks==1 else 's'})"


def _part_sort_key(part: Optional[str]) -> tuple:
    if part is None:
        return (0,)
    # 'a' → (1, 'a'); 'b.i' → (1, 'b', 'i')
    return (1,) + tuple(part.split("."))


def load_groups(question_ids: list[str]) -> list[QuestionGroup]:
    """Resolve a flat list of question_ids into ordered QuestionGroup objects.

    For each (source, question_number) in the input list, also pulls the sibling
    stem (part=null) and ALL leaf siblings — selecting just one part of a
    multi-part question still includes its sibling parts so the stem makes sense
    in context."""
    if not question_ids:
        return []
    conn = connect()
    try:
        # Step 1: resolve to distinct (source_id, section, question_number) groups.
        # Section is part of question identity — Section A Q1 and Section B Q1 of the
        # same paper are different questions that happen to share question_number.
        placeholders = ",".join("?" * len(question_ids))
        meta = conn.execute(
            f"""
            select distinct q.source_id, q.section, q.question_number, s.year, s.paper
            from questions q join sources s on s.id = q.source_id
            where q.id in ({placeholders})
            order by s.year, s.paper,
                     case when q.section = 'A' then 0 when q.section = 'B' then 1 else 2 end,
                     q.question_number
            """,
            question_ids,
        ).fetchall()

        groups: list[QuestionGroup] = []
        for m in meta:
            rows = conn.execute(
                """
                select q.*, s.year, s.paper
                from questions q join sources s on s.id = q.source_id
                where q.source_id = ? and q.section is ? and q.question_number = ?
                """,
                (m["source_id"], m["section"], m["question_number"]),
            ).fetchall()
            rows = [dict(r) for r in rows]
            stem = next((r for r in rows if r["part"] is None and any(o["part"] is not None for o in rows)), None)
            sub_stems_by_part: dict[str, dict] = {}
            parts: list[dict] = []
            for r in rows:
                if r is stem:
                    continue
                p = r["part"]
                if p and p.startswith("pre-"):
                    sub_stems_by_part[p[4:]] = r
                else:
                    parts.append(r)
            parts.sort(key=lambda r: _part_sort_key(r["part"]))
            total = sum((p["marks"] or 0) for p in parts) if parts else None
            groups.append(QuestionGroup(
                source_year=m["year"],
                source_paper=m["paper"],
                question_number=m["question_number"],
                section=parts[0].get("section") if parts else None,
                stem=stem,
                sub_stems_by_part=sub_stems_by_part,
                parts=parts,
                total_marks=total,
            ))
        return groups
    finally:
        conn.close()


def load_answers(question_ids: list[str]) -> dict[str, dict]:
    if not question_ids:
        return {}
    conn = connect()
    try:
        placeholders = ",".join("?" * len(question_ids))
        rows = conn.execute(
            f"select * from answers where question_id in ({placeholders})",
            question_ids,
        ).fetchall()
        return {r["question_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def _basename(path: Optional[str]) -> str:
    return Path(path).name if path else ""


def _abs_asset(path: Optional[str]) -> str:
    """Return an absolute file:// URL for an asset. Playwright needs absolute paths."""
    if not path:
        return ""
    return (REPO_ROOT / path).resolve().as_uri()


def _section_breakdown(groups: list[QuestionGroup]) -> list[dict]:
    """Aggregate groups into contents rows for the cover: one per section with question + mark counts."""
    by_section: dict[Optional[str], list[QuestionGroup]] = {}
    for g in groups:
        by_section.setdefault(g.section, []).append(g)
    # Section A before B before unsectioned (Paper 1).
    order = [s for s in ("A", "B", None) if s in by_section]
    out: list[dict] = []
    for sec in order:
        gs = by_section[sec]
        out.append({
            "section": sec,
            "label": (
                "Section A — Multiple-choice questions" if sec == "A"
                else "Section B — Short and extended response" if sec == "B"
                else "Questions"
            ),
            "n_questions": len(gs),
            "marks": sum((g.total_marks or 0) for g in gs),
        })
    return out


def _source_summary(groups: list[QuestionGroup]) -> str:
    """Human-readable list of distinct (year, paper) sources, e.g. '2023 Paper 1 + 2024 Paper 2'."""
    seen = []
    for g in groups:
        key = (g.source_year, g.source_paper)
        if key not in seen:
            seen.append(key)
    return " · ".join(f"{y} Paper {p}" for y, p in seen) if seen else ""


def render_html(groups: list[QuestionGroup], answers: dict[str, dict],
                *, title: str, subtitle: str, filters_summary: str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["abs_asset"] = _abs_asset
    env.filters["basename"] = _basename
    env.filters["from_json"] = lambda s: json.loads(s) if s else []
    env.filters["md"] = _render_md
    env.filters["has_math_gaps"] = _has_math_gaps
    env.filters["mc_answer"] = _parse_mc_answer
    tmpl = env.get_template("practice_paper.html")
    total_marks = sum((g.total_marks or 0) for g in groups)
    return tmpl.render(
        title=title,
        subtitle=subtitle,
        filters_summary=filters_summary,
        groups=groups,
        answers=answers,
        total_marks=total_marks,
        section_breakdown=_section_breakdown(groups),
        source_summary=_source_summary(groups),
        generated_at=datetime.now().strftime("%-d %B %Y"),
    )


def html_to_pdf_bytes(html: str) -> bytes:
    """Render html → A4 PDF using headless Chromium. KaTeX auto-render runs in the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            f.write(html.encode("utf-8"))
            html_path = Path(f.name)
        try:
            page.goto(html_path.as_uri(), wait_until="networkidle")
            # Best-effort wait for KaTeX auto-render to finish (it's async).
            try:
                page.wait_for_function("window.__katexReady === true", timeout=8000)
            except Exception:
                pass
            pdf_bytes = page.pdf(
                format="A4",
                margin={"top": "16mm", "bottom": "16mm", "left": "16mm", "right": "14mm"},
                print_background=True,
            )
        finally:
            try:
                html_path.unlink()
            except FileNotFoundError:
                pass
            browser.close()
    return pdf_bytes


def generate_pdf(question_ids: list[str], *, title: str, subtitle: str,
                 filters_summary: str) -> bytes:
    groups = load_groups(question_ids)
    qids_in_groups: list[str] = []
    for g in groups:
        if g.stem:
            qids_in_groups.append(g.stem["id"])
        for sub in g.sub_stems_by_part.values():
            qids_in_groups.append(sub["id"])
        qids_in_groups.extend(p["id"] for p in g.parts)
    answers = load_answers(qids_in_groups)
    html = render_html(groups, answers,
                       title=title, subtitle=subtitle, filters_summary=filters_summary)
    return html_to_pdf_bytes(html)
