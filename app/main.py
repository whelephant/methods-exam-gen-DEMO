"""FastAPI server for VCE Hub.

Public (always on):
  GET  /                              generator form
  POST /generate.pdf                  generate a practice paper from form input
  GET  /generate.pdf                  same, via legacy query params (year/paper/aos/dot)
  GET  /diagrams/{filename}           serve an extracted diagram (PNG)
  GET  /answers/{filename}            serve an examiner-report answer image
  GET  /mc-row-thumbs/{filename}      serve a MC answer-table row thumbnail

Admin (only registered when ADMIN=1 env var is set — local authoring tool):
  GET  /admin/sources, /admin/questions, /admin/question/{qid}, /admin/review
  GET  /admin/mc-answers/{sid}, POST /admin/mc-answers/save
  GET  /admin/diagrams/{sid}, /admin/diagrams/{sid}/q/{qid}[/opt/{label}]
  POST /admin/diagrams/save, /admin/diagrams/save_option, /diagrams/dismiss, /diagrams/flag
  GET  /source/{sid}/page/{p}.png     render a source PDF page as PNG (admin audit)
"""
from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import fitz  # pymupdf
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline.db import REPO_ROOT, connect

ADMIN_ENABLED = os.environ.get("ADMIN") == "1"

app = FastAPI(title="VCE Hub")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DIAGRAMS_DIR = REPO_ROOT / "assets" / "diagrams"
ANSWERS_DIR = REPO_ROOT / "assets" / "answers"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["from_json"] = lambda s: json.loads(s) if s else []
templates.env.filters["basename"] = lambda s: Path(s).name if s else ""
templates.env.globals["admin_enabled"] = ADMIN_ENABLED
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ───── helpers ────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ───── routes ────────────────────────────────────────────────────────

@app.get("/generate.pdf")
def generate_pdf_route(
    year: Optional[int] = Query(None),
    paper: Optional[int] = Query(None),
    aos: Optional[list[int]] = Query(None, description="One or more AoS numbers — repeat the param for multiple, e.g. ?aos=1&aos=2"),
    dot: Optional[int] = Query(None, description="dot_point_sort_order; requires aos (single value)"),
) -> Response:
    """Build a practice-paper PDF from the question DB.

    Selection:
      - year + paper → all leaf questions from that source
      - aos[+dot] → all questions tagged with that AoS (and optionally that dot point);
                    aos accepts multiple values (?aos=1&aos=2 → questions tagged AoS 1 OR 2)
      - year + paper + aos[+dot] → intersection
    """
    from app.render import generate_pdf  # local import to avoid Playwright at app boot

    where = ["1 = 1"]
    params: list = []
    if year is not None:
        where.append("s.year = ?")
        params.append(year)
    if paper is not None:
        where.append("s.paper = ?")
        params.append(paper)
    if aos:
        if dot is not None and len(aos) == 1:
            where.append("exists (select 1 from question_tags t where t.question_id = q.id and t.aos = ? and t.dot_point_sort_order = ?)")
            params.extend([aos[0], dot])
        else:
            placeholders = ",".join("?" * len(aos))
            where.append(f"exists (select 1 from question_tags t where t.question_id = q.id and t.aos in ({placeholders}))")
            params.extend(aos)

    # Restrict to leaf rows (real parts, or parentless wholes). Exclude sub_stems
    # (part like 'pre-%') — they're contextual prose, not questions. Section-aware
    # so Section A whole MCs don't get classified as Section B stems.
    where.append(
        "((q.part is not null and q.part not like 'pre-%') or not exists ("
        " select 1 from questions q2 where q2.source_id = q.source_id "
        " and q2.section is q.section"
        " and q2.question_number = q.question_number"
        " and q2.part is not null and q2.part not like 'pre-%'))"
    )
    sql = (
        "select q.id from questions q join sources s on s.id = q.source_id "
        f"where {' and '.join(where)} "
        "order by s.year, s.paper, "
        " case when q.section = 'A' then 0 when q.section = 'B' then 1 else 2 end,"
        " q.question_number, q.part"
    )
    with _db() as conn:
        qids = [r["id"] for r in conn.execute(sql, params).fetchall()]
    if not qids:
        raise HTTPException(status_code=404, detail="no questions match this filter")

    # Human-readable filter description for the cover. Skip year+paper when set —
    # the cover's subtitle already names the source paper ("drawn from 2023 Paper 2"),
    # so repeating it under "Selection" reads as filler.
    filter_bits: list[str] = []
    if aos:
        # Look up AoS titles for readable labels (e.g. "AoS 1 (Functions, relations and graphs)").
        with _db() as conn:
            ph = ",".join("?" * len(aos))
            rows = conn.execute(
                f"select aos, title from study_areas where aos in ({ph}) order by aos",
                aos,
            ).fetchall()
        aos_labels = [f"AoS {r['aos']} ({r['title']})" for r in rows]
        if len(aos_labels) == 1 and dot is not None:
            filter_bits.append(f"{aos_labels[0]}, dot point {dot}")
        elif len(aos_labels) > 1:
            filter_bits.append("Questions tagged with " + " or ".join(aos_labels))
        else:
            filter_bits.append(aos_labels[0] if aos_labels else f"AoS {aos[0]}")
    if not filter_bits:
        if year is not None and paper is not None:
            filters_summary = f"All questions from {year} Paper {paper}"
        elif year is not None:
            filters_summary = f"All questions from {year}"
        elif paper is not None:
            filters_summary = f"All Paper {paper} questions across years"
        else:
            filters_summary = "All questions in the corpus"
    else:
        filters_summary = ", ".join(filter_bits)

    # Count top-level questions (distinct source_id + question_number) for the subtitle,
    # not leaf parts, so "9 questions" matches the meta block on the cover.
    with _db() as conn:
        placeholders = ",".join("?" * len(qids))
        n_top = conn.execute(
            f"select count(distinct source_id || '/' || coalesce(section,'') || '/' || question_number)"
            f" from questions where id in ({placeholders})",
            qids,
        ).fetchone()[0]
    title = "VCE Mathematical Methods — practice paper"
    subtitle = f"{n_top} question{'s' if n_top != 1 else ''} from real past VCAA exams ({len(qids)} sub-parts)"

    pdf_bytes = generate_pdf(qids, title=title, subtitle=subtitle,
                             filters_summary=filters_summary)
    fname_bits = []
    if year and paper: fname_bits.append(f"{year}_p{paper}")
    if aos:
        aos_part = "aos" + "-".join(str(a) for a in aos)
        if dot is not None and len(aos) == 1:
            aos_part += f"-dot{dot}"
        fname_bits.append(aos_part)
    fname = "methods_practice_" + ("_".join(fname_bits) or "all") + ".pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Generator form. Loads the AoS / dot-point taxonomy and basic corpus stats."""
    with _db() as conn:
        areas_rows = conn.execute(
            """
            select aos, title from study_areas
            where subject = 'mathematical_methods' order by aos
            """
        ).fetchall()
        points_rows = conn.execute(
            """
            select aos, sort_order, text from study_points
            where subject = 'mathematical_methods' and is_header = 0
            order by aos, sort_order
            """
        ).fetchall()
        stats = {
            "sources": conn.execute("select count(*) from sources").fetchone()[0],
            "questions": conn.execute("select count(*) from questions").fetchone()[0],
            "tagged":    conn.execute(
                "select count(distinct question_id) from question_tags"
            ).fetchone()[0],
            "answered":  conn.execute("select count(*) from answers").fetchone()[0],
        }
    by_aos: dict[int, list] = {}
    for p in points_rows:
        by_aos.setdefault(p["aos"], []).append({"sort_order": p["sort_order"], "text": p["text"]})
    areas = [{"aos": a["aos"], "title": a["title"], "points": by_aos.get(a["aos"], [])}
             for a in areas_rows]
    return templates.TemplateResponse(
        request, "index.html", {"areas": areas, "stats": stats},
    )


@app.post("/generate.pdf")
def generate_pdf_form(
    dot: list[str] = Form(default_factory=list),
    paper_type: str = Form(...),
    duration_minutes: int = Form(...),
    seed: Optional[int] = Form(None),
) -> Response:
    """Build a practice-paper PDF from the user-facing form.

    Body (form-encoded):
      dot[]: ["3.8", "4.5", ...]   — chosen dot points (aos.sort_order)
      paper_type: "tech_free" | "mc" | "tech_active"
      duration_minutes: int        — target marks ≈ duration / 1.5
      seed: int (optional)         — for reproducibility tests
    """
    from app.render import generate_pdf  # local import to defer Playwright at boot
    from app.select import (PAPER_TYPE_LABEL, dot_point_labels,
                             pick_question_ids)

    if paper_type not in PAPER_TYPE_LABEL:
        raise HTTPException(status_code=400,
                             detail=f"paper_type must be one of {list(PAPER_TYPE_LABEL)}")
    if duration_minutes <= 0:
        raise HTTPException(status_code=400, detail="duration_minutes must be positive")

    chosen_dots: list[tuple[int, int]] = []
    for d in dot:
        try:
            a, s = d.split(".", 1)
            chosen_dots.append((int(a), int(s)))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400,
                                 detail=f"bad dot point format: {d!r} (expected 'aos.sort_order')")
    if not chosen_dots:
        raise HTTPException(status_code=400,
                             detail="Pick at least one dot point.")

    target_marks = round(duration_minutes / 1.5)
    qids, diag = pick_question_ids(chosen_dots, paper_type, target_marks, seed=seed)
    if not qids:
        raise HTTPException(status_code=400,
                             detail=diag.get("warning") or "No questions match this combination.")

    paper_label = PAPER_TYPE_LABEL[paper_type]
    summary = dot_point_labels(chosen_dots)
    if diag.get("warning"):
        summary = f"{summary} — {diag['warning']}"

    title = "VCE Mathematical Methods — practice paper"
    achieved = diag.get("achieved_marks", target_marks)
    subtitle = f"{diag['n_units']} questions · {achieved} marks · {paper_label.lower()}"

    pdf_bytes = generate_pdf(
        qids,
        title=title,
        subtitle=subtitle,
        filters_summary=summary,
        paper_type_label=paper_label,
        duration_minutes=duration_minutes,
    )

    fname = f"vce_methods_{paper_type}_{duration_minutes}min.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ───── admin routes (registered only when ADMIN=1) ──────────────────
# Public deploys (Fly.io) leave ADMIN unset, so the entire authoring tool is
# absent from the public URL. Local dev runs `ADMIN=1 uvicorn app.main:app`.

def _admin_guard():
    if not ADMIN_ENABLED:
        raise HTTPException(status_code=404)


@app.get("/admin/sources", response_class=HTMLResponse)
def admin_sources(request: Request) -> HTMLResponse:
    _admin_guard()
    with _db() as conn:
        sources = conn.execute(
            """
            select s.*,
                   (select count(*) from questions q where q.source_id = s.id) as n_questions
            from sources s
            order by s.year, s.paper
            """
        ).fetchall()
    return templates.TemplateResponse(
        request, "admin_sources.html", {"sources": sources},
    )


@app.get("/admin/questions", response_class=HTMLResponse)
def admin_questions(
    request: Request,
    aos: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    paper: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> HTMLResponse:
    _admin_guard()
    where = ["1=1"]
    params: list = []
    if year is not None:
        where.append("s.year = ?")
        params.append(year)
    if paper is not None:
        where.append("s.paper = ?")
        params.append(paper)
    if aos is not None:
        where.append("exists (select 1 from question_tags t where t.question_id = q.id and t.aos = ?)")
        params.append(aos)
    where_sql = " and ".join(where)

    with _db() as conn:
        total = conn.execute(
            f"select count(*) from questions q join sources s on s.id = q.source_id where {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            select q.*, s.year, s.paper, s.format_era,
                   (select group_concat(aos || '.' || dot_point_sort_order || case when is_primary then '*' else '' end, ', ')
                    from question_tags t where t.question_id = q.id) as tags
            from questions q
            join sources s on s.id = q.source_id
            where {where_sql}
            order by s.year desc, s.paper, q.question_number, q.part
            limit ? offset ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "admin_questions.html",
        {
            "questions": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "filters": {"aos": aos, "year": year, "paper": paper},
        },
    )


@app.get("/admin/question/{qid}", response_class=HTMLResponse)
def admin_question(request: Request, qid: str) -> HTMLResponse:
    _admin_guard()
    with _db() as conn:
        q = conn.execute(
            """
            select q.*, s.year, s.paper, s.pdf_path, s.format_era
            from questions q join sources s on s.id = q.source_id
            where q.id = ?
            """,
            (qid,),
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail=f"question {qid} not found")
        tags = conn.execute(
            """
            select t.*, sp.text as dot_point_text
            from question_tags t
            join study_points sp
              on sp.subject = t.subject and sp.aos = t.aos and sp.sort_order = t.dot_point_sort_order
            where t.question_id = ?
            order by t.is_primary desc, t.aos, t.dot_point_sort_order
            """,
            (qid,),
        ).fetchall()
        answer = conn.execute(
            "select * from answers where question_id = ?", (qid,)
        ).fetchone()
        override = conn.execute(
            "select * from question_overrides where question_id = ?", (qid,)
        ).fetchone()

        # Within-paper prev/next so the audit view can be clicked through linearly.
        # Order matches the audit list: (question_number, part) with NULL part sorting first.
        siblings = conn.execute(
            """
            select id from questions
            where source_id = ?
            order by question_number,
                     case when part is null then 0 else 1 end,
                     part
            """,
            (q["source_id"],),
        ).fetchall()
        sibling_ids = [r["id"] for r in siblings]
        idx = sibling_ids.index(qid) if qid in sibling_ids else -1
        prev_qid = sibling_ids[idx - 1] if idx > 0 else None
        next_qid = sibling_ids[idx + 1] if 0 <= idx < len(sibling_ids) - 1 else None

    return templates.TemplateResponse(
        request,
        "admin_question.html",
        {
            "q": q,
            "tags": tags,
            "answer": answer,
            "override": override,
            "bbox": json.loads(q["source_bbox"]) if q["source_bbox"] else None,
            "prev_qid": prev_qid,
            "next_qid": next_qid,
            "position": (idx + 1, len(sibling_ids)) if idx >= 0 else None,
        },
    )


@app.get("/admin/review", response_class=HTMLResponse)
def admin_review(request: Request) -> HTMLResponse:
    _admin_guard()
    with _db() as conn:
        rows = conn.execute(
            """
            select r.*, s.year, s.paper
            from review_queue r
            left join sources s on s.id = r.source_id
            where r.resolved = 0
            order by r.created_at desc
            """
        ).fetchall()
    return templates.TemplateResponse(
        request, "admin_review.html", {"items": rows},
    )


@app.get("/source/{sid}/page/{p}.png")
def source_page_png(sid: int, p: int, dpi: int = Query(150, ge=72, le=300)) -> Response:
    _admin_guard()
    with _db() as conn:
        row = conn.execute("select pdf_path from sources where id = ?", (sid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    pdf_path = REPO_ROOT / row["pdf_path"]
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"pdf missing on disk: {pdf_path}")
    doc = fitz.open(pdf_path)
    try:
        if p < 1 or p > doc.page_count:
            raise HTTPException(status_code=404, detail=f"page {p} out of range")
        page = doc.load_page(p - 1)
        pix = page.get_pixmap(dpi=dpi)
        buf = io.BytesIO(pix.tobytes("png"))
    finally:
        doc.close()
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/diagrams/{filename}")
def diagram(filename: str) -> Response:
    # safe path resolution
    target = (DIAGRAMS_DIR / filename).resolve()
    if not str(target).startswith(str(DIAGRAMS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="bad path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="diagram not found")
    # Let FastAPI infer from extension; PNG/SVG both work.
    return FileResponse(target)


@app.get("/answers/{filename}")
def answer_image(filename: str) -> Response:
    target = (ANSWERS_DIR / filename).resolve()
    if not str(target).startswith(str(ANSWERS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="bad path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="answer image not found")
    return FileResponse(target)


@app.get("/mc-row-thumbs/{filename}")
def mc_row_thumb(filename: str) -> Response:
    _admin_guard()
    from pipeline.extract_answers import MC_ROW_THUMBS_DIR
    target = (MC_ROW_THUMBS_DIR / filename).resolve()
    if not str(target).startswith(str(MC_ROW_THUMBS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="bad path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="thumbnail not found")
    return FileResponse(target)


# ───── manual MC-answer verification ─────────────────────────────────


@app.get("/admin/mc-answers/{sid}", response_class=HTMLResponse)
def admin_mc_answers(sid: int, request: Request) -> HTMLResponse:
    """Chip-based audit: each Section A MC shows the % distribution, the current
    `mc_correct` highlighted, and a thumbnail of the row from the examiner-report
    PDF so the user can verify the shaded cell by eye.
    """
    _admin_guard()
    from pipeline.extract_answers import crop_mc_answer_table_rows
    with _db() as conn:
        source = conn.execute(
            "select id, year, paper, report_path from sources where id = ?", (sid,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        if source["paper"] != 2:
            raise HTTPException(status_code=400, detail="only Paper 2 has MCs")
        rows = conn.execute(
            """
            select q.id, q.question_number, q.mc_correct,
                   (select final_answer_md from answers a where a.question_id = q.id) as fam
            from questions q where q.source_id = ? and q.section = 'A'
            order by q.question_number
            """,
            (sid,),
        ).fetchall()

    thumbs = crop_mc_answer_table_rows(REPO_ROOT / source["report_path"], sid)

    items = []
    for r in rows:
        dist = []
        try:
            fam = json.loads(r["fam"]) if r["fam"] else {}
            for d in (fam.get("distribution") or []):
                dist.append({"letter": d.get("letter"), "pct": d.get("pct")})
        except Exception:
            pass
        items.append({
            "qid": r["id"],
            "qn": r["question_number"],
            "current": r["mc_correct"],
            "distribution": dist,
            "thumb_filename": Path(thumbs[r["question_number"]]).name if r["question_number"] in thumbs else None,
        })

    return templates.TemplateResponse(
        request, "admin_mc_answers.html",
        {"source": dict(source), "items": items},
    )


@app.post("/admin/mc-answers/save")
def admin_mc_answers_save(payload: dict) -> dict:
    _admin_guard()
    qid = payload.get("question_id")
    letter = (payload.get("letter") or "").strip().upper()
    if not qid or letter not in {"A", "B", "C", "D", "E"}:
        raise HTTPException(status_code=400, detail="need question_id + letter A-E")
    with _db() as conn:
        # Update questions.mc_correct
        conn.execute("update questions set mc_correct = ? where id = ?", (letter, qid))
        # Update final_answer_md.correct in the answers row, preserving distribution.
        row = conn.execute("select final_answer_md from answers where question_id = ?", (qid,)).fetchone()
        if row and row["final_answer_md"]:
            try:
                blob = json.loads(row["final_answer_md"])
                blob["correct"] = letter
                conn.execute("update answers set final_answer_md = ? where question_id = ?",
                             (json.dumps(blob), qid))
            except json.JSONDecodeError:
                pass
        conn.commit()
    return {"question_id": qid, "letter": letter, "ok": True}


# ───── manual diagram annotation ─────────────────────────────────────


@app.get("/admin/diagrams/{sid}", response_class=HTMLResponse)
def admin_diagrams_list(sid: int, request: Request) -> HTMLResponse:
    """Three-bucket annotation dashboard for one source:
       - To draw: has_diagram=1 but no manual bbox yet
       - Done: has a manual bbox stored
       - Audit warnings: pages with many drawings but no flagged question
    """
    _admin_guard()
    from pipeline.extract_diagrams import find_audit_pages
    with _db() as conn:
        source = conn.execute(
            "select id, year, paper from sources where id = ?", (sid,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        questions = conn.execute(
            """
            select q.id, q.section, q.question_number, q.part, q.has_diagram,
                   q.diagram_path, q.source_page_start, q.source_bbox, q.prompt_md,
                   q.is_mc, q.mc_options_md
            from questions q
            where q.source_id = ?
              and (q.has_diagram = 1
                   or (q.is_mc = 1 and q.mc_options_md like '%"kind": "diagram"%'))
            order by q.source_page_start,
                     case when q.section = 'A' then 0 when q.section = 'B' then 1 else 2 end,
                     q.question_number,
                     case when q.part is null then '' else q.part end
            """,
            (sid,),
        ).fetchall()
    to_draw, done = [], []
    for q in questions:
        sb = json.loads(q["source_bbox"]) if q["source_bbox"] else {}
        # Stem-level row (only when the question itself is flagged with a diagram).
        if q["has_diagram"]:
            d = dict(q)
            d["method"] = sb.get("method", "—")
            d["is_manual"] = (sb.get("method") == "manual")
            d["row_kind"] = "stem"
            (done if d["is_manual"] else to_draw).append(d)
        # MC-option rows: one per diagram option, queued separately.
        if q["is_mc"] and q["mc_options_md"]:
            opts = json.loads(q["mc_options_md"])
            for i, o in enumerate(opts):
                if o.get("kind") != "diagram":
                    continue
                label = chr(ord("A") + i)
                row = dict(q)
                row["row_kind"] = "option"
                row["option_label"] = label
                row["option_path"] = o.get("path")
                row["is_manual"] = (o.get("bbox_source") == "manual")
                (done if row["is_manual"] else to_draw).append(row)
    audit = find_audit_pages(sid)
    return templates.TemplateResponse(
        request,
        "admin_diagrams.html",
        {
            "source": dict(source),
            "to_draw": to_draw,
            "done": done,
            "audit": audit,
        },
    )


@app.get("/admin/diagrams/{sid}/q/{qid}", response_class=HTMLResponse)
def admin_diagram_edit(sid: int, qid: str, request: Request,
                       page: Optional[int] = Query(None)) -> HTMLResponse:
    """Single-question annotation editor. Renders the source page and a draw canvas.
    If `page` is omitted, defaults to the question's source_page_start.
    """
    _admin_guard()
    with _db() as conn:
        q = conn.execute(
            """
            select q.id, q.source_id, q.section, q.question_number, q.part, q.marks,
                   q.prompt_md, q.has_diagram, q.diagram_path, q.source_bbox,
                   q.source_page_start, q.source_page_end, s.year, s.paper, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ? and q.source_id = ?
            """,
            (qid, sid),
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail="question not found in this source")
        # Compute prev/next within the "to draw + done" set for this source.
        nav_rows = conn.execute(
            """
            select id from questions
            where source_id = ? and has_diagram = 1
            order by source_page_start,
                     case when section = 'A' then 0 when section = 'B' then 1 else 2 end,
                     question_number,
                     case when part is null then '' else part end
            """,
            (sid,),
        ).fetchall()
    ids = [r["id"] for r in nav_rows]
    try:
        idx = ids.index(qid)
    except ValueError:
        idx = -1
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if 0 <= idx < len(ids) - 1 else None

    target_page = page if page is not None else q["source_page_start"]
    existing_bbox = None
    sb = json.loads(q["source_bbox"]) if q["source_bbox"] else None
    if sb and sb.get("bbox_normalised"):
        existing_bbox = sb["bbox_normalised"]
    elif sb and sb.get("bbox_pdf") and sb.get("page") == target_page:
        # Compute normalised from PDF coords by re-opening the page.
        pdf_path = REPO_ROOT / q["pdf_path"]
        doc = fitz.open(pdf_path)
        try:
            pg = doc.load_page(target_page - 1)
            pw, ph = pg.rect.width, pg.rect.height
        finally:
            doc.close()
        b = sb["bbox_pdf"]
        existing_bbox = [b[0] / pw, b[1] / ph, b[2] / pw, b[3] / ph]
    return templates.TemplateResponse(
        request,
        "admin_diagram_edit.html",
        {
            "q": dict(q),
            "source_id": sid,
            "target_page": target_page,
            "existing_bbox": existing_bbox,
            "prev_id": prev_id,
            "next_id": next_id,
            "position": (idx + 1, len(ids)),
        },
    )


@app.post("/admin/diagrams/save")
def admin_diagrams_save(payload: dict) -> dict:
    """Body: {question_id, page, bbox_normalised: [x0,y0,x1,y1]}.
    Crops the source page and updates the DB. Idempotent."""
    _admin_guard()
    from pipeline.extract_diagrams import crop_from_manual_bbox
    qid = payload.get("question_id")
    page = payload.get("page")
    bbox = payload.get("bbox_normalised")
    if not qid or page is None or not bbox:
        raise HTTPException(status_code=400,
                             detail="need question_id, page, bbox_normalised")
    result = crop_from_manual_bbox(qid, int(page), [float(v) for v in bbox])
    return result


@app.get("/admin/diagrams/{sid}/q/{qid}/opt/{label}", response_class=HTMLResponse)
def admin_diagram_option_edit(sid: int, qid: str, label: str, request: Request,
                              page: Optional[int] = Query(None)) -> HTMLResponse:
    """Editor for one MC option panel (A/B/C/D/E)."""
    _admin_guard()
    label = label.upper()
    if label not in {"A", "B", "C", "D", "E"}:
        raise HTTPException(status_code=400, detail="option label must be A-E")
    with _db() as conn:
        q = conn.execute(
            """
            select q.id, q.source_id, q.section, q.question_number, q.part, q.marks,
                   q.prompt_md, q.is_mc, q.mc_options_md, q.source_page_start,
                   q.source_page_end, s.year, s.paper, s.pdf_path
            from questions q join sources s on s.id = q.source_id
            where q.id = ? and q.source_id = ? and q.is_mc = 1
            """,
            (qid, sid),
        ).fetchone()
        if q is None or not q["mc_options_md"]:
            raise HTTPException(status_code=404,
                                 detail="MC question with options not found")
    opts = json.loads(q["mc_options_md"])
    idx = ord(label) - ord("A")
    if idx >= len(opts) or opts[idx].get("kind") != "diagram":
        raise HTTPException(status_code=404,
                             detail=f"option {label} is not a diagram option")

    target_page = page if page is not None else q["source_page_start"]
    existing_bbox = None
    bb = opts[idx].get("bbox")
    if isinstance(bb, list) and len(bb) == 4:
        existing_bbox = bb

    diagram_labels = [chr(ord("A") + i) for i, o in enumerate(opts)
                      if o.get("kind") == "diagram"]
    try:
        li = diagram_labels.index(label)
    except ValueError:
        li = -1
    prev_label = diagram_labels[li - 1] if li > 0 else None
    next_label = diagram_labels[li + 1] if 0 <= li < len(diagram_labels) - 1 else None

    return templates.TemplateResponse(
        request,
        "admin_diagram_edit.html",
        {
            "q": dict(q),
            "source_id": sid,
            "target_page": target_page,
            "existing_bbox": existing_bbox,
            "prev_id": None,
            "next_id": None,
            "position": (li + 1, len(diagram_labels)),
            "mode": "option",
            "option_label": label,
            "prev_option": prev_label,
            "next_option": next_label,
        },
    )


@app.post("/admin/diagrams/save_option")
def admin_diagrams_save_option(payload: dict) -> dict:
    """Body: {question_id, option_label, page, bbox_normalised}."""
    _admin_guard()
    from pipeline.extract_diagrams import crop_option_from_manual_bbox
    qid = payload.get("question_id")
    label = payload.get("option_label")
    page = payload.get("page")
    bbox = payload.get("bbox_normalised")
    if not qid or not label or page is None or not bbox:
        raise HTTPException(status_code=400,
                             detail="need question_id, option_label, page, bbox_normalised")
    return crop_option_from_manual_bbox(qid, label, int(page),
                                         [float(v) for v in bbox])


@app.post("/admin/diagrams/dismiss")
def admin_diagrams_dismiss(payload: dict) -> dict:
    """Mark a question as having no diagram (false-positive correction)."""
    _admin_guard()
    qid = payload.get("question_id")
    if not qid:
        raise HTTPException(status_code=400, detail="need question_id")
    with _db() as conn:
        n = conn.execute(
            "update questions set has_diagram = 0, diagram_path = null, source_bbox = null where id = ?",
            (qid,),
        ).rowcount
        conn.commit()
    return {"question_id": qid, "updated": n}


@app.post("/admin/diagrams/flag")
def admin_diagrams_flag(payload: dict) -> dict:
    """Mark a question as having a diagram. Used from audit-warning rows to escalate
    a question from the warning bucket into the 'to draw' bucket."""
    _admin_guard()
    qid = payload.get("question_id")
    if not qid:
        raise HTTPException(status_code=400, detail="need question_id")
    with _db() as conn:
        n = conn.execute(
            "update questions set has_diagram = 1 where id = ?",
            (qid,),
        ).rowcount
        conn.commit()
    return {"question_id": qid, "updated": n}
