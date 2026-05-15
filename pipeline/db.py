"""SQLite connection + migration runner. Single source of truth for paths."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "methods.db"
MIGRATIONS_DIR = REPO_ROOT / "migrations"
EXAM_PAPERS_DIR = REPO_ROOT / "mathematical_methods" / "exam_papers"
EXAMINER_REPORTS_DIR = REPO_ROOT / "mathematical_methods" / "examiner_reports"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def apply_migrations() -> None:
    """Apply migrations from MIGRATIONS_DIR in filename order.

    Tracks applied migrations in `schema_migrations` so re-runs are no-ops.
    Idempotent SQL (create-if-not-exists, on-conflict upserts) still re-runs;
    non-idempotent SQL (alter table add column) is skipped after first apply.
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
            # Always re-run the first two — they're create-if-not-exists / upsert,
            # and we want refreshing the study-design seed to stay automatic.
            is_idempotent = name in {"001_schema.sql", "002_study_design.sql"}
            if name in applied and not is_idempotent:
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


def seed_sources() -> int:
    """Register every (year, paper) PDF in `sources`. Idempotent. Returns rows touched."""
    if not EXAM_PAPERS_DIR.exists():
        return 0
    rows = []
    for pdf in sorted(EXAM_PAPERS_DIR.glob("mathematical_methods_*_paper_*.pdf")):
        # filename: mathematical_methods_<year>_paper_<n>.pdf
        stem = pdf.stem
        parts = stem.split("_")
        year = int(parts[2])
        paper = int(parts[4])
        report = _find_report(year, paper)
        era = "modern_2024" if year >= 2024 else "legacy"
        rows.append((year, paper, str(pdf.relative_to(REPO_ROOT)),
                     str(report.relative_to(REPO_ROOT)) if report else None,
                     era))
    conn = connect()
    try:
        conn.executemany(
            """
            insert into sources (year, paper, pdf_path, report_path, format_era)
            values (?, ?, ?, ?, ?)
            on conflict (year, paper) do update set
              pdf_path = excluded.pdf_path,
              report_path = excluded.report_path,
              format_era = excluded.format_era
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _find_report(year: int, paper: int) -> Path | None:
    """Resolve examiner-report path. Names are inconsistent across years; brute-force match."""
    if not EXAMINER_REPORTS_DIR.exists():
        return None
    year_s = str(year)
    short_year = year_s[-2:]
    candidates = list(EXAMINER_REPORTS_DIR.iterdir())
    for c in candidates:
        n = c.name.lower()
        if year_s not in n and short_year not in n:
            continue
        # paper-disambiguation: look for '1' or '2' near 'methods'/'mm'
        if f"methods{paper}" in n.replace(" ", "").replace("-", "").replace("_", ""):
            return c
        if f"mm{paper}" in n.replace(" ", "").replace("-", "").replace("_", ""):
            return c
        if f"-{paper}-" in n or f"_{paper}_" in n or f"methods-{paper}" in n or f"methods{paper}-" in n:
            return c
    return None


if __name__ == "__main__":
    apply_migrations()
    n = seed_sources()
    conn = connect()
    try:
        sp_count = conn.execute("select count(*) from study_points").fetchone()[0]
        sp_tagable = conn.execute("select count(*) from study_points where is_header = 0").fetchone()[0]
        sa_count = conn.execute("select count(*) from study_areas").fetchone()[0]
        src_count = conn.execute("select count(*) from sources").fetchone()[0]
    finally:
        conn.close()
    print(json.dumps({
        "db_path": str(DB_PATH),
        "study_areas": sa_count,
        "study_points_total": sp_count,
        "study_points_tagable": sp_tagable,
        "sources_seeded": src_count,
    }, indent=2))
