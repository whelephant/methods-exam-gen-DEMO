-- VCE Methods Exam Gen — core schema.
-- Pure SQLite. Apply after enabling foreign keys (`pragma foreign_keys = on`).

pragma foreign_keys = on;

-- ─── STUDY DESIGN ────────────────────────────────────────────────────
-- Source of truth for AoS + dot-point taxonomy. Seeded by 002_study_design.sql.

create table if not exists study_areas (
  subject text not null,
  aos     integer not null,
  title   text not null,
  intro   text not null,
  primary key (subject, aos)
);

create table if not exists study_points (
  subject     text not null,
  aos         integer not null,
  is_header   integer not null default 0,         -- 1 = sub-heading, not a tagable dot point
  text        text not null,
  sort_order  integer not null,
  primary key (subject, aos, sort_order),
  foreign key (subject, aos) references study_areas(subject, aos)
);

-- ─── SOURCES ─────────────────────────────────────────────────────────
-- One row per source PDF (year × paper).

create table if not exists sources (
  id            integer primary key,
  year          integer not null,
  paper         integer not null check (paper in (1, 2)),
  pdf_path      text not null,
  report_path   text,
  format_era    text not null check (format_era in ('legacy', 'modern_2024')),
  skipped_pages text,                              -- JSON int array (formula-sheet pages, etc.)
  unique (year, paper)
);

-- ─── QUESTIONS ───────────────────────────────────────────────────────
-- One row per leaf question or sub-part. A composite parent question (e.g. Q3 with parts a/b/c)
-- gets rows for the children only — the stem prose belongs to whichever child consumes it
-- or, if the stem is shared, to a row with part = NULL and prompt_md holding the stem.

create table if not exists questions (
  id                  text primary key,            -- deterministic: '<year>-p<paper>-q<n>[-<part>]'
  source_id           integer not null references sources(id),
  section             text check (section in ('A', 'B') or section is null),
  question_number     integer not null,
  part                text,                        -- 'a', 'b', 'b.i', null for top-level
  marks               integer,
  prompt_md           text not null,               -- KaTeX-flavoured markdown
  is_mc               integer not null default 0,
  mc_options_md       text,                        -- JSON array of option strings
  mc_correct          text,                        -- 'A'..'E'
  has_diagram         integer not null default 0,
  diagram_path        text,                        -- relative SVG path under assets/diagrams/
  source_page_start   integer not null,
  source_page_end     integer not null,
  source_bbox         text,                        -- JSON [x0, y0, x1, y1] (page coords) for traceability
  extraction_model    text,                        -- 'claude-haiku-4-5', etc.
  extraction_run_id   integer,                     -- references extraction_log(id)
  created_at          text not null default (datetime('now')),
  unique (source_id, question_number, part)
);

create index if not exists idx_questions_source on questions(source_id);

-- ─── TAGS ────────────────────────────────────────────────────────────
-- Up to 2 dot-point tags per question, exactly one primary.

create table if not exists question_tags (
  question_id           text not null references questions(id) on delete cascade,
  subject               text not null default 'mathematical_methods',
  aos                   integer not null,
  dot_point_sort_order  integer not null,
  is_primary            integer not null default 0,
  confidence            real,                      -- model-reported 0..1
  tagged_by             text,                      -- 'claude-sonnet-4-6' or 'manual'
  primary key (question_id, aos, dot_point_sort_order),
  foreign key (subject, aos, dot_point_sort_order) references study_points(subject, aos, sort_order)
);

create index if not exists idx_question_tags_aos on question_tags(aos, dot_point_sort_order);

-- Enforce: at most 2 tags per question, exactly one primary.
create trigger if not exists question_tags_cap_two_insert
before insert on question_tags
for each row
when (select count(*) from question_tags where question_id = new.question_id) >= 2
begin
  select raise(abort, 'question_tags: max 2 tags per question');
end;

create trigger if not exists question_tags_one_primary_insert
before insert on question_tags
for each row
when new.is_primary = 1
   and exists (select 1 from question_tags
               where question_id = new.question_id and is_primary = 1)
begin
  select raise(abort, 'question_tags: only one primary tag per question');
end;

create trigger if not exists question_tags_one_primary_update
before update of is_primary on question_tags
for each row
when new.is_primary = 1
   and exists (select 1 from question_tags
               where question_id = new.question_id
                 and is_primary = 1
                 and (aos, dot_point_sort_order) != (new.aos, new.dot_point_sort_order))
begin
  select raise(abort, 'question_tags: only one primary tag per question');
end;

-- ─── ANSWERS ─────────────────────────────────────────────────────────
-- Examiner-report content, verbatim. No LLM-generated solutions.

create table if not exists answers (
  question_id        text primary key references questions(id) on delete cascade,
  final_answer_md    text,                          -- best-effort parsed final answer
  commentary_md      text not null,                 -- examiner-report commentary, verbatim
  source_report_path text not null,
  created_at         text not null default (datetime('now'))
);

-- ─── REVIEW QUEUE ────────────────────────────────────────────────────
-- Anything the pipeline wasn't confident about lands here for human review.

create table if not exists review_queue (
  id           integer primary key,
  question_id  text references questions(id) on delete cascade,
  source_id    integer references sources(id),
  page         integer,
  reason       text not null,                       -- 'low_confidence_tag', 'cid_artefact', 'schema_violation', etc.
  detail       text,
  resolved     integer not null default 0,
  created_at   text not null default (datetime('now'))
);

-- ─── MANUAL OVERRIDES ────────────────────────────────────────────────
-- Human corrections to extracted prompts. Original `questions.prompt_md` is preserved;
-- the UI/PDF renderer prefers the override when present.

create table if not exists question_overrides (
  question_id    text primary key references questions(id) on delete cascade,
  prompt_md      text,
  mc_options_md  text,
  mc_correct     text,
  note           text,
  edited_at      text not null default (datetime('now'))
);

-- ─── EXTRACTION LOG ──────────────────────────────────────────────────
-- Append-only record of every API call. Used for spend tracking and for answering
-- "did anything about how X was extracted change?" after the fact.

create table if not exists extraction_log (
  id              integer primary key,
  ts              text not null default (datetime('now')),
  call_type       text not null,                    -- 'extract_question', 'extract_answer', 'tag', 'formula_sheet_check'
  source_id       integer references sources(id),
  page            integer,
  question_id     text references questions(id),
  model           text not null,
  prompt_hash     text,
  input_tokens    integer not null default 0,
  cached_tokens   integer not null default 0,
  output_tokens   integer not null default 0,
  cost_usd        real not null default 0.0,
  latency_ms      integer,
  ok              integer not null default 1,
  error_message   text
);

create index if not exists idx_extraction_log_source on extraction_log(source_id);
