"""Question selection for the user-facing generator form.

Given (chosen dot points, paper type, target marks), return a list of leaf
question IDs that `render.generate_pdf` can hand to `load_groups` (which will
auto-expand each leaf to its parent stem + sibling parts).

Selection scope: picking dot point (aos, sort_order) makes ALL earlier
dot points within the same AoS eligible too — VCE Methods is taught
sequentially within an AoS, so prior dot points are fair game. Multi-select
unions across AoS.

Topic purity: a question is included only when EVERY leaf's primary tag
falls within the chosen scope. `load_groups` will pull in all siblings,
so a 12-mark Section B question with one transformation sub-part and 11
marks of calculus would otherwise drift the paper off-topic. A leaf with
no primary tag (untagged / manual queue) also disqualifies its unit —
unknown topics are treated as out-of-scope.

The unit of selection is the WHOLE question. The marks budget is computed
per unit upfront, summed across all leaf siblings.
"""
from __future__ import annotations

import random
from typing import Literal

from pipeline.db import connect

PaperType = Literal["tech_free", "mc", "tech_active"]


def _paper_type_clause(paper_type: PaperType) -> tuple[str, list]:
    if paper_type == "tech_free":
        return "s.paper = 1", []
    if paper_type == "mc":
        return "s.paper = 2 AND q.section = 'A' AND q.is_mc = 1", []
    if paper_type == "tech_active":
        return "s.paper = 2 AND q.section = 'B'", []
    raise ValueError(f"unknown paper_type {paper_type!r}")


def _eligible_per_aos(chosen_dots: list[tuple[int, int]]) -> dict[int, int]:
    """Collapse chosen dots to {aos: max sort_order chosen}.

    Within an AoS we only need the max chosen sort_order — picking 3.5 + 3.8
    collapses to (aos=3, ≤8). Headers (is_header=1) occupy sort_orders but
    are never tagged, so the ≤ filter is harmless.
    """
    by_aos: dict[int, int] = {}
    for aos, s in chosen_dots:
        by_aos[aos] = max(s, by_aos.get(aos, 0))
    return by_aos


def _eligible_tag_clause(by_aos: dict[int, int]) -> tuple[str, list]:
    """Build OR-joined `(t.aos = ? AND t.dot_point_sort_order <= ?)` per AoS."""
    parts = []
    params: list = []
    for aos, max_s in by_aos.items():
        parts.append("(t.aos = ? AND t.dot_point_sort_order <= ?)")
        params.extend([aos, max_s])
    return "(" + " OR ".join(parts) + ")", params


# Leaf-detection clause matching `app/main.py` GET /generate.pdf — a row is a
# leaf if it has a real `part` (not a `pre-*` sub_stem) OR it's a parentless
# whole question (no sibling parts exist for the same source/section/qnum).
_LEAF_CLAUSE = (
    "((q.part IS NOT NULL AND q.part NOT LIKE 'pre-%') OR NOT EXISTS ("
    " SELECT 1 FROM questions q2 WHERE q2.source_id = q.source_id"
    " AND q2.section IS q.section"
    " AND q2.question_number = q.question_number"
    " AND q2.part IS NOT NULL AND q2.part NOT LIKE 'pre-%'))"
)


def pick_question_ids(
    chosen_dots: list[tuple[int, int]],
    paper_type: PaperType,
    target_marks: int,
    *, seed: int | None = None,
) -> tuple[list[str], dict]:
    """Return (qids, diagnostics).

    `qids` is a list of one representative leaf id per chosen whole-question
    unit; `load_groups` will pull in the parent stem + all siblings.

    Diagnostics: {"target_marks", "achieved_marks", "n_units", "warning"}.
    Caller raises HTTP 400 on empty pool.
    """
    if not chosen_dots:
        return [], {"target_marks": target_marks, "achieved_marks": 0,
                    "n_units": 0, "warning": "No dot points selected."}
    if target_marks <= 0:
        return [], {"target_marks": target_marks, "achieved_marks": 0,
                    "n_units": 0, "warning": "Target marks must be positive."}

    paper_clause, paper_params = _paper_type_clause(paper_type)
    eligible_per_aos = _eligible_per_aos(chosen_dots)
    tag_clause, tag_params = _eligible_tag_clause(eligible_per_aos)

    sql = f"""
        SELECT DISTINCT q.id, q.source_id, q.section, q.question_number
        FROM questions q
        JOIN sources s ON s.id = q.source_id
        JOIN question_tags t ON t.question_id = q.id
        WHERE {paper_clause}
          AND {tag_clause}
          AND {_LEAF_CLAUSE}
    """

    conn = connect()
    try:
        candidate_leaves = conn.execute(sql, paper_params + tag_params).fetchall()

        # Group candidate leaves into whole-question units.
        units: dict[tuple, dict] = {}
        for r in candidate_leaves:
            key = (r["source_id"], r["section"], r["question_number"])
            units.setdefault(key, {"qid": r["id"], "key": key})

        if not units:
            return [], {"target_marks": target_marks, "achieved_marks": 0,
                        "n_units": 0,
                        "warning": "No questions match this combination of dot points and paper type."}

        # Purity filter: for each unit, check EVERY leaf's primary tag is
        # within the chosen scope. Multi-part questions often span AoS — e.g.
        # 2021 P2 Q3 has part (a) on domain/range (AoS 1) but parts (b)–(f)
        # on calculus (AoS 3). load_groups will pull in all siblings, so the
        # unit must be topic-pure or the rendered paper drifts off-topic.
        #
        # A leaf with no primary tag → unknown topic → unit is rejected.
        # Cost: one query per candidate unit (typically dozens, not thousands).
        purity_sql = f"""
            SELECT q.id, COALESCE(q.marks, 0) AS marks,
                   t.aos AS prim_aos, t.dot_point_sort_order AS prim_sort
            FROM questions q
            LEFT JOIN question_tags t
              ON t.question_id = q.id AND t.is_primary = 1
            WHERE q.source_id = ? AND q.section IS ? AND q.question_number = ?
              AND {_LEAF_CLAUSE}
        """
        units_list: list[dict] = []
        for key, u in units.items():
            sid, section, qnum = key
            leaves = conn.execute(purity_sql, (sid, section, qnum)).fetchall()
            total = 0
            pure = True
            for leaf in leaves:
                prim_aos = leaf["prim_aos"]
                prim_sort = leaf["prim_sort"]
                if prim_aos is None:
                    pure = False
                    break
                max_sort = eligible_per_aos.get(prim_aos)
                if max_sort is None or prim_sort > max_sort:
                    pure = False
                    break
                total += leaf["marks"]
            if pure and total > 0:
                u["total_marks"] = total
                units_list.append(u)

        if not units_list:
            return [], {"target_marks": target_marks, "achieved_marks": 0,
                        "n_units": 0,
                        "warning": ("No questions match cleanly. Multi-part questions "
                                    "in the candidate pool include parts on other "
                                    "topics. Try adding more dot points or a different "
                                    "paper type.")}

        # Greedy fill against budget — random order so re-running gives a fresh paper.
        rng = random.Random(seed)
        rng.shuffle(units_list)
        chosen: list[dict] = []
        total = 0
        for u in units_list:
            if total + u["total_marks"] <= target_marks:
                chosen.append(u)
                total += u["total_marks"]
            if total == target_marks:
                break

        # If pool is too small to reach the target, take everything we have.
        # A short paper that flags the gap on the cover beats an empty paper.
        if not chosen and units_list:
            chosen = units_list[:]
            total = sum(u["total_marks"] for u in chosen)

        warning = None
        if total < target_marks:
            warning = (f"Only {total}/{target_marks} marks of matching questions "
                       f"available — add more dot points for a longer paper.")

        return (
            [u["qid"] for u in chosen],
            {"target_marks": target_marks, "achieved_marks": total,
             "n_units": len(chosen), "warning": warning},
        )
    finally:
        conn.close()


def dot_point_labels(chosen_dots: list[tuple[int, int]]) -> str:
    """Human-readable cover summary of the upstream-eligible scope.

    Format: 'AoS 3 §1–§8 (FTC), AoS 4 §1–§5 (binomial distribution)'.
    The §-range reflects what's actually eligible (sort_order ≤ chosen_max),
    not just what the user ticked, so the student understands the breadth.
    """
    if not chosen_dots:
        return "No selection"
    by_aos: dict[int, int] = {}
    for aos, s in chosen_dots:
        by_aos[aos] = max(s, by_aos.get(aos, 0))

    conn = connect()
    try:
        bits: list[str] = []
        for aos in sorted(by_aos):
            max_s = by_aos[aos]
            row = conn.execute(
                """
                SELECT text FROM study_points
                WHERE subject = 'mathematical_methods'
                  AND aos = ? AND sort_order = ? AND is_header = 0
                """,
                (aos, max_s),
            ).fetchone()
            short = _short_label(row["text"]) if row and row["text"] else f"dot point {max_s}"
            range_str = f"§1–§{max_s}" if max_s > 1 else "§1"
            bits.append(f"AoS {aos} {range_str} ({short})")
        return ", ".join(bits)
    finally:
        conn.close()


def _short_label(text: str) -> str:
    """Truncate a dot-point text to a short noun phrase for the cover.

    Cuts at the earliest of: ` — `, ` (`, comma, or first inline math marker `$`,
    so the label never strands a half-rendered LaTeX expression on the cover.
    Falls back to a 60-char hard cap with ellipsis.
    """
    t = text.strip()
    candidates = []
    for sep in (" — ", " (", ", ", " $"):
        i = t.find(sep)
        if 0 < i <= 80:
            candidates.append(i)
    if candidates:
        return t[:min(candidates)].rstrip()
    return t[:60].rstrip() + ("…" if len(t) > 60 else "")


PAPER_TYPE_LABEL = {
    "tech_free": "Technology-free",
    "mc": "Multiple-choice",
    "tech_active": "Technology-active",
}
