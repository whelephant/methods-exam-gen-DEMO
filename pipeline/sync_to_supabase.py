"""Push the local SQLite state + diagram/answer PNGs to Supabase.

One-way sync (local → Supabase). The local SQLite DB is the source of truth;
this script makes Supabase reflect it.

Reads credentials from environment:
    SUPABASE_URL              — e.g. https://abcdefgh.supabase.co
    SUPABASE_SERVICE_ROLE_KEY — service_role key (bypasses RLS)

Both come from the Supabase Dashboard → Project Settings → API.

What it does
------------
1. Truncate-and-insert the 6 public tables in dependency order:
   study_areas → study_points → sources → questions → question_tags → answers
2. Apply `question_overrides` to questions on the way out, so Supabase sees
   the corrected prompt_md / mc_options_md / mc_correct.
3. Strip local-only columns (pdf_path, report_path, source_report_path,
   extraction_model, extraction_run_id, source_bbox).
4. Rewrite diagram_path / answer_image_path from 'assets/<bucket>/<file>'
   to '<bucket>/<file>' so they match the Supabase Storage object keys.
5. Upload assets/diagrams/*.png and assets/answers/*.png to the `assets`
   bucket. Creates the bucket (public) if missing.

Usage
-----
    python -m pipeline.sync_to_supabase                 # full sync
    python -m pipeline.sync_to_supabase --tables-only   # skip Storage uploads
    python -m pipeline.sync_to_supabase --storage-only  # skip table sync
    python -m pipeline.sync_to_supabase --dry-run       # plan only, no writes
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from pipeline.db import REPO_ROOT, connect

BUCKET = "assets"
DIAGRAMS_DIR = REPO_ROOT / "assets" / "diagrams"
ANSWERS_DIR = REPO_ROOT / "assets" / "answers"

# Tables in dependency order — must insert parents before children.
TABLE_ORDER = [
    "study_areas",
    "study_points",
    "sources",
    "questions",
    "question_tags",
    "answers",
]

# Columns to drop on the way out (local-only, see CLAUDE.md).
DROPPED_COLS: dict[str, set[str]] = {
    "sources":   {"pdf_path", "report_path"},
    "questions": {"extraction_model", "extraction_run_id", "source_bbox"},
    "answers":   {"source_report_path"},
}

# Columns whose SQLite TEXT value is a JSON string; need to be parsed before
# sending to Postgres jsonb.
JSON_COLS: dict[str, set[str]] = {
    "sources":   {"skipped_pages"},
    "questions": {"mc_options_md"},
}

# Columns whose SQLite INTEGER value is 0/1 and the Postgres column is boolean.
BOOL_COLS: dict[str, set[str]] = {
    "study_points":   {"is_header"},
    "questions":      {"is_mc", "has_diagram"},
    "question_tags":  {"is_primary"},
}


def _strip_assets_prefix(path: str | None) -> str | None:
    """'assets/diagrams/foo.png' → 'diagrams/foo.png'; None passes through."""
    if not path:
        return None
    if path.startswith("assets/"):
        return path[len("assets/"):]
    return path


def _coerce_row(table: str, row: sqlite3.Row) -> dict[str, Any]:
    """Apply column drops, JSON parse, bool coercion, and path rewrites."""
    drops = DROPPED_COLS.get(table, set())
    json_cols = JSON_COLS.get(table, set())
    bool_cols = BOOL_COLS.get(table, set())
    out: dict[str, Any] = {}
    for k in row.keys():
        if k in drops:
            continue
        v = row[k]
        if k in json_cols and isinstance(v, str) and v:
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                # Leave as text — the row's lineage might warrant inspection,
                # but skip rather than crash the whole sync.
                pass
        elif k in bool_cols and v is not None:
            v = bool(v)
        out[k] = v
    if table == "questions":
        out["diagram_path"] = _strip_assets_prefix(out.get("diagram_path"))
    if table == "answers":
        out["answer_image_path"] = _strip_assets_prefix(out.get("answer_image_path"))
    return out


def _load_questions_with_overrides(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return questions rows with question_overrides applied."""
    rows = conn.execute("select * from questions order by id").fetchall()
    overrides = {
        r["question_id"]: r
        for r in conn.execute("select * from question_overrides").fetchall()
    }
    out = []
    for row in rows:
        qid = row["id"]
        d = _coerce_row("questions", row)
        ov = overrides.get(qid)
        if ov is not None:
            if ov["prompt_md"] is not None:
                d["prompt_md"] = ov["prompt_md"]
            if ov["mc_options_md"] is not None:
                try:
                    d["mc_options_md"] = json.loads(ov["mc_options_md"])
                except json.JSONDecodeError:
                    d["mc_options_md"] = ov["mc_options_md"]
            if ov["mc_correct"] is not None:
                d["mc_correct"] = ov["mc_correct"]
        out.append(d)
    return out


def _validate_tags(rows: list[dict[str, Any]]) -> None:
    """Enforce 'max 2 tags per question, exactly one primary' before insert."""
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["question_id"]].append(r)
    problems = []
    for qid, tags in by_q.items():
        if len(tags) > 2:
            problems.append(f"  {qid}: {len(tags)} tags (max 2)")
        primaries = sum(1 for t in tags if t["is_primary"])
        if primaries != 1:
            problems.append(f"  {qid}: {primaries} primary tags (expected 1)")
    if problems:
        raise SystemExit(
            "question_tags validation failed:\n" + "\n".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )


def _load_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table == "questions":
        return _load_questions_with_overrides(conn)
    rows = conn.execute(f"select * from {table}").fetchall()
    out = [_coerce_row(table, r) for r in rows]
    if table == "question_tags":
        _validate_tags(out)
    return out


def _chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def sync_tables(client, dry_run: bool) -> None:
    sqlite_conn = connect()
    try:
        # Delete in reverse dependency order to satisfy FKs.
        for table in reversed(TABLE_ORDER):
            if dry_run:
                print(f"[dry-run] would delete from {table}")
            else:
                # supabase-py requires a filter on delete(); use a tautology.
                # neq('subject', '__never__') works for tables without 'subject';
                # easier: just use 'id is not null' style via filter '.neq("<any-col>", None)'.
                # Cleanest: use rpc-style truncate? Not available with anon API.
                # Fall back to delete all rows in chunks via raw SQL would need direct DB.
                # supabase-py: .delete().neq('<col>', '<impossible-value>')
                # We'll pick a column that exists on every table.
                # study_areas/study_points have 'subject'; sources/questions/answers/question_tags
                # all have at least one non-null column we can filter on.
                filters = {
                    "study_areas":   ("subject", "__none__"),
                    "study_points":  ("subject", "__none__"),
                    "sources":       ("id", -1),
                    "questions":     ("id", "__none__"),
                    "question_tags": ("subject", "__none__"),
                    "answers":       ("question_id", "__none__"),
                }
                col, val = filters[table]
                client.table(table).delete().neq(col, val).execute()
                print(f"  cleared {table}")

        for table in TABLE_ORDER:
            rows = _load_table(sqlite_conn, table)
            if dry_run:
                print(f"[dry-run] would insert {len(rows)} rows into {table}")
                continue
            if not rows:
                print(f"  {table}: 0 rows")
                continue
            for chunk in _chunked(rows, 500):
                client.table(table).insert(chunk).execute()
            print(f"  inserted {len(rows)} rows into {table}")
    finally:
        sqlite_conn.close()


def _ensure_bucket(client, dry_run: bool) -> None:
    try:
        existing = {b.name for b in client.storage.list_buckets()}
    except Exception as e:
        print(f"warning: could not list buckets: {e}")
        existing = set()
    if BUCKET in existing:
        return
    if dry_run:
        print(f"[dry-run] would create public bucket '{BUCKET}'")
        return
    client.storage.create_bucket(BUCKET, options={"public": True})
    print(f"  created public bucket '{BUCKET}'")


def sync_storage(client, dry_run: bool) -> None:
    _ensure_bucket(client, dry_run)
    for local_dir, prefix in [(DIAGRAMS_DIR, "diagrams"), (ANSWERS_DIR, "answers")]:
        if not local_dir.exists():
            print(f"  skip {prefix}/ — local dir missing: {local_dir}")
            continue
        files = sorted(local_dir.glob("*.png"))
        print(f"  {prefix}/: {len(files)} local files")
        for f in files:
            key = f"{prefix}/{f.name}"
            if dry_run:
                continue
            with f.open("rb") as fh:
                # upsert=True so re-runs replace existing objects.
                client.storage.from_(BUCKET).upload(
                    path=key,
                    file=fh.read(),
                    file_options={
                        "content-type": "image/png",
                        "upsert": "true",
                    },
                )
        if not dry_run:
            print(f"  uploaded {len(files)} files to {BUCKET}/{prefix}/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tables-only", action="store_true")
    parser.add_argument("--storage-only", action="store_true")
    args = parser.parse_args(argv)

    if args.tables_only and args.storage_only:
        parser.error("--tables-only and --storage-only are mutually exclusive")

    load_dotenv(REPO_ROOT / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print(
            "error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
            "(see .env or Supabase dashboard).",
            file=sys.stderr,
        )
        return 2

    try:
        from supabase import create_client
    except ImportError:
        print("error: `pip install supabase` (see requirements.txt)", file=sys.stderr)
        return 2

    client = create_client(url, key)

    if not args.storage_only:
        print("syncing tables …")
        sync_tables(client, args.dry_run)
    if not args.tables_only:
        print("syncing Storage …")
        sync_storage(client, args.dry_run)

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
