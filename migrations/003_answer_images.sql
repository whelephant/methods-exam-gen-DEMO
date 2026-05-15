-- Add raster snapshot path for examiner-report answer sections.
-- The math in VCAA reports is stored as MathType OLE objects (rendered to WMF
-- images), which python-docx and pandoc both drop in text extraction. To preserve
-- the worked-solution math faithfully we crop the rendered docx→PDF page region
-- as a PNG and reference it here. commentary_md still holds the verbatim body
-- text for vector rendering in the practice PDF.
--
-- Safe to re-run (column add only when missing — SQLite checks on schema apply
-- via "if not exists" pattern not supported, so this migration is one-shot.)

alter table answers add column answer_image_path text;
