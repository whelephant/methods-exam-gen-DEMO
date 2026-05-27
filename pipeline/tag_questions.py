"""Tag each extracted question against the Study Design dot points.

Constraints (enforced by both prompt and DB triggers):
  - At most 2 dot-point tags per question
  - Exactly one tag is marked is_primary
  - Tag rows reference real (aos, sort_order) in study_points
  - No AoS-only fallback — every question must end up with at least 1 dot-point tag
    or land in the manual-review queue.

Tagging granularity: leaf rows only. A row is a leaf if it has a non-null `part`,
OR if it has part=null and no siblings under the same question_number have part!=null.
Stems (rows with part=null AND sibling parts exist) are NOT tagged — their tag context
is supplied to the leaves via the prompt.

Model: Claude Sonnet 4.6, text-only. The full dot-point catalogue is sent as a
cached system-prompt prefix so it costs ~$0.0012 to read instead of ~$0.012 to send
after the first call in a 5-minute window.

CLI:
  python -m pipeline.tag_questions --year 2023 --paper 1
  python -m pipeline.tag_questions --year 2023 --paper 1 --force
  python -m pipeline.tag_questions --question 2023-p1-q3-a
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from pipeline.db import DEFAULT_SUBJECT, REPO_ROOT, SUBJECTS, SubjectSpec, connect, subject_spec
from pipeline.extract_questions import ANTHROPIC_MODEL_IDS, _client
from pipeline.spend import (
    BudgetExceeded, MODEL_PRICING, check_budget, estimate_cost, log_call, total_spend,
)

load_dotenv(REPO_ROOT / ".env")

TAGGING_MODEL = "claude-sonnet-4-6"
CONFIDENCE_FLOOR = 0.5


# ─── build the cached dot-point catalogue (per-subject) ───────────────

def _build_catalogue(subject: str) -> tuple[str, dict[tuple[int, int], str]]:
    """Returns (catalogue_md, valid_keys) for the given subject.

    catalogue_md is the human-readable list of AoS intros and dot points that goes
    into the cached system-prompt prefix. valid_keys is {(aos, sort_order): text}
    used for validation after the model responds.
    """
    conn = connect()
    try:
        areas = conn.execute(
            "select aos, title, intro from study_areas where subject = ? order by aos",
            (subject,),
        ).fetchall()
        points = conn.execute(
            "select aos, sort_order, is_header, text from study_points "
            "where subject = ? order by aos, sort_order",
            (subject,),
        ).fetchall()
    finally:
        conn.close()

    lines: list[str] = []
    valid: dict[tuple[int, int], str] = {}
    for a in areas:
        lines.append(f"## AoS {a['aos']} — {a['title']}")
        lines.append("")
        lines.append(a["intro"])
        lines.append("")
        lines.append("Dot points (tagable items in this AoS):")
        for p in points:
            if p["aos"] != a["aos"]:
                continue
            if p["is_header"]:
                lines.append(f"  *(sub-heading, NOT a tagable dot point — {p['text']})*")
                continue
            lines.append(f"  - **AoS {p['aos']}, dot_point_sort_order {p['sort_order']}**: {p['text']}")
            valid[(p["aos"], p["sort_order"])] = p["text"]
        lines.append("")
    return "\n".join(lines), valid


# Cache catalogues per subject — built lazily on first use (so importing this module
# doesn't run a DB query for every known subject).
_CATALOGUE_CACHE: dict[str, tuple[str, dict[tuple[int, int], str]]] = {}


def get_catalogue(subject: str) -> tuple[str, dict[tuple[int, int], str]]:
    if subject not in _CATALOGUE_CACHE:
        _CATALOGUE_CACHE[subject] = _build_catalogue(subject)
    return _CATALOGUE_CACHE[subject]


SYSTEM_PROMPT_PREFIX = """You tag a single VCE {display_name} (Units 3 & 4) exam question against the Study Design dot points.

# Output rules

Return 1 OR 2 dot-point tags via the `record_tags` tool. Constraints:

- **At most 2 tags.** Use a second tag ONLY when the question genuinely tests two distinct dot points (e.g. an optimisation problem testing AoS 3 dot-point 5 *and* AoS 1 dot-point 4). Don't pad.
- **Exactly one tag has `is_primary: true`**. The primary is the dot point most centrally tested.
- Tag pairs are `(aos, dot_point_sort_order)` — both numbers must reference an item in the catalogue below.
- Set `confidence` in 0..1 for each tag (your own assessment of how clearly the question maps to that dot point).
- If no dot point applies well (confidence < 0.5 on every candidate), return zero tags. The question will go to a manual-review queue. Don't guess just to fill the slot.
- Do NOT tag any `is_header` sub-heading row from the catalogue.

# Rationale

Provide a one-sentence `rationale` per tag explaining *what aspect of the question* maps to that dot point. This is used for human audit, so be specific (cite the actual operation: "uses the product rule to differentiate", not "calculus"). The rationale is also the disambiguation signal when two related dot points could both apply.

# Catalogue

"""


def build_system_prompt(spec: SubjectSpec) -> str:
    catalogue_md, _ = get_catalogue(spec.name)
    return SYSTEM_PROMPT_PREFIX.replace("{display_name}", spec.display_name) + catalogue_md


TAG_TOOL: dict[str, Any] = {
    "name": "record_tags",
    "description": "Record the 0–2 dot-point tags for this question.",
    "input_schema": {
        "type": "object",
        "required": ["tags"],
        "properties": {
            "tags": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": ["aos", "dot_point_sort_order", "is_primary", "confidence", "rationale"],
                    "properties": {
                        # max 6 covers both subjects (Methods has AoS 1–4, Specialist 1–6).
                        # Per-subject validity is checked at run time via the catalogue's valid_keys.
                        "aos": {"type": "integer", "minimum": 1, "maximum": 6},
                        "dot_point_sort_order": {"type": "integer", "minimum": 1},
                        "is_primary": {"type": "boolean"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                    },
                },
            }
        },
    },
}


# ─── validation ───────────────────────────────────────────────────────

def validate_tags(payload: dict, valid_keys: dict[tuple[int, int], str]) -> tuple[Optional[list[dict]], Optional[str]]:
    """Return (cleaned_tags, reason_if_invalid). Cleaned tags pass the CONFIDENCE_FLOOR;
    if fewer than 1 survive, returns ([], None) — caller treats as "no tag, send to review"."""
    tags_raw = payload.get("tags", [])
    if not isinstance(tags_raw, list):
        return None, "tags is not a list"
    if len(tags_raw) > 2:
        return None, f"more than 2 tags ({len(tags_raw)})"

    cleaned: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, t in enumerate(tags_raw):
        if not isinstance(t, dict):
            return None, f"tag[{i}] is not a dict"
        try:
            aos = int(t["aos"])
            sort_order = int(t["dot_point_sort_order"])
            primary = bool(t["is_primary"])
            conf = float(t["confidence"])
        except (KeyError, ValueError, TypeError) as e:
            return None, f"tag[{i}] missing/invalid field: {e}"
        key = (aos, sort_order)
        if key not in valid_keys:
            return None, f"tag[{i}] references unknown dot point ({aos}, {sort_order})"
        if key in seen:
            return None, f"tag[{i}] duplicates ({aos}, {sort_order})"
        seen.add(key)
        if conf < CONFIDENCE_FLOOR:
            continue
        cleaned.append({
            "aos": aos,
            "dot_point_sort_order": sort_order,
            "is_primary": primary,
            "confidence": conf,
            "rationale": str(t.get("rationale") or ""),
        })

    if not cleaned:
        return [], None  # caller handles as "no confident tag"

    primaries = [t for t in cleaned if t["is_primary"]]
    if len(primaries) == 0:
        # promote the highest-confidence tag to primary
        cleaned.sort(key=lambda t: t["confidence"], reverse=True)
        cleaned[0]["is_primary"] = True
    elif len(primaries) > 1:
        # keep highest-confidence primary, demote the others
        primaries.sort(key=lambda t: t["confidence"], reverse=True)
        primary_key = (primaries[0]["aos"], primaries[0]["dot_point_sort_order"])
        for t in cleaned:
            t["is_primary"] = ((t["aos"], t["dot_point_sort_order"]) == primary_key)
    return cleaned, None


# ─── load taggable questions ──────────────────────────────────────────

def _part_sort_key(part: Optional[str]) -> tuple:
    """Order parts: 'a' < 'b' < 'b.i' < 'b.ii' < 'c' < ..."""
    if part is None:
        return ()
    return tuple(part.split("."))


def load_taggable(source_id: int) -> list[dict]:
    """Returns leaf rows joined with parent stem + any preceding sub_stems for the given source.

    A leaf is:
      - a row with a real `part` value (not null, not 'pre-...'), OR
      - a row with part=null AND no real-part siblings (Paper 1 "whole" questions)

    sub_stems (part LIKE 'pre-%') are NOT tagged on their own, but their text is concatenated
    onto each leaf's stem context when the sub_stem precedes-or-equals the leaf's part in
    the natural ordering — so e.g. Q4.h sees the diameter-D stem AND the cylindrical-container
    sub_stem AND the grade-A/B sub_stem AND the serving-speed-V sub_stem, but Q4.a only
    sees the diameter-D stem (it predates the others).
    """
    conn = connect()
    try:
        # Section is part of the question's identity. Section A Q1 and Section B Q1 are
        # different questions that happen to share question_number=1 — without filtering
        # by section, Section A's "whole" MC (part=null) would be mis-classified as a stem
        # of Section B's Q1 (because Section B Q1 has real sub-parts).
        rows = conn.execute(
            """
            select q.id, q.section, q.question_number, q.part, q.prompt_md, q.is_mc, q.mc_options_md
            from questions q
            where q.source_id = ?
              and (
                (q.part is not null and q.part not like 'pre-%')
                or not exists (
                  select 1 from questions q2
                  where q2.source_id = q.source_id
                    and q2.section is q.section
                    and q2.question_number = q.question_number
                    and q2.part is not null
                    and q2.part not like 'pre-%'
                )
              )
            order by q.section, q.question_number, q.part
            """,
            (source_id,),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            qn = r["question_number"]
            sec = r["section"]
            stem_parts: list[str] = []
            # Stem only matters when this row HAS a part — otherwise the row IS the
            # whole question and there's no separate stem to prepend.
            if r["part"] is not None:
                stem_row = conn.execute(
                    "select prompt_md from questions where source_id = ? and section is ? and question_number = ? and part is null",
                    (source_id, sec, qn),
                ).fetchone()
                if stem_row:
                    stem_parts.append(stem_row["prompt_md"])
            sub_stems = conn.execute(
                """
                select part, prompt_md from questions
                where source_id = ? and section is ? and question_number = ? and part like 'pre-%'
                """,
                (source_id, sec, qn),
            ).fetchall()
            leaf_key = _part_sort_key(r["part"])
            for sub in sub_stems:
                # sub["part"] is 'pre-<X>'; the sub_stem precedes part X, so it applies
                # to leaves whose part-key is >= X's part-key.
                before_part = sub["part"][4:]
                if _part_sort_key(before_part) <= leaf_key:
                    stem_parts.append(sub["prompt_md"])
            d = dict(r)
            d["stem_prompt"] = "\n\n".join(stem_parts) if stem_parts else None
            result.append(d)
        return result
    finally:
        conn.close()


# ─── Anthropic call ───────────────────────────────────────────────────

@dataclass
class TagCallResult:
    ok: bool
    payload: dict
    input_tokens: int
    cached_read_tokens: int
    cached_write_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    error: Optional[str] = None


def call_tagger(qrow: dict, spec: SubjectSpec) -> TagCallResult:
    parts: list[str] = [f"Question id: `{qrow['id']}`"]
    if qrow.get("stem_prompt"):
        parts.append(f"\n**Stem** (shared context for this multi-part question):\n\n{qrow['stem_prompt']}")
    label = f"Q{qrow['question_number']}"
    if qrow["part"]:
        label += f".{qrow['part']}"
    parts.append(f"\n**{label} prompt**:\n\n{qrow['prompt_md']}")
    if qrow.get("is_mc") and qrow.get("mc_options_md"):
        try:
            opts = json.loads(qrow["mc_options_md"])
            rendered = []
            for i, o in enumerate(opts):
                label = chr(65 + i)
                if isinstance(o, dict):
                    if o.get("kind") == "diagram":
                        rendered.append(f"{label}. [diagram]")
                    else:
                        rendered.append(f"{label}. {o.get('md', '')}")
                else:
                    rendered.append(f"{label}. {o}")
            parts.append("\n**Options**: " + "; ".join(rendered))
        except Exception:
            pass

    user_msg = "\n".join(parts)

    t0 = time.time()
    try:
        resp = _client().messages.create(
            model=ANTHROPIC_MODEL_IDS[TAGGING_MODEL],
            max_tokens=1024,
            system=[
                {"type": "text", "text": build_system_prompt(spec),
                 "cache_control": {"type": "ephemeral"}}
            ],
            tools=[TAG_TOOL],
            tool_choice={"type": "tool", "name": "record_tags"},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return TagCallResult(False, {}, 0, 0, 0, 0, 0.0, int((time.time() - t0) * 1000), error=str(e))

    latency_ms = int((time.time() - t0) * 1000)
    usage = resp.usage
    input_t = usage.input_tokens
    out_t = usage.output_tokens
    cached_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    cached_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = estimate_cost(
        TAGGING_MODEL,
        input_tokens=input_t, cached_read_tokens=cached_r,
        cached_write_tokens=cached_w, output_tokens=out_t,
    )

    payload: dict = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_tags":
            payload = dict(block.input)
            break

    return TagCallResult(
        ok=bool(payload), payload=payload,
        input_tokens=input_t, cached_read_tokens=cached_r,
        cached_write_tokens=cached_w, output_tokens=out_t,
        cost_usd=cost, latency_ms=latency_ms,
        error=None if payload else "no tool_use block",
    )


# ─── DB writes ────────────────────────────────────────────────────────

def mark_out_of_scope(question_id: str, source_id: int, reason: str) -> None:
    """Set questions.out_of_scope = 1 for a question and log a review-queue note.

    Out-of-scope questions are PRIMARILY testing a topic dropped from the current
    Study Design (mechanics, statics). They get no `question_tags` rows and are
    filtered out of practice paper generation by default.

    Existing tags on the question are deleted (this may overwrite a prior force-fit
    from before the dropped-topics guidance was added to the tagger prompt).
    """
    conn = connect()
    try:
        conn.execute("update questions set out_of_scope = 1 where id = ?", (question_id,))
        conn.execute("delete from question_tags where question_id = ?", (question_id,))
        # Resolved=0 so the human reviewer can audit; reason includes the model's phrase.
        conn.execute(
            """
            insert into review_queue (question_id, source_id, reason, detail)
            values (?, ?, 'out_of_scope_under_current_design', ?)
            """,
            (question_id, source_id, reason or "(no reason provided)"),
        )
        conn.commit()
    finally:
        conn.close()


def write_tags(question_id: str, subject: str, tags: list[dict]) -> None:
    conn = connect()
    try:
        conn.execute("delete from question_tags where question_id = ?", (question_id,))
        for t in tags:
            conn.execute(
                """
                insert into question_tags
                  (question_id, subject, aos, dot_point_sort_order, is_primary, confidence, tagged_by)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (question_id, subject, t["aos"], t["dot_point_sort_order"],
                 1 if t["is_primary"] else 0, t["confidence"], TAGGING_MODEL),
            )
        conn.commit()
    finally:
        conn.close()


def write_review_entry(question_id: str, source_id: int, reason: str, detail: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "insert into review_queue (question_id, source_id, reason, detail) values (?, ?, ?, ?)",
            (question_id, source_id, reason, detail),
        )
        conn.commit()
    finally:
        conn.close()


# ─── orchestrator ─────────────────────────────────────────────────────

def tag_paper(subject: str, year: int, paper: int, *, budget_usd: float = 1.00, force: bool = False) -> dict:
    baseline_usd = total_spend()
    spec = subject_spec(subject)
    _, valid_keys = get_catalogue(subject)
    conn = connect()
    try:
        src = conn.execute(
            "select id from sources where subject = ? and year = ? and paper = ?",
            (subject, year, paper),
        ).fetchone()
        if src is None:
            raise RuntimeError(f"no source for {subject} {year} paper {paper}")
        source_id = src["id"]
        # Clear stale review entries from prior tagger passes for this source.
        conn.execute(
            "delete from review_queue where source_id = ? and reason in ('low_confidence_tag', 'tag_call_failed') and resolved = 0",
            (source_id,),
        )
        conn.commit()
    finally:
        conn.close()

    questions = load_taggable(source_id)
    results = []
    review_count = 0
    for q in questions:
        if not force:
            with connect() as conn:
                existing = conn.execute(
                    "select count(*) from question_tags where question_id = ?", (q["id"],)
                ).fetchone()[0]
            if existing > 0:
                results.append({"qid": q["id"], "status": "skipped_existing", "tags": existing})
                continue

        check_budget(budget_usd, baseline_usd)
        cr = call_tagger(q, spec)
        log_call(
            call_type="tag", model=TAGGING_MODEL,
            input_tokens=cr.input_tokens,
            cached_tokens=cr.cached_read_tokens + cr.cached_write_tokens,
            output_tokens=cr.output_tokens, cost_usd=cr.cost_usd,
            latency_ms=cr.latency_ms, question_id=q["id"], source_id=source_id,
            ok=cr.ok, error_message=cr.error,
        )
        if not cr.ok:
            write_review_entry(q["id"], source_id, "tag_call_failed", cr.error or "")
            review_count += 1
            results.append({"qid": q["id"], "status": "call_failed", "error": cr.error})
            continue

        cleaned, reason = validate_tags(cr.payload, valid_keys)
        if cleaned is None:
            write_review_entry(q["id"], source_id, "tag_call_invalid",
                               f"{reason}: {json.dumps(cr.payload)[:300]}")
            review_count += 1
            results.append({"qid": q["id"], "status": "invalid", "reason": reason})
            continue
        if not cleaned:
            write_review_entry(q["id"], source_id, "low_confidence_tag",
                               f"all candidate tags below confidence floor; raw: {json.dumps(cr.payload)[:300]}")
            review_count += 1
            results.append({"qid": q["id"], "status": "low_confidence",
                            "raw": cr.payload})
            continue

        write_tags(q["id"], subject, cleaned)
        primary = next(t for t in cleaned if t["is_primary"])
        results.append({
            "qid": q["id"], "status": "tagged",
            "n_tags": len(cleaned),
            "primary": f"AoS {primary['aos']} dot {primary['dot_point_sort_order']}",
            "primary_text": valid_keys[(primary["aos"], primary["dot_point_sort_order"])][:80],
        })

    return {
        "subject": subject, "year": year, "paper": paper,
        "taggable": len(questions),
        "tagged": sum(1 for r in results if r["status"] == "tagged"),
        "skipped_existing": sum(1 for r in results if r["status"] == "skipped_existing"),
        "to_review": review_count,
        "results": results,
    }


def tag_one(question_id: str, *, budget_usd: float = 0.10, force: bool = False) -> dict:
    baseline_usd = total_spend()
    conn = connect()
    try:
        row = conn.execute(
            "select q.*, s.id as src_id, s.subject as src_subject "
            "from questions q join sources s on s.id=q.source_id where q.id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"question {question_id} not found")
        source_id = row["src_id"]
        subject = row["src_subject"]
        spec = subject_spec(subject)
        _, valid_keys = get_catalogue(subject)
        if not force:
            existing = conn.execute(
                "select count(*) from question_tags where question_id = ?", (question_id,)
            ).fetchone()[0]
            if existing > 0:
                return {"qid": question_id, "status": "skipped_existing", "tags": existing}
        # Reuse the same stem + sub_stem composition logic as load_taggable.
        # Section-aware so Section A and Section B questions sharing a question_number
        # don't cross-pollinate context.
        stem_parts: list[str] = []
        sec = row["section"]
        if row["part"] is not None:
            stem_row = conn.execute(
                "select prompt_md from questions where source_id = ? and section is ? and question_number = ? and part is null",
                (source_id, sec, row["question_number"]),
            ).fetchone()
            if stem_row:
                stem_parts.append(stem_row["prompt_md"])
        sub_stems = conn.execute(
            """
            select part, prompt_md from questions
            where source_id = ? and section is ? and question_number = ? and part like 'pre-%'
            """,
            (source_id, sec, row["question_number"]),
        ).fetchall()
        leaf_key = _part_sort_key(row["part"])
        for sub in sub_stems:
            before_part = sub["part"][4:]
            if _part_sort_key(before_part) <= leaf_key:
                stem_parts.append(sub["prompt_md"])
    finally:
        conn.close()

    q = dict(row)
    q["stem_prompt"] = "\n\n".join(stem_parts) if stem_parts else None

    check_budget(budget_usd, baseline_usd)
    cr = call_tagger(q, spec)
    log_call(
        call_type="tag", model=TAGGING_MODEL,
        input_tokens=cr.input_tokens,
        cached_tokens=cr.cached_read_tokens + cr.cached_write_tokens,
        output_tokens=cr.output_tokens, cost_usd=cr.cost_usd,
        latency_ms=cr.latency_ms, question_id=question_id, source_id=source_id,
        ok=cr.ok, error_message=cr.error,
    )
    if not cr.ok:
        write_review_entry(question_id, source_id, "tag_call_failed", cr.error or "")
        return {"qid": question_id, "status": "call_failed", "error": cr.error}
    cleaned, reason = validate_tags(cr.payload, valid_keys)
    if cleaned is None:
        write_review_entry(question_id, source_id, "tag_call_invalid",
                           f"{reason}: {json.dumps(cr.payload)[:300]}")
        return {"qid": question_id, "status": "invalid", "reason": reason}
    if not cleaned:
        write_review_entry(question_id, source_id, "low_confidence_tag",
                           f"all candidate tags below floor; raw: {json.dumps(cr.payload)[:300]}")
        return {"qid": question_id, "status": "low_confidence", "raw": cr.payload}
    write_tags(question_id, subject, cleaned)
    return {"qid": question_id, "status": "tagged", "tags": cleaned}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--year", type=int, help="paired with --paper")
    g.add_argument("--question", type=str, help="single question id, e.g. 2023-p1-q3-a or sp-2023-p1-q3-a")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT, choices=sorted(SUBJECTS.keys()))
    ap.add_argument("--paper", type=int, choices=[1, 2])
    ap.add_argument("--budget", type=float, default=1.00)
    ap.add_argument("--force", action="store_true",
                    help="re-tag questions even if they already have tags")
    args = ap.parse_args()

    try:
        if args.question:
            # subject derived from the question's source row in tag_one
            print(json.dumps(tag_one(args.question, budget_usd=args.budget, force=args.force), indent=2))
        else:
            if args.paper is None:
                ap.error("--year requires --paper")
            print(json.dumps(tag_paper(args.subject, args.year, args.paper, budget_usd=args.budget, force=args.force), indent=2))
    except BudgetExceeded as e:
        print(f"BUDGET EXCEEDED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
