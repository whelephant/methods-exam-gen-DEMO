-- Add `section` to the questions unique constraint so Paper 2 Section A and Section B
-- can coexist without colliding on (source_id, question_number, part).
--
-- Without this, Section A's Q1 (whole MC, part=null) collides with Section B's Q1
-- (multi-part question's stem, part=null) — same (source_id=16, qnum=1, part=null).
--
-- SQLite doesn't support modifying unique constraints in place; we recreate the table
-- and migrate data. FKs from answers/question_tags/review_queue/question_overrides/
-- extraction_log reference questions.id (the text PK), which is preserved during the
-- swap, so cascade behaviour is intact afterwards.

pragma foreign_keys = off;

begin transaction;

create table questions_new (
  id                  text primary key,
  source_id           integer not null references sources(id),
  section             text check (section in ('A', 'B') or section is null),
  question_number     integer not null,
  part                text,
  marks               integer,
  prompt_md           text not null,
  is_mc               integer not null default 0,
  mc_options_md       text,
  mc_correct          text,
  has_diagram         integer not null default 0,
  diagram_path        text,
  source_page_start   integer not null,
  source_page_end     integer not null,
  source_bbox         text,
  extraction_model    text,
  extraction_run_id   integer,
  created_at          text not null default (datetime('now')),
  unique (source_id, section, question_number, part)
);

insert into questions_new
  (id, source_id, section, question_number, part, marks, prompt_md,
   is_mc, mc_options_md, mc_correct, has_diagram, diagram_path,
   source_page_start, source_page_end, source_bbox,
   extraction_model, extraction_run_id, created_at)
select
  id, source_id, section, question_number, part, marks, prompt_md,
  is_mc, mc_options_md, mc_correct, has_diagram, diagram_path,
  source_page_start, source_page_end, source_bbox,
  extraction_model, extraction_run_id, created_at
from questions;

drop table questions;
alter table questions_new rename to questions;

create index idx_questions_source on questions(source_id);

commit;

pragma foreign_keys = on;
