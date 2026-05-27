# Methods Exam Gen — context for Claude

A practice-exam generator for **VCE Mathematical Methods, Units 3 & 4**. The user picks an Area of Study (AoS) or a specific study-design dot point and the app assembles a PDF of real past VCAA questions with the matching examiner-report commentary as answers.

## Scope and shape of the corpus

- **Two papers per year:**
  - **Paper 1** — technology-free. 9 short-answer questions, 40 marks total, ~16–20 pages.
  - **Paper 2** — technology-active. Section A = 20 multiple-choice (1 mark each), Section B = 5 extended-response (12 marks each). ~28–32 pages.
- **Years covered: 2016–2025.** Both papers each year (40 PDFs total).
- **2024 redesign:** in 2024, VCAA moved to a modern, more accessible visual design. The 2024+ look is the visual target for the generated practice PDFs. Extraction logic is the same across eras (vector text, CID-encoded math).
- **Every exam ends with the Mathematical Methods formula reference sheet.** These pages must be **skipped** during extraction — they contain no questions. Detected by the literal text "Formula sheet" / "Mathematical Methods formulas". Skipped pages are recorded in `sources.skipped_pages` for auditability.

## Source layout

```
mathematical_methods/
├── exam_papers/                      # mathematical_methods_<year>_paper_<1|2>.pdf
├── examiner_reports/                 # .docx (2020+) and .pdf (2016–2019)
└── study_design/
    └── aos_dot_points_units_3_4.sql  # AoS + dot-point taxonomy (verbatim VCAA text, LaTeX-in-$...$)
```

**Examiner reports** contain final answers + commentary, **not full worked solutions**. We use the commentary verbatim as the "answer" in generated PDFs. No LLM-authored solutions in scope.

Answers are stored two ways per question (table `answers`):
- `answer_image_path`: a cropped PNG of the answer section as rendered via LibreOffice (docx → PDF → PyMuPDF crop). This is the **canonical answer artefact** — it preserves the worked math, marks distribution table, and commentary together. The math in VCAA reports is stored as MathType OLE objects (rendered to WMF inside the docx) which `python-docx` and pandoc both drop in plain-text extraction, so the image is the only complete representation without going through OCR.
- `commentary_md`: verbatim body-text prose extracted via `python-docx`. Useful for search/audit but **has gaps where math used to be** (e.g. *"giving the antiderivative of  as  or stating  instead of "*). Practice PDFs use the image, not this text. Vision-OCR to fill the gaps is a deferred upgrade (~$0.50/paper, ~$10/corpus on Claude Sonnet).

## Extraction notes (important)

Source PDFs are vector but use **CID font encoding** for math glyphs. Direct text extraction (`pdftotext`, naive `pdfplumber.extract_text`) returns garbled artefacts like `(cid:30)` for `−` and `(cid:27)` for closing delimiters. **Do not rely on text-mode extraction for math content.** Use vision-based extraction via the Anthropic API instead.

## Tagging rules

- Every extracted question is tagged against the Study Design dot points.
- **Maximum 2 dot points per question**, with exactly one marked `is_primary = 1`. Secondary tag only when the question genuinely tests two distinct dot points.
- Questions the model cannot confidently tag go to a manual-review queue; no AoS-only fallback (otherwise the dot-point filter on the UI becomes unreliable).

## Output rules

- **Vector text, tables, and math** in the generated PDF (LaTeX → KaTeX).
- **Raster (PNG) inside diagram regions.** Diagrams are cropped from the source PDF at **300 DPI** and saved as PNG under `assets/diagrams/<question_id>.png`. We tried vector SVG — PyMuPDF emits `<text>` referencing the source's CID-encoded fonts and browsers render them as � replacement glyphs. `text_as_path=True` works but inflates files 5×. PNG at 300 DPI is visually indistinguishable at viewing/print zoom, no font edge cases, similar size. Cost is identical (same `find_diagram_bbox` vision call).
- Generated PDFs include an answer key (examiner commentary) on a new page after the questions.

## Secrets

`.env` contains `ANTHROPIC_API_KEY`. Already in `.gitignore`. Do not commit, do not echo to logs.

## Project layout (built incrementally)

```
.
├── CLAUDE.md                    # ← this file
├── pyproject.toml
├── data/methods.db              # SQLite (gitignored)
├── migrations/
│   ├── 001_schema.sql
│   └── 002_study_design.sql
├── pipeline/                    # offline ingest pipeline (the only API consumer)
│   ├── db.py
│   ├── spend.py
│   ├── extract_questions.py
│   ├── extract_diagrams.py
│   ├── extract_answers.py
│   ├── tag_questions.py
│   └── ingest.py
├── app/                         # FastAPI runtime ($0 API cost at runtime)
│   ├── main.py
│   ├── templates/
│   ├── static/
│   └── render.py
└── assets/diagrams/             # <question_id>.png at 300 DPI
```

## Build order

Quality-first; pause for human verification after every paper. See `/Users/danielgao/.claude/plans/i-am-trying-to-swirling-bunny.md` for the full staged plan and per-stage acceptance criteria.

## Models and cost expectations

**Sonnet 4.6 is the default for question + diagram extraction.** Originally we planned Haiku-first with auto-escalation, but Haiku silently miss-reads stacked fractions on this corpus (see Implementation gotchas below). Haiku is opt-in via `--cheap`; Opus 4.7 is opt-in escalation via `--include-opus`. Tagging is text-only on Sonnet 4.6 with a cached dot-point prefix.

Reference (2023 Paper 1, the first paper through the full pipeline):
- Question extraction: $0.46
- Diagram bbox detection (5 diagrams): $0.05
- **Total per paper: ~$0.51.** Whole-corpus projection: **~$11**. Per-paper iteration after a prompt fix: ~$0.50 (one Sonnet sweep).

## Implementation gotchas (learned while building the pipeline)

Durable lessons accumulated during Stage B. Read these before adding extraction logic or debugging quality issues — most failure modes here aren't obvious from first principles.

- **Sonnet 4.6 default, not Haiku 4.5.** Haiku silently miss-reads vertically-stacked fractions (integral upper bound `π/3` collapses to `π`; ditto `π/2`, `π/4`, etc.). The output passes JSON quality checks but is mathematically wrong. Stronger prompting and worked examples didn't fix it. Sonnet handles these correctly. Cost is ~3.5× higher; math correctness is non-negotiable.

- **Manual annotation is the canonical diagram source; vision is the fallback.** The user can draw diagram bboxes directly in the browser at `/admin/diagrams/{source_id}` (and the per-question editor at `/admin/diagrams/{sid}/q/{qid}`). Saved annotations write `source_bbox.method = "manual"` to the questions row; `pipeline.extract_diagrams.extract_one` checks this first and crops from the stored normalised bbox with zero API cost. Vision extraction only runs when no manual bbox is present.
  - **MC option panels also support manual annotation.** Q7-style MCs (stem diagram + 5 option diagrams) and Q15-style MCs (5 option diagrams, no stem) surface in the "To draw" bucket as 5 separate rows — one per option A–E. Editor URL is `/admin/diagrams/{sid}/q/{qid}/opt/{A|B|C|D|E}`; "Save & next" cycles A→B→C→D→E then returns to the list. Saves POST to `/admin/diagrams/save_option` and invoke `crop_option_from_manual_bbox`, which writes the bbox + cropped PNG into `mc_options_md[i]` with `bbox_source = "manual"`. The vision-derived option bbox is pre-loaded onto the canvas as an editable starting suggestion.
  - **JS cache-busting:** `app/static/diagram_annotator.js` is referenced with a `?v=N` query string in `admin_diagram_edit.html`. Bump the N whenever you change the JS — `--reload` only reloads Python, not static files, and a stale cached annotator will silently POST to the wrong endpoint (which is how saves can land in `source_bbox` instead of `mc_options_md`).
  - **List view buckets**: *To draw* (`has_diagram=1` and no manual bbox), *Audit warnings* (pages with high curve-drawing density but no flagged question — catches diagrams the model failed to flag), *Done* (manual bbox stored).
  - **Audit detection** (`pipeline.extract_diagrams.find_audit_pages`) filters chrome/sidebars/working-lines, drops pure-line drawings (table grids), and rejects drawings inside prose blocks (math fraction bars). Threshold: `AUDIT_DRAWING_THRESHOLD = 8` curve-like drawings on a page with no flagged question.
  - **False-positive controls in the UI**: a "no diagram" link on each To-draw row flips `has_diagram=0` (`POST /admin/diagrams/dismiss`). An audit row's "flag" button does the inverse — escalates a question into the To-draw bucket (`POST /admin/diagrams/flag`).
  - The vision-based path (below) stays as the fallback. Once every paper is annotated, it can be deleted.

- **Vision bboxes (fallback path) are systematically loose.** Claude identifies the right region but over-extends 30–100pt past the actual content. Always post-process. The refiner (`pipeline/extract_diagrams.refine_bbox`) uses **PyMuPDF vector drawings** as the primary signal (axes, curves, points, dashed lines, fills); text classification is a fallback when no drawings are present. Pipeline:
  1. **Drawings union.** Take all drawings on the page; drop chrome (full-content-area borders), sidebar fills, and "horizontal hairline, width > 60% of content" (= working-line answer rules). Drawings whose centre falls inside a prose block (see (2)) are also excluded — those are math typesetting strokes (fraction bars, underlines on `\mathbf`) that LOOK like drawings but belong to question text.
  2. **Prose detection via `page.get_text("blocks")`.** Blocks group paragraph-level text even when CID-encoded math spans break PyMuPDF's "lines". A block with `>25` chars total OR `>250pt` horizontal width is prose. Identified prose bands gate-keep drawings AND clip the refined y-range above/below.
  3. **Label expansion.** After computing the drawings-union bbox, expand by short text labels (≤ 40 chars) within ±12pt vertically and ±80pt horizontally — pulls in axis names ("t (weeks)"), tick numbers, point coordinates ("(20, 700)"). Question text never qualifies because the prose-block clip strictly bounds the y-range.
  4. **MC stem-diagram clipping.** When a question is MC with diagram options (Q7-style: a stem `f(x)` graph above 5 option panels), the refiner accepts a `y_upper_bound` from the caller. `extract_one` computes this from the topmost option letter's y position via `_find_option_label_anchors` and passes it in, so the stem-diagram bbox cannot spill into the option-panel region below.
  5. **Sidebar clamp** to `[SIDEBAR_X_END_LEFT+3, SIDEBAR_X_START_RIGHT-3]`.

  The legacy text-classification refiner (long-vs-short y-row heuristics) is now `_refine_bbox_text_fallback` — only used if drawings-union returns no candidate drawings (rare; rasterised embedded images).

- **Continuation pages systematically lose the question number.** When a page lacks a visible `Question N — continued` header, the model either returns `question_number = null` or fabricates a wrong number (forward jump past the next sequential question, or a backward jump to an earlier one). Stitching (`pipeline/extract_questions._stitch`) enforces a **monotonicity invariant**: question numbers can stay or increase by ≤ 1 — never decrease, never skip past `max_seen + 1`. Violations get auto-corrected to `last_open_qnum` and logged to `review_queue` with the reason. On 2023 P1 this caught Q7.d misnumbered as Q9.d (forward skip) and Q8.c as Q7.c (backwards).

- **Monotonicity resets at every section transition.** Paper 2 has Section A numbered Q1..Q20 and Section B independently numbered Q1..Q5 — Section B's Q1 is NOT a "backwards jump" from Section A's Q20. The stitcher tracks `last_section`; when the model emits a fragment with a new section, `last_open_qnum` and `max_qnum_seen` reset. On 2022 P2 this fix recovered all 30 Section B fragments that were otherwise being mass-mislabelled as `Q20` (continuations of Section A's last question). Without this, the stitcher's "backwards jump = corrupted continuation" rule fires for every real Section B question.

- **Model omits `section` on continuation pages.** Page banners like "SECTION B – continued" don't reliably get into the `section` field — the model treats it as optional when not visibly fresh. The stitcher fills it in from `last_section` so every Section B continuation row carries `section='B'`. Without this, page-25 fragments of 2022 P2 Q5.d/e came back `section=null` and the renderer treated them as a separate non-sectioned question.

- **Redacted questions carry `marks=null`.** VCAA's Independent Review (post-2022) withdrew specific questions from existing papers — the question text is replaced with a redaction notice ("This question has been redacted following the findings of the Independent Review into the VCAA's Examination-Setting Policies..."). Affected questions in scope: 2022 P2 Q4.e.ii. Quality-check (`pipeline/extract_questions.quality_check`) detects "has been redacted" in `prompt_md` and waives the "marks required on complete part" check. The redaction notice is preserved in `prompt_md` so the practice paper documents the absence; tagging skips redacted leaves automatically (they have no real content to tag).

- **`fragment_kind="whole"` with `part="a"` is contradictory.** The schema permits it but the invariant doesn't: within one `question_number`, fragments are either one `"whole"` alone, or any combination of `"stem"` + `"part"`s. Mixed kinds fail the quality check and escalate to the next tier.

- **One diagram per stem/sub_stem fragment** (Section B quirk). Section B questions commonly introduce two diagrams in their preamble *before* part `a` — e.g. 2021 P2 Q1 page 10 has a flat cardboard sheet diagram, then "The sides of this sheet of cardboard are then folded up..." with a folded-box diagram, all before part a. The model's default behaviour is to bundle BOTH diagrams into a single stem fragment — which means only one of them gets a bbox row and the second is silently dropped. The extraction prompt (`pipeline/extract_questions.py` §3a) now instructs: a `stem` or `sub_stem` references AT MOST ONE diagram; if there are two "shown below"/"shown above" phrases before the first sub-part, split into a `stem` (first preamble + first diagram) plus a `sub_stem` with `before_part = "a"` (second preamble + second diagram), each with `has_diagram=true`. Manual surgery to fix a missed split: shrink the stem's `prompt_md`, insert a `2021-p2-q1-pre-a` row (or whatever the qid would be) with `has_diagram=1` and the second preamble. The annotation UI surfaces both as separate "To draw" entries.

- **Lettered preamble misclassified as a "part"** (Paper 1 quirk). When a printed page has e.g. `c. For 0 < q ≤ 1, let P' be ... [DIAGRAM] Let g be the function ... in terms of θ.` followed only by sub-questions `i.` (2 marks) and `ii.` (2 marks) — with NO question text after `c.` that asks for an answer before `i.` — the lettered `c.` is a **sub_stem** (`part="pre-c.i"`, `marks=null`, `has_diagram=true`), not a 4-mark part. The model regularly misreads this as a part because the `c.` label looks like the start of a normal sub-part. Observed on 2021 P1 Q9.c. The extraction prompt (`pipeline/extract_questions.py` §3a example 4) now teaches the rule: if a lettered label is followed only by roman-numbered sub-questions, it's a sub_stem. A post-extraction safety net (`extract_paper`) scans for parts whose marks equal the sum of their `.i/.ii` children and writes a `possible_misclassified_substem` row to `review_queue`. Manual surgery to fix in DB: `update questions set part='pre-X.i', marks=null where id=...` and delete the stale tags on the bad row from `question_tags`.

- **Always delete-before-insert per `source_id`.** Re-runs after correcting a model mistake leave stale rows under the prior (wrong) deterministic ID. `insert_questions` wipes `questions where source_id = ?` inside the same transaction as the new inserts.

- **Cache invalidation rules.** Per-page JSON cache is keyed on `prompt_hash = sha256(SYSTEM_PROMPT + tool schema + tier list)`. Changing those rebuilds the cache (and re-pays for extraction). Pure post-processing changes (stitch logic, quality checks not in the hash, diagram rendering) **don't** invalidate — re-running re-stitches from cache for free. This is intentional: lets us iterate stitch/render logic cheaply, while big prompt changes correctly force a fresh pass.

- **Don't include `(cid:NN)` artefacts in extracted prompts.** Source PDFs use CID-encoded fonts where the same glyph appears as `(cid:30)`, `(cid:27)`, etc. in linearised text. Vision-based extraction sidesteps this. Quality check fails any prompt containing `(cid:N)` and escalates.

- **Two-pass diagram extraction.** Step 1: vision call returns a loose normalised bbox. Step 2 (free, no API): heuristic refinement using PyMuPDF text + drawing layout tightens it. Both bboxes are persisted to `questions.source_bbox` as JSON `{page, bbox_pdf, bbox_pdf_model, refine}` for audit.

- **Tagger bias: AoS 3.10 over AoS 3.8 (FTC).** When a question literally computes a definite integral via $\int_a^b f(x)\,dx = F(b) - F(a)$ but then uses the result for something else (e.g. solving an equation, finding a value), the tagger often picks the broad catch-all dot point **AoS 3.10** ("application of integration... area / function from rate of change / average value / other situations") as primary instead of the specific **AoS 3.8** (FTC). Observed on 2023 P1 Q5.b — overridden manually. When tightening the tagger prompt later, add explicit guidance: "prefer the most specific applicable dot point; 3.10 is for area / function-from-rate / average-value problems, NOT routine evaluation of definite integrals which is 3.8". Manual overrides go via `question_tags` (set `tagged_by = 'manual'`) and a resolved `review_queue` row with reason `manual_tag_override` for the audit trail.

- **Answer extraction (docx path) uses LibreOffice + python-docx, not pandoc.** Pandoc 2.x drops MathType OLE objects on docx → markdown / HTML conversion. Workflow: `soffice --headless --convert-to pdf` renders the docx faithfully (with math), then PyMuPDF crops each "Question Xa." section into a 200 DPI PNG. python-docx separately extracts body-text commentary. Three docx quirks to know about: (a) some "Question Xa." headings have style `VCAA body` not `VCAA Heading 4` — match by regex on text content, not style; (b) prose paragraphs sometimes use the `VCAA math equations` style — accept any non-trivial (≥25 char) paragraph; (c) prose extraction has gaps where math used to be (e.g. "antiderivative of  as ") — that's accepted because the rendered image is the canonical answer artefact. The pipeline is fully local, $0 API.

- **PDF examiner reports** (2016–2019). Before 2020, VCAA published reports as PDFs (`.pdf`), not docx. `extract_paper_answers` branches on `report_path.suffix`: docx → soffice convert → existing path; PDF → use `pdf_commentary_by_question` and `extract_mc_answers_from_pdf` directly on the report PDF. Section cropping logic (`find_headings`, `crop_section`, `section_ranges`) is format-agnostic and reused unchanged. Commentary text is extracted via PyMuPDF `get_text("blocks")` and has the same CID-math gaps as the docx path — image is canonical.
  - **MC answer table parsing for PDFs is fiddly.** Three real quirks: (a) Each `% A` / `% B` header spans 3 sub-columns in PyMuPDF's extraction; the actual percentage value can be in any of them, so `extract_mc_answers_from_pdf` scans the header's span for the first non-empty cell. (b) Some Q-rows split across 2 PyMuPDF rows when the Comments cell has multi-line content — merge rows where the Question column is empty into the prior Q-numbered row. (c) Header column count differs across pages (sub-cell merging is inconsistent), so multi-page tables use `_realign_to` to remap a continuation page's rows onto the chosen_table's column positions via named-header anchors. The 2019 P2 header is split across 2 rows because of wrapping in `% No\nanswer`, handled by scanning the first 3 rows for the header.
  - **MC correct answer via highest-percentage heuristic.** Unlike docx tables which have a "Correct Answer" column, PDF tables only mark the correct answer with cell shading (lost in text extraction). The parser picks the option with the highest percentage; when the top two are within 10pp, a `mc_correct_low_margin` row is logged to `review_queue` for manual audit. Across all 4 P2 papers 2016–2019 this fires ~16 times total; user audits those by hand against the PDF.

- **Examiner-report sub-step labels mismatch the paper** (2020+ quirk). Some years (notably 2020) subdivide a single paper-part into answer-step headings: a paper that has `Q1.b` as a single 1-mark part is answered in the report under `Question 1b.i.` + `Question 1b.ii.` + sometimes more. There are no `b.i / b.ii` sub-parts on the paper itself — these are answer-explanation steps. The answer resolver (`pipeline/extract_answers.resolve_question_id`) handles this by progressively stripping the trailing dot-component until a match is found (`b.i.iv` → `b.i` → `b`). The orchestrator then merges consecutive sections that resolved to the same `qid`, producing ONE combined image + concatenated commentary per real paper-part. Without this, sub-step headings either skip entirely or clobber each other under the unique-key constraint on `answers.question_id`. Observed first on 2020 P2: 33 of 45 docx sections used sub-step labels.

- **Practice-paper PDF rendering uses Playwright + Chromium, fed file:// URIs for assets.** `app/render.py` writes the rendered Jinja HTML to a temp file and tells Playwright `page.goto(html_path.as_uri())`. Diagram and answer images are referenced by absolute `file://` URIs via the `abs_asset` Jinja filter — relative paths don't resolve under the temp-file root. KaTeX auto-render runs in the page; we wait on a `window.__katexReady === true` flag set by the script's `onload`, with a best-effort 8s timeout (`try/except`) so a missed CDN load can't hang the pipeline indefinitely.

- **PDF layout gotchas (Chromium print).** (a) **Never use negative `margin-left` / `margin-right` to bleed banners to the page edge** — Chromium's print area respects the `page.pdf(margin=...)` setting, so negative margins push content into the invisible region and clip text (we lost the "V" of "VCE..." this way). Put colored bands inside the content area instead. (b) **Avoid `height: 100vh` on the cover** combined with `page-break-after: always` — the cover's full-viewport height pushes the break onto the *next* page, creating a blank trailing page before content starts. Let the cover flow naturally; the page-break still fires. (c) Cover meta-counts: `Total questions` should use `len(groups)` (top-level question numbers, e.g. 9 for a Paper 1) and subtitle "N questions" should match — using `len(qids)` (21 leaf parts) on the subtitle is confusing.

- **Working-lines convention** (`app/templates/practice_paper.html`). Real exam papers signal *expected working depth* via lines-per-mark. We render **`marks × 3` faint horizontal lines at 10 mm spacing** per sub-part, spanning the full content width. **Skip lines** for (a) MC questions, (b) sketch questions (prompt contains `sketch`, `on the axes`, or `draw the`), (c) fill-in-the-bullets parts (markdown `- ` lines or "complete a/the …" phrasing), and (d) fill-in-the-table parts (markdown `|` table syntax in the prompt). For (c)/(d), the answer space IS the bullets/table.

- **Q-id format and section in question identity.** Paper 2 has two sections that re-use question numbers — Section A Q1 and Section B Q1 of the same paper are different questions. Section is therefore part of the row's identity:
  - Section A IDs are prefixed: `2023-p2-sA-q15`. Section B and Paper 1 keep the legacy form `<year>-p<paper>-q<n>[-<part>]` to avoid migrating existing answers / tags / diagrams.
  - DB unique constraint is `(source_id, section, question_number, part)` (migration 004).
  - **Any query that joins or filters by `(source_id, question_number)` MUST also filter by `section`.** Without this, Section A whole MCs get mis-classified as Section B stems because Section B's same-numbered question has children. All affected sites in `pipeline/tag_questions.py` and `app/render.py` / `app/main.py` are section-aware as of the MC support landing — check carefully if you add a new query.

- **MC option schema** (`mc_options_md` JSON column). Each entry is a dict `{"kind": "text"|"diagram", ...}`. Text entries have `"md"` with LaTeX-aware markdown. Diagram entries have a normalised `"bbox"` (4-tuple in 0..1) from the question extractor and a `"path"` filled in later by `pipeline.extract_diagrams.extract_mc_option_diagrams`. Legacy string-array form is auto-normalised to the text-kind dict shape on insert.

- **2024+ MC questions have 4 options, not 5.** From the 2024 visual redesign onward, Section A MCs are A–D (the E option was dropped). The tool schema (`pipeline/extract_questions.py`) accepts `minItems=4, maxItems=5`; the quality check accepts either count; `_find_option_label_anchors` takes an `n_options` parameter (defaulted to 5 but threaded from `len(opts)` at callsites). The model is told in §3 of the prompt that 2016–2023 have 5 options and 2024+ have 4, and must emit the actual count — never pad with empties. A 2024 paper emitting a 5th option means the model hallucinated; quality check should fail it.

- **MC diagram-option bboxes use PDF text-layer anchors, not vision bboxes.** Vision-derived option bboxes drift ~5–10% on y for small option panels (caught on 2023 P2 §A Q15: option-A crop captured the entire C panel below it). `pipeline.extract_diagrams._find_option_label_anchors` finds the literal `A.` / `B.` / ... / `E.` text spans on the source page and uses their positions to bracket each panel exactly. Panel right edge is set by the sibling letter's x in the next column (NOT a column midpoint — panels span all the way to the next column's letter). The vision bbox is still useful as a coarse y-disambiguator when multiple A-E groups appear on the same page (e.g. 2023 P2 §A page 7 has both Q14 and Q15 options).

- **Diagram-options MC: `has_diagram` depends on whether the question ALSO has a stem-level diagram.** Two sub-cases the suppression logic in `insert_questions` distinguishes by scanning `prompt_md` for "shown below" / "shown above" / "graph below" / "diagram below" / "figure below":
  - **Q15-style** (2023 P2 §A Q15: just diagram options, prompt is text only): `has_diagram=0`. Without this the paper-level extractor would crop ONE diagram for the question and arbitrarily pick a single option panel.
  - **Q7-style** (2022 P2 §A Q7: prompt says "the graph of y=f(x) is **shown below**", followed by a stem diagram, then 5 option diagrams): `has_diagram=1`, the stem `f(x)` graph gets cropped, and the refiner's `y_upper_bound` clips the crop to ABOVE the topmost option letter so it doesn't spill into the option panels.

  The model's own `has_diagram` flag is unreliable here — it sometimes sets it for the option panels alone (false positive for stem) and sometimes misses a real stem diagram (false negative). The "shown below" phrasing heuristic is the durable signal.

- **Pseudocode in MC questions (2023+ algorithms content)** is captured as a fenced markdown code block in `prompt_md`. The `←` assignment arrow is preserved as a literal. Rendered with monospace + light gray background by `pre`/`code` CSS in the template. `markdown-it-py`'s `commonmark` preset already supports fenced code blocks; we only had to opt into `table` for the Newton's-method fill-in tables.

- **Markdown rendering uses markdown-it-py with math sentinels.** `app/render._render_md` stashes `$..$` / `$$..$$` / `\(..\)` / `\[..\]` regions behind `@@MATHn@@` placeholders before running markdown-it (which would otherwise interpret `_` inside math as italics, and strip `\_` escapes). After HTML rendering, the placeholders are swapped back so KaTeX can pick them up in the browser. **Don't use `\x00` as the sentinel** — CommonMark spec replaces null bytes with U+FFFD, which breaks the round-trip.

- **Section-aware delete-before-insert in `pipeline/extract_questions.insert_questions`.** Only deletes rows in the sections present in the fragment batch, so re-extracting just Section A pages doesn't wipe Section B. Sections are tracked via `_section` on each fragment (set during stitching from the model's `section` field).

## Multi-subject support (Specialist Mathematics)

The repo now ingests **both** VCE Mathematical Methods AND VCE Specialist Mathematics (Units 3 & 4) under the "VCE Hub" brand, sharing one SQLite database. The `SUBJECTS` registry in `pipeline/db.py` is the source of truth for per-subject metadata: display name, exam-papers dir, examiner-reports dir, PDF filename glob, `id_prefix` for questions.id, running-header pattern (e.g. `MATHMETH EXAM` vs `SPECMATH EXAM`), and formula-sheet heading text.

- **Schema**: `subject` column on `study_areas`, `study_points`, `question_tags` (migration 001), `sources` + `questions` (migration 005). All queries that filter by `(year, paper)` must also filter by `subject`.
- **Question ID prefix**: Methods IDs stay unprefixed for legacy continuity (`2023-p1-q3-a`); Specialist IDs prepend `sp-` (`sp-2023-p1-q3-a`). Without the prefix the text-PK `questions.id` would collide.
- **Cache layout**: `pipeline/cache/<subject>/<year>_p<paper>_page<NNN>.json`. A fallback read for Methods checks the legacy flat layout (`pipeline/cache/<year>_p<paper>_page<NNN>.json`) so pre-multi-subject cache files stay valid byte-for-byte (their `_prompt_hash` still matches because the rendered Methods prompt is byte-identical to the pre-refactor `SYSTEM_PROMPT`).
- **CLI**: every pipeline module accepts `--subject` (defaults to `mathematical_methods` for backwards compat). E.g. `python -m pipeline.extract_questions --subject specialist_mathematics --year 2023 --paper 1`.
- **Page-width quirk (matters for diagram cropping)**: Methods 2019+ question pages use a custom 623.62pt-wide page; **everything else** (all Specialist 2016–2025, Methods 2016–2018, and the formula-sheet pages on every modern Methods paper) is standard A4 at 595.28pt. Sidebar constants dispatch on `page.rect.width` via `_sidebar_x_for()` in `pipeline/extract_diagrams.py` — `(50, 580)` for ~623pt, `(50, 568)` for ~595pt. Not subject-based; width-based.

### Specialist Study Design taxonomy

Source: VCAA "VCE Mathematics Study Design 2023" (Updated v1.1), pages 111–117 (Units 3 & 4: Specialist Mathematics section). Seeded by `migrations/006_specialist_study_design.sql`. Six AoSes, 58 tagable dot points + 13 sub-headings.

The original `specialist_mathematics/study_design/aos_dot_points_units_3_4.sql` provided in the repo had unrenderable LaTeX bugs (truncated bullets, `$a 0$` instead of `$a > 0$`, unbalanced parens). **Do not use it.** Migration 006 was regenerated via Claude Sonnet 4.6 vision OCR of the rendered PDF, with every `$...$` block KaTeX-validated for balanced braces and parens before being committed.

### Pre-2023 Study Design change and the scope-triage workflow

VCAA revised the Specialist Mathematics Study Design between 2022 and 2023. Pre-2023 Specialist papers test topics that are no longer in scope under the 2023+ design — confirmed:

- **Mechanics**: Newton's laws (F = ma applied to derive acceleration from forces), forces on inclined planes, free-body diagrams, friction coefficients, tension in strings, equilibrium of forces, weight as a gravitational-force calculation, normal reaction forces, mass-on-pulley systems
- **Statics**: rigid-body equilibrium, moments of force / torque, centre of mass calculations

These are NOT in the 2023+ catalogue and won't ever match a dot point cleanly. **The tagger reliably force-fits them** — confirmed on Specialist 2022 P1 Q5 (body on smooth inclined plane requiring Newton's second law) which the tagger mapped to AoS 4 dot 23 ("rectilinear motion via differential equations") at confidence 0.85–0.90. The model can't tell that the surface description ("body moves in a straight line, find the speed after 2 seconds") looks compatible with the in-scope dot point while the actual mathematical operation (Newton's laws on an inclined plane) is out of scope.

**Mitigation**: manual scope triage via an admin UI. The user (curriculum expert) flags out-of-scope questions per source paper after extraction completes; the pipeline does NOT attempt to auto-detect.

- Schema: `questions.out_of_scope` (boolean, default 0) added by `migrations/007_questions_out_of_scope.sql`
- Helper: `mark_out_of_scope(question_id, source_id, reason)` in `pipeline/tag_questions.py` sets the flag, deletes any existing tags, and writes a `review_queue` row with reason `out_of_scope_under_current_design`
- Filter: `/generate.pdf` always filters `q.out_of_scope = 0`; out-of-scope leaves never appear in generated practice papers regardless of how they were flagged
- Admin UI (TODO at time of writing): `/admin/scope/{sid}` list view per source with "In scope" / "Out of scope" buckets and per-row toggle (mirrors `/admin/diagrams/{sid}` pattern). Per-question toggle on `/admin/question/{qid}` for spot fixes. See `~/.claude/plans/plan-groovy-wombat.md` for the detailed UI plan.

### Specialist ingestion status (as of latest session)

| Year | P1 | P2 | Diagrams annotated | PDF generated | Notes |
|---|---|---|---|---|---|
| 2025 | ✓ | ✓ | ✓ | ✓ | $1.24 total. Q3.e answer manually inserted (heading scan miss) |
| 2024 | ✓ | ✓ | ✓ | ✓ | $1.56 total. Q2/Q3 boundary surgery on P2 page 13 (Argand parts mis-labelled as Q3) |
| 2023 | ✓ | ✓ | ✓ | ✓ | $1.85 total. Canary paper; baseline for the pipeline |
| 2022 | ✓ | ✓ | ✓ | ✓ | P1 Q5.a+Q5.b out-of-scope (mechanics); Q3.b redacted. P2 Q19 redacted, Q6.f low confidence tag, Q20+Q5 out-of-scope (mechanics). Both PDFs generated. |
| 2021 | ✓ | ✓ | ✓ | ✓ | P1: 22q, 22/22 tagged. P2: 53q, 52/53 tagged (1 review). Both scope triaged + diagrams done. PDFs generated. |
| 2020 | ✓ | ✓ | ✓ | ✓ | P1: 21q, 16/16 tagged. P2: 58q (surgery: Q3 g(x) parts moved from Q4, Q4.d drone q inserted), 49/49 tagged. MC fix: 2020 docx has no "Correct answer" column — added highest-% fallback. Q5.P2 mechanics out-of-scope. PDFs generated. |
| 2019 | ✓ | ✓ | ✓ | ✓ | P1: 21q, 18/18 tagged. P2: 64q, 54/54 tagged, 20/20 MC. Scope triaged. PDFs generated. |
| 2018 | ✓ | ✓ | ✓ | ✓ | P1: 18q, 15/15 tagged. P2: 67q, 57/58 tagged (sA-q4 low confidence), Q4.d/e stitching fixed. 20/20 MC. Scope triaged. PDFs generated. |
| 2017 | ✓ | ✓ | ✓ | ✓ | P1: 16q, 14/14 tagged. P2: 63q, 55/55 tagged (q4-d manually tagged AoS 3 dot 3 — complex locus). 20/20 MC. Scope triaged. PDFs generated. |
| 2016 | ✓ | ✓ | ✓ | ✓ | P1: 18q, 15/15 tagged (Q1.a OOS statics). P2: 61q, 53/53 tagged. 20/20 MC. Scope triaged. PDFs generated. |

Cumulative Specialist spend: ~$12.42. Cumulative all-subject spend: ~$25.77.

### Durable bugs caught during 2024–2025 ingestion

These have been fixed; documented here so they don't regress:

- **`_MC_TABLE_HEADER_KEYS` required `% E`** — broke 4-option MC parsing for every 2024+ paper. Fixed by removing `% E` from the required keys (`pipeline/extract_answers.py`); the `pct_cols` loop at the call site already iterates A–E and skips missing letters. Retroactively repaired Methods 2024 P2 + 2025 P2 (both went from 0/20 to 20/20 `mc_correct`).
- **Split MC answer table** — Specialist 2025 P2 docx splits the 20-row MC answer table into TWO separate `<table>` elements (Q1–10, Q11–20), each with its own header row. `_find_section_a_mc_table` → `_find_section_a_mc_tables` (plural) now returns all matching tables; `extract_mc_answers_from_docx` iterates all of them.
- **Currency `$` in finance MC questions** — Sonnet emitted `$800` literally for currency amounts, producing an odd `$` count that failed the unbalanced-delimiter quality check. Fix is two-part: extraction prompt now explicitly instructs `\$` escaping for currency contexts (e.g. Q19 on Specialist 2023 P2), AND `quality_check` strips both `$$` (display math) and `\$` (escaped currency) before counting.
- **Q2/Q3 boundary mis-extraction (page-13 continuation)** — on Specialist 2024 P2 page 13 the model labelled the Q2 continuation parts (Argand circle / ray) as Q3 because the page lacked a visible "Question N" header. The stitcher's monotonicity guard only catches **backward** jumps, not forward skips. Fixed via one-off SQL surgery (rename 4 rows from `q3-*` to `q2-*`, insert the missed Q2.e). The pattern may recur on other split-question pages — watch for marks imbalance (Q2 totalling <10 marks, Q3 totalling >10).
- **Tagger `aos.maximum: 6`** — was previously hardcoded to 4 (Methods has 4 AoSes, Specialist has 6). Fixed in `TAG_TOOL` schema; per-subject validity is now enforced via the catalogue's `valid_keys` in `validate_tags()`.
- **`check_budget` compared against all-time cumulative spend** — `pipeline/spend.py`'s `check_budget(budget_usd)` was calling `total_spend()` which sums the entire `extraction_log` table. Once cumulative spend exceeds any per-run `--budget` value, every subsequent run fails on the first page. Fixed by adding a `baseline_usd` parameter; each entry-point (`extract_paper`, `extract_paper_diagrams`, `tag_paper`, `tag_one`) captures `total_spend()` at the start of the run and passes it as `baseline_usd`, so the guard only counts spend incurred during the current run.
- **`load_groups` in `app/render.py` re-includes out-of-scope siblings** — the renderer fetches all sibling rows for a question group (to get the stem + parts together), but the original query had no `out_of_scope` filter. Result: a redacted/out-of-scope part (e.g. Q3.b) correctly excluded from `qids` by the generate route would be added back as a sibling. Fixed by adding `and q.out_of_scope = 0` to the siblings query in `load_groups`.
- **Stitcher collapses MC questions when a redacted question creates a number gap** — the monotonicity invariant ("qnum must not skip past max_seen+1") fired on Section A `mc`-kind fragments when a redacted question was skipped by the model (e.g. Q4 redacted → model returns Q3 then Q5 → stitcher treats 3→5 as a forward skip and collapses Q5–Q20 into Q3). Fix: the forward-skip check is now bypassed for `fragment_kind in ("whole", "mc")` — these are always single-page and cannot be mis-numbered continuation pages; a skip simply means a redacted question left a gap.
- **Specialist 2020 docx MC table has no "Correct answer" column** — only `% A`–`% E` columns; correct answer is indicated by cell shading (unrecoverable from text). `_MC_TABLE_HEADER_KEYS` required "Correct answer", so the table was silently skipped (0/20 MC correct). Fixed by removing "Correct answer" from the required keys and adding a highest-% fallback in `extract_mc_answers_from_docx` (same logic the PDF path already uses). Low-margin cases (<10pp gap) are flagged to `review_queue`. This format applies to all Specialist docx reports that predate the "Correct answer" column — check each year.
- **Q3/Q4 stitching error on Specialist 2020 P2** — page 17 (Q3's g(x) continuation parts d, e.i, e.ii) was mis-labelled as Q4 by the model; page 20's Q4.d (drone contact, 3 marks) was mis-labelled as Q5.d and overwritten by Q5 mechanics. Result: Q3 had only 6/10 marks, Q4 had 15 marks with g(x) content, Q4.d was the wrong question. Fixed by SQL surgery: renamed Q4.pre-d/d/e.i/e.ii → Q3.*; manually inserted missing Q4.d. Watch for this pattern on other pre-2023 papers with multi-topic Section B questions spanning page breaks.
