-- Add `out_of_scope` flag to questions. Used to mark pre-2023 Specialist
-- questions that test topics removed from the 2023+ Study Design (mechanics
-- including Newton's laws / forces / friction / tension / equilibrium /
-- inclined planes; statics including rigid-body equilibrium / moments of
-- force / centre of mass).
--
-- The tagger now emits an `out_of_scope` signal in its tool output when a
-- question primarily tests a dropped topic; the orchestrator sets this column
-- and skips tag insertion. The practice-paper builder filters on this column
-- so out-of-scope questions never appear in generated papers.

alter table questions add column out_of_scope integer not null default 0;
