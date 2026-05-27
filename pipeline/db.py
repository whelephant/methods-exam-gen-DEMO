"""SQLite connection + migration runner. Single source of truth for paths and subjects.

The pipeline supports multiple VCE subjects under one database. Add a subject by
extending the `SUBJECTS` registry below — the rest of the pipeline reads from it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "methods.db"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ─── Subject registry ──────────────────────────────────────────────────
# Each subject lives in its own directory tree mirroring `mathematical_methods/`.
# `id_prefix` keeps the deterministic `questions.id` text-PK globally unique across
# subjects: Methods keeps the legacy bare form ("2023-p1-q1") so existing FK chains
# don't need rewriting; Specialist prepends "sp-" ("sp-2023-p1-q1").

@dataclass(frozen=True)
class SubjectSpec:
    name: str                       # canonical subject key, matches `sources.subject`
    display_name: str               # human-readable, used in prompts and UI
    exam_papers_dir: Path
    examiner_reports_dir: Path
    pdf_glob: str                   # e.g. "mathematical_methods_*_paper_*.pdf"
    id_prefix: str                  # prepended to questions.id, e.g. "" or "sp-"
    running_header_short: str       # text in the page-top banner, e.g. "MATHMETH EXAM"
    formula_heading: str            # text that uniquely identifies the formula sheet


SUBJECTS: dict[str, SubjectSpec] = {
    "mathematical_methods": SubjectSpec(
        name="mathematical_methods",
        display_name="Mathematical Methods",
        exam_papers_dir=REPO_ROOT / "mathematical_methods" / "exam_papers",
        examiner_reports_dir=REPO_ROOT / "mathematical_methods" / "examiner_reports",
        pdf_glob="mathematical_methods_*_paper_*.pdf",
        id_prefix="",
        running_header_short="MATHMETH EXAM",
        formula_heading="Mathematical Methods formulas",
    ),
    "specialist_mathematics": SubjectSpec(
        name="specialist_mathematics",
        display_name="Specialist Mathematics",
        exam_papers_dir=REPO_ROOT / "specialist_mathematics" / "exam_papers",
        examiner_reports_dir=REPO_ROOT / "specialist_mathematics" / "examiner_reports",
        pdf_glob="specialist_mathematics_*_paper_*.pdf",
        id_prefix="sp-",
        running_header_short="SPECMATH EXAM",
        formula_heading="Specialist Mathematics formulas",
    ),
}

DEFAULT_SUBJECT = "mathematical_methods"


def subject_spec(subject: str) -> SubjectSpec:
    if subject not in SUBJECTS:
        raise ValueError(f"unknown subject: {subject!r} (known: {list(SUBJECTS)})")
    return SUBJECTS[subject]


# ─── Connection / migrations ───────────────────────────────────────────

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


# Study-design seeds are upserts and safe to re-apply on every db.py run so that
# updating a dot point doesn't require manually clearing schema_migrations.
IDEMPOTENT_MIGRATIONS = {
    "001_schema.sql",
    "002_study_design.sql",
    "006_specialist_study_design.sql",
}


def apply_migrations() -> None:
    """Apply migrations from MIGRATIONS_DIR in filename order.

    Tracks applied migrations in `schema_migrations` so re-runs are no-ops for
    non-idempotent migrations. Migrations listed in IDEMPOTENT_MIGRATIONS are
    re-run every time (create-if-not-exists or upsert).
    """
    conn = connect()
    try:
        conn.execute(
            """
            create table if not exists schema_migrations (
              filename   text primary key,
              applied_at text not null default (datetime('now'))
            )
            """
        )
        applied = {r[0] for r in conn.execute("select filename from schema_migrations").fetchall()}
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            name = sql_file.name
            if name in applied and name not in IDEMPOTENT_MIGRATIONS:
                continue
            sql = sql_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "insert or replace into schema_migrations (filename) values (?)",
                (name,),
            )
        conn.commit()
    finally:
        conn.close()


# ─── Source seeding ────────────────────────────────────────────────────

def seed_sources() -> dict[str, int]:
    """Register every (subject, year, paper) PDF in `sources`. Idempotent.

    Returns dict {subject: rows_touched}.
    """
    counts: dict[str, int] = {}
    rows: list[tuple] = []
    for spec in SUBJECTS.values():
        n_for_subject = 0
        if not spec.exam_papers_dir.exists():
            counts[spec.name] = 0
            continue
        for pdf in sorted(spec.exam_papers_dir.glob(spec.pdf_glob)):
            year, paper = _parse_pdf_filename(spec, pdf)
            report = _find_report(spec, year, paper)
            era = "modern_2024" if year >= 2024 else "legacy"
            rows.append((
                spec.name, year, paper,
                str(pdf.relative_to(REPO_ROOT)),
                str(report.relative_to(REPO_ROOT)) if report else None,
                era,
            ))
            n_for_subject += 1
        counts[spec.name] = n_for_subject

    if rows:
        conn = connect()
        try:
            conn.executemany(
                """
                insert into sources (subject, year, paper, pdf_path, report_path, format_era)
                values (?, ?, ?, ?, ?, ?)
                on conflict (subject, year, paper) do update set
                  pdf_path    = excluded.pdf_path,
                  report_path = excluded.report_path,
                  format_era  = excluded.format_era
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    return counts


def _parse_pdf_filename(spec: SubjectSpec, pdf: Path) -> tuple[int, int]:
    """Both subjects use `{subject}_<year>_paper_<n>.pdf`. Extract (year, paper)."""
    parts = pdf.stem.split("_")
    # Filename shape: <token>_<token>..._<year>_paper_<n>.pdf — year and paper
    # are always the trailing two recognisable pieces. Find them robustly.
    # For both current subjects the pattern is identical: prefix tokens + _<year>_paper_<n>.
    try:
        paper_idx = parts.index("paper")
    except ValueError as e:
        raise RuntimeError(f"unexpected filename shape: {pdf.name}") from e
    year = int(parts[paper_idx - 1])
    paper = int(parts[paper_idx + 1])
    return year, paper


# ─── Examiner-report resolvers ─────────────────────────────────────────

def _find_report(spec: SubjectSpec, year: int, paper: int) -> Optional[Path]:
    """Dispatch to the subject-specific examiner-report resolver."""
    if spec.name == "mathematical_methods":
        return _find_methods_report(spec.examiner_reports_dir, year, paper)
    if spec.name == "specialist_mathematics":
        return _build_specialist_report_map(spec.examiner_reports_dir).get((year, paper))
    raise NotImplementedError(f"no report resolver for subject {spec.name!r}")


def _find_methods_report(reports_dir: Path, year: int, paper: int) -> Optional[Path]:
    """Methods report filename resolver (legacy heuristic preserved from original db.py)."""
    if not reports_dir.exists():
        return None
    year_s = str(year)
    short_year = year_s[-2:]
    for c in reports_dir.iterdir():
        n = c.name.lower()
        if year_s not in n and short_year not in n:
            continue
        flat = n.replace(" ", "").replace("-", "").replace("_", "")
        if f"methods{paper}" in flat or f"mm{paper}" in flat:
            return c
        if f"-{paper}-" in n or f"_{paper}_" in n or f"methods-{paper}" in n or f"methods{paper}-" in n:
            return c
    return None


# Match modern Specialist docx names like:
#   2020specmaths1-exam-report.docx
#   2021specmaths2-report.docx
#   2023specialistmaths1-report.docx
#   2025-SpecialistMaths2-report.docx
_SPECIALIST_DOCX_RE = re.compile(
    r"^(?P<year>\d{4})[-_]?(?:spec(?:ial)?(?:ist)?)?maths?(?P<paper>\d)",
    re.IGNORECASE,
)
# Match legacy PDF names like SM1_examrep.pdf, SM1_examrep16.pdf, sm2_examrep18.pdf.
_SPECIALIST_PDF_RE = re.compile(
    r"^sm(?P<paper>\d)_examrep(?P<yy>\d{2})?\.pdf$",
    re.IGNORECASE,
)


def _build_specialist_report_map(reports_dir: Path) -> dict[tuple[int, int], Path]:
    """Map (year, paper) → examiner-report Path for all Specialist filename eras.

    Un-suffixed legacy PDFs (e.g. `SM1_examrep.pdf`) are assigned by elimination:
    any (2016–2019, paper) slot that has no suffixed PDF gets the un-suffixed file
    for that paper number. This matters because the 2019 P1 report is the only one
    without a year suffix in the corpus.
    """
    by_key: dict[tuple[int, int], Path] = {}
    unsuffixed_pdfs: dict[int, Path] = {}

    if not reports_dir.exists():
        return by_key

    for f in sorted(reports_dir.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        if name.lower().endswith(".docx"):
            m = _SPECIALIST_DOCX_RE.match(name)
            if m:
                year = int(m.group("year"))
                paper = int(m.group("paper"))
                by_key[(year, paper)] = f
                continue
        if name.lower().endswith(".pdf"):
            m = _SPECIALIST_PDF_RE.match(name)
            if m:
                paper = int(m.group("paper"))
                yy = m.group("yy")
                if yy is None:
                    unsuffixed_pdfs[paper] = f
                else:
                    by_key[(2000 + int(yy), paper)] = f
                continue

    # Resolve un-suffixed PDFs by elimination across 2016-2019.
    for paper, f in unsuffixed_pdfs.items():
        for year in range(2016, 2020):
            if (year, paper) not in by_key:
                by_key[(year, paper)] = f
                break

    return by_key


# ─── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    apply_migrations()
    counts = seed_sources()
    conn = connect()
    try:
        sp_total = conn.execute("select count(*) from study_points").fetchone()[0]
        sp_by_subject = dict(conn.execute(
            "select subject, count(*) from study_points where is_header = 0 group by subject"
        ).fetchall())
        sa_by_subject = dict(conn.execute(
            "select subject, count(*) from study_areas group by subject"
        ).fetchall())
        src_by_subject = dict(conn.execute(
            "select subject, count(*) from sources group by subject"
        ).fetchall())
    finally:
        conn.close()
    print(json.dumps({
        "db_path": str(DB_PATH),
        "study_areas_by_subject": sa_by_subject,
        "study_points_tagable_by_subject": sp_by_subject,
        "study_points_total": sp_total,
        "sources_seeded_by_subject": counts,
        "sources_in_db_by_subject": src_by_subject,
    }, indent=2))
