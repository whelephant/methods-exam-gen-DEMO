-- Add `subject` to `sources` and `questions` so the schema can hold multiple subjects
-- (Mathematical Methods + Specialist Mathematics) under the "VCE Hub" brand.
--
-- `study_areas`, `study_points`, and `question_tags` already carry `subject` from
-- migration 001 — this migration brings `sources` and `questions` into line.
--
-- - `sources`: full table rebuild because the unique constraint reshapes from
--   `(year, paper)` to `(subject, year, paper)`. The integer PK `id` is preserved
--   explicitly so the integer FKs from questions/review_queue/extraction_log
--   stay valid.
-- - `questions`: simple `alter table add column` is enough — its unique constraint
--   `(source_id, section, question_number, part)` already implies subject via the
--   source_id FK, so no rebuild is needed. `subject` is a denormalised convenience
--   column for queries that don't want to join sources.
--
-- All existing rows default to 'mathematical_methods' (the only subject seeded so far).

pragma foreign_keys = off;

begin transaction;

-- ─── sources rebuild ────────────────────────────────────────────────────

create table sources_new (
  id            integer primary key,
  subject       text not null default 'mathematical_methods',
  year          integer not null,
  paper         integer not null check (paper in (1, 2)),
  pdf_path      text not null,
  report_path   text,
  format_era    text not null check (format_era in ('legacy', 'modern_2024')),
  skipped_pages text,
  unique (subject, year, paper)
);

insert into sources_new
  (id, subject, year, paper, pdf_path, report_path, format_era, skipped_pages)
select
  id, 'mathematical_methods', year, paper, pdf_path, report_path, format_era, skipped_pages
from sources;

drop table sources;
alter table sources_new rename to sources;

-- ─── questions: add column (no rebuild required) ────────────────────────

alter table questions add column subject text not null default 'mathematical_methods';

-- ─── verify FK graph survived the rebuild ───────────────────────────────
-- If any FK is broken, `foreign_key_check` returns rows and the commit below
-- will roll back via the surrounding pragma — but easier to fail loudly here.

commit;

pragma foreign_keys = on;
