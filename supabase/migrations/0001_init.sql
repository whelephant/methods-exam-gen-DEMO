-- VCE Methods Exam Gen — public read-only schema for the Vercel/Supabase demo.
--
-- This is the Postgres port of the SQLite schema in /migrations/00{1..4}_*.sql,
-- limited to the tables the public demo needs. Admin-only tables
-- (extraction_log, review_queue, question_overrides) stay local in SQLite;
-- question_overrides are applied at sync time, so the rows here already reflect
-- the corrected prompt_md / mc_options_md / mc_correct.
--
-- All tables have RLS enabled with a SELECT-only policy for `anon`. The
-- service_role bypasses RLS and is used by pipeline/sync_to_supabase.py to
-- write rows. No client-side writes are possible.

-- ─── EXTENSIONS ──────────────────────────────────────────────────────

-- (none needed — jsonb, timestamptz, etc. are built in)

-- ─── STUDY DESIGN ────────────────────────────────────────────────────

create table study_areas (
  subject text not null,
  aos     integer not null,
  title   text not null,
  intro   text not null,
  primary key (subject, aos)
);

create table study_points (
  subject     text not null,
  aos         integer not null,
  is_header   boolean not null default false,         -- true = sub-heading, not a tagable dot point
  text        text not null,
  sort_order  integer not null,
  primary key (subject, aos, sort_order),
  foreign key (subject, aos) references study_areas(subject, aos)
);

-- ─── SOURCES ─────────────────────────────────────────────────────────

create table sources (
  id            integer primary key,                   -- preserved from local SQLite (no auto-sequence)
  year          integer not null,
  paper         integer not null check (paper in (1, 2)),
  format_era    text not null check (format_era in ('legacy', 'modern_2024')),
  skipped_pages jsonb,                                 -- int array (formula-sheet pages, etc.)
  unique (year, paper)
);

-- pdf_path / report_path from the SQLite schema are dropped here — those are
-- filesystem paths in the local authoring environment and have no meaning in
-- the public demo.

-- ─── QUESTIONS ───────────────────────────────────────────────────────

create table questions (
  id                  text primary key,                -- '<year>-p<paper>-q<n>[-<part>]' or sectioned variant
  source_id           integer not null references sources(id),
  section             text check (section in ('A', 'B')),
  question_number     integer not null,
  part                text,
  marks               integer,
  prompt_md           text not null,                   -- KaTeX-flavoured markdown, with question_overrides applied
  is_mc               boolean not null default false,
  mc_options_md       jsonb,                           -- array of option dicts (see CLAUDE.md MC schema)
  mc_correct          text,
  has_diagram         boolean not null default false,
  diagram_path        text,                            -- Storage-relative, e.g. 'diagrams/2023-p1-q3-a.png'
  source_page_start   integer not null,
  source_page_end     integer not null,
  created_at          timestamptz not null default now(),
  unique (source_id, section, question_number, part)
);

create index idx_questions_source on questions(source_id);

-- ─── TAGS ────────────────────────────────────────────────────────────

create table question_tags (
  question_id           text not null references questions(id) on delete cascade,
  subject               text not null default 'mathematical_methods',
  aos                   integer not null,
  dot_point_sort_order  integer not null,
  is_primary            boolean not null default false,
  confidence            real,
  tagged_by             text,
  primary key (question_id, aos, dot_point_sort_order),
  foreign key (subject, aos, dot_point_sort_order) references study_points(subject, aos, sort_order)
);

create index idx_question_tags_aos on question_tags(aos, dot_point_sort_order);

-- "Exactly one primary tag per question" — enforced via partial unique index.
-- "Max 2 tags per question" — enforced upstream by the local sync script,
-- not in the DB (would require a trigger, low value for a read-only demo).
create unique index uq_question_tags_one_primary
  on question_tags(question_id) where is_primary;

-- ─── ANSWERS ─────────────────────────────────────────────────────────

create table answers (
  question_id        text primary key references questions(id) on delete cascade,
  final_answer_md    text,
  commentary_md      text not null,
  answer_image_path  text,                             -- Storage-relative, e.g. 'answers/2023-p1-q1-a.png'
  created_at         timestamptz not null default now()
);

-- source_report_path from the SQLite schema is dropped — local-only.

-- ─── RLS: public read-only ───────────────────────────────────────────

alter table study_areas enable row level security;
alter table study_points enable row level security;
alter table sources enable row level security;
alter table questions enable row level security;
alter table question_tags enable row level security;
alter table answers enable row level security;

create policy "anon read study_areas"    on study_areas    for select to anon using (true);
create policy "anon read study_points"   on study_points   for select to anon using (true);
create policy "anon read sources"        on sources        for select to anon using (true);
create policy "anon read questions"      on questions      for select to anon using (true);
create policy "anon read question_tags"  on question_tags  for select to anon using (true);
create policy "anon read answers"        on answers        for select to anon using (true);

-- service_role bypasses RLS by default (used by the sync script), so no
-- explicit write policies are needed. authenticated users get the same
-- SELECT-only access as anon by inheriting the anon policies via Supabase's
-- default role hierarchy — if we ever introduce per-user accounts we'll
-- revisit.
