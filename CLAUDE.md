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
