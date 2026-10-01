# Specifications

Technical reference for developers and maintainers of **Photo Scanner v0.1.0**.
The repository name `photo_scanner_banana` and the internal `banana` package, CLI and environment variable prefix are a naming convention only; the product name shown to users is "Photo Scanner".
Each section is marked **[implemented]** or **[planned]**. Planned items follow the approved design and aren't in the code yet.

- [1. Scope](#1-scope)
- [2. Architecture](#2-architecture)
- [3. Data model](#3-data-model)
- [4. Ingest](#4-ingest)
- [5. Analysis](#5-analysis)
- [6. Dates](#6-dates)
- [7. Export contract (Immich)](#7-export-contract-immich)
- [8. HTTP API](#8-http-api)
- [9. Web UI](#9-web-ui)
- [10. CLI](#10-cli)
- [11. Configuration](#11-configuration)
- [12. Native module](#12-native-module)
- [13. Deployment](#13-deployment)
- [14. Testing](#14-testing)
- [15. Roadmap](#15-roadmap)

---

## 1. Scope

Offline pipeline for family photo scans from an Epson FF-680W:
pair fronts/backs → flag blank backs and rescans → human review → export files with EXIF + XMP metadata
into a folder tree that Immich imports as a **read-only External Library**. No Immich database access and no cloud services.

**Non-goals:** editing photos, face recognition (Immich does it), two-way sync from Immich back to the app, driving the scanner directly (the FF-680W has no eSCL/AirScan; scans arrive through the inbox).

---

## 2. Architecture

```
Windows PC + FF-680W ──SMB──► NAS photo_vault/inbox
                                   │
Ubuntu server (Docker, NVIDIA)     ▼
 ├─ banana-api     FastAPI + UI :8000 ──► SQLite (WAL) /srv/banana/banana.db
 ├─ banana-worker  job queue (GPU stages, planned)
 └─ ollama         optional VLM fallback (planned, compose profile "vlm")
                                   │ export
                                   ▼
NAS photo_vault/sorted ──(:ro mount)──► Immich External Library
```

### Code layout

| Path | Responsibility |
|---|---|
| `banana/config.py` | TOML settings (pydantic) |
| `banana/models.py`, `banana/db.py` | SQLModel tables, engine with WAL pragmas |
| `banana/jobs.py` | SQLite job queue, retries, worker loop **[implemented, no handlers registered yet]** |
| `banana/dates.py` | `PhotoDate` with precision |
| `banana/analysis/date_parse.py` | rule-based date extraction |
| `banana/core/` | per-image hot paths: C++ `banana_core` if importable, else NumPy `reference.py` |
| `banana/ingest/` | `pairing.py`, `service.py` (inbox → archive → DB), `thumbs.py` (preview cache) |
| `banana/export/` | `layout.py`, `metadata.py`, `exiftool_writer.py`, `exporter.py`, `service.py` |
| `banana/web/` | `api.py` (FastAPI), `static/` (UI) |
| `native/` | C++20 nanobind extension |
| `docker/` | image + compose |
| `scripts/` | `wsl_build.sh`, `make_demo_inbox.py` |

### Language split
Python handles orchestration and I/O. C++ (`banana_core`) handles per-pixel CPU work and releases the GIL.
Inference runs in ONNX Runtime and search in FAISS **[planned]**. Metadata goes through ExifTool, the same parser Immich uses.

---

## 3. Data model

SQLite, `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`. Tables are created on startup (`create_all`).
New model columns are added to existing databases automatically (`ALTER TABLE ADD COLUMN` with the column default).
Renames, type changes and drops aren't migrated.

### Scan status machine

```
ingested ─► analyzed ─► needs_review ─► approved ─► exported ─► stacked
                              │   ▲         │
                              ▼   └─────────┘  (UI can move between needs_review/approved/rejected)
                           rejected ─► (deleted, see below)
```
v0.1: ingest creates scans directly in `needs_review`. `ingested`, `analyzed` and `stacked` are reserved.

**Deleting a rejected scan [implemented]** (`DELETE /api/scans/{id}`): rejection itself never deletes anything -
originals stay in `archive/<batch>/` exactly like every other status, and the button (`#btn-delete`) only appears
once a scan is already `rejected`, never during normal review. Deletion is refused (409) unless **all** of: the
scan is `rejected`, it has no `export` row, and it has no `correction_event` rows (i.e. it was never approved,
even briefly, before being rejected - that history is training data and is never discarded). On success, the
`scan` row is removed and committed **first**, then the front/back/enhanced files are unlinked; a file that
can't be removed is listed in `files_kept`. If the commit fails, nothing on disk has changed.

`duplicate_group_id` isn't a real foreign key (`banana/models.py`), so before removal
`release_from_duplicate_group` (`banana/ingest/service.py`) re-points any other scan that named this one as
its duplicate-group anchor - to a promoted survivor if more than one remains, or clears it if this was the only
one, so nothing is ever left saying "possible rescan of #N" for an id that no longer exists. `banana
repair-duplicates` fixes any such reference left dangling from before this existed.

### Tables

**batch**: `id`, `name` (unique), `box_label`, `created_at`

**scan**
| Column | Type | Notes |
|---|---|---|
| `id` | int PK | global; becomes `SCAN_{id:06d}` |
| `batch_id` | FK batch | |
| `source_key` | str unique | scanner base name (`Attic3_0001`) or resolved path for manual imports |
| `front_path`, `front_enhanced_path`, `back_path` | str | absolute archive paths |
| `back_type` | str? | `blank` \| `content` (v0.1); planned: `watermark_only` \| `handwriting` \| `stamp` \| `sticker` |
| `keep_back` | bool | export `_B` file? |
| `dhash_hex` | str? | 16 hex chars (uint64 doesn't fit SQLite signed int) |
| `ocr_lines`, `ocr_engine` | JSON, str | reserved for TrOCR |
| `date_precision`, `date_year`, `date_month`, `date_day`, `date_season`, `date_circa`, `date_source` | | `PhotoDate` fields; source `manual` \| `back_ocr` \| `front_imprint` \| `batch` |
| `description` | str? | |
| `people`, `places`, `events`, `tags` | JSON list | |
| `status` | str | see status machine |
| `duplicate_group_id` | int? | id of the first scan in the similar group |
| `is_keeper` | bool | reserved for the duplicate resolver |
| `immich_duplicate_asset_id` | str? | **[implemented]** Immich asset id our dHash matched against, if any - separate from local `duplicate_group_id` |
| `operator_immich_duplicate` | bool | **[implemented]** operator's confirm/override of the Immich-duplicate suggestion |
| `created_at`, `updated_at` | datetime (UTC) | |

**job**: `id`, `type`, `scan_id`, `state` (`queued|running|done|failed`), `attempts` (max 3), `error`, timestamps

**export**: `scan_id` (unique), `front_rel`, `front_sha256`, `back_rel`, `back_sha256`, `exported_at`,
`front_sha1`, `back_sha1` **[implemented]** (SHA1 alongside the SHA256, for Immich's `bulk-upload-check`; not a
security hash - `usedforsecurity=False`), `immich_front_id`, `immich_back_id`, `immich_checked_at`
**[implemented]** (asset ids from an exact-checksum match, and when that check last ran; reset to `None` on
every re-export since the bytes - and therefore the checksums - changed), `stack_id` **[planned]**

**immich_setting** (singleton, `id=1`) **[implemented]**: `enabled`, `url`, `api_key`, `library_id`,
`import_path_prefix`, `updated_at` - overrides `config.toml`'s `[immich]` block once the operator has saved
anything through the UI. The API key is never returned by any endpoint; only `api_key_set` and its last 4
characters are.

**immich_asset_hash** (`asset_id` PK) **[implemented]**: `checksum`, `dhash_hex`, `fetched_at` - one cached
perceptual hash per Immich asset, keyed by Immich's own reported checksum so a re-run only re-fetches assets
that actually changed.

---

## 4. Ingest

**[implemented]** `banana.ingest.service.ingest_inbox`

1. List files in `paths.inbox` (non-recursive). A file modified within `ingest.settle_seconds` (default 10) is
   still being written: it is reported as `not_ready` and left for the next pass. Files the app placed itself
   (a finished scan run, a recovery) are passed as `trusted` and skip the wait.
2. **Pair** with `pairing.pattern` (case-insensitive), which must define a `base` group and an optional `suffix` group:
   - no suffix → `original` front, `_a` → `enhanced` front, `_b` → back
   - duplicate slots and non-matching names → `unmatched` (left in the inbox)
   - back without a front → `skipped` (left in the inbox)
3. Batch name `inbox-YYYYmmdd-HHMMSS` (made unique with `-2`, `-3`... when two ingests share a second).
   **One photo per transaction [implemented]:** each group is analysed (steps 5-6 and OCR) while still in the
   inbox, with only reads; then its files are moved to `archive/<batch>/` and its row committed at once. The
   write lock is held for milliseconds per photo. If the commit fails, that photo's files are moved back to the
   inbox, so the archive never holds a file without a row. A photo that can't be opened or decoded is reported
   as `failed` and retried on later passes; after `ingest.unreadable_after` attempts (3) or
   `ingest.unreadable_minutes` (10) it is moved to `inbox/_unreadable/` and listed in the System panel with
   **Retry unreadable files** (`POST /api/ingest/retry-unreadable`).
4. The front is chosen by `pairing.front_variant` (falling back to the other variant).
5. dHash of the front (decoded to ≤512 px). Blank metrics of the back (≤1024 px):
   `back_type = blank if edge_density < analysis.blank_edge_density`; `keep_back = back_type != blank`.
6. Rescan check: Hamming distance ≤ `analysis.dhash_max_distance` against **all** previously ingested scans
   (first match wins) → `duplicate_group_id`.
7. **Reused names [implemented]:** `scan.source_sha256` is the sha256 of the source front. A `source_key`
   that already exists (case-insensitive) with the **same** content → `skipped`, files moved to
   `inbox/_already_ingested/`. Same name, **different** content (scanner software restarted its numbering) →
   a new scan stored as `<base>~<hash8>`. Rows from before the column existed get their hash on first collision.

**One ingest at a time [implemented]** (`banana/ingest/runner.py`, `IngestRunner`): a finished scan, the
**Ingest inbox** button, scan recovery and the watcher all go through one lock; a second request waits instead
of overlapping. **Inbox watcher [implemented]:** polls the inbox every `ingest.watch_seconds` (15) and ingests
ready files (`ingest.watch_inbox`, default true; polling, so NFS-safe). `GET /api/ingest/status` reports the
watcher, the last result, unreadable files and **archived files with no scan** (checked at startup; reported,
never moved). The System panel shows these as the **Ingest** row (`warn` when something needs a person).

---

## 4a. Direct scanning (SANE)

**[implemented]** `banana/scanner/sane.py`

- **Device:** Epson FF-680W at `192.168.16.178` through SANE's built-in **`epsonds`** backend (`epsonds:net:<host>`, TCP 1865).
  Verified with sane-backends 1.2.1: `scanimage -d epsonds:net:192.168.16.178 -A`.
  TWAIN is Windows-only and isn't used on the Linux server. The scanner has no eSCL/AirScan.
- **Capabilities (SANE):** `--source ADF Front|ADF Duplex`, `--mode Lineart|Gray|Color`, 50–600 dpi, `--adf-crp`
  (auto crop), `--adf-skew`, `--eject`. Not available: FastFoto enhance and auto-rotate, 1200 dpi, long-page mode.
- **Run:** one scan at a time in a background thread:
  `scanimage -d <device> --source <source> --mode <mode> --resolution <dpi> --adf-crp=… --adf-skew=… --format=jpeg --batch=<inbox>/.scanning-<run>/page_%04d.jpg`.
  `<run>` = `scanYYYYmmddHHMMSS`. Ingest ignores the hidden staging folder (it only reads top-level files).
- **Placement:** pages are renamed in scan order with `os.replace`. Duplex: odd pages become `<run>_NNNN.jpg`, even pages `<run>_NNNN_b.jpg`.
  An odd page count leaves the last front without a back and adds a warning to the message.
- **Destination** (per scan; default `scanner.after_scan`):
  - `review`: after placement, `ingest_inbox` runs and the result is attached to the run (`ingest`).
  - `inbox`: files stay in the inbox until **Ingest inbox** / `POST /api/ingest`.
- **Errors:** no pages means the run failed. "Document feeder out of documents" becomes "No photos in the feeder…".
  The staging folder is removed unless pages were left in it.
- **Interrupted runs [implemented]** (B-27): if the TWAIN driver raises after handing over pages, the leftover
  `page_NNNN.bmp` is converted and every page is still placed and ingested; the run ends `done` with a `warning`
  saying what stopped the driver. Every run's start, outcome and any driver error are logged.
- **Stranded runs [implemented]:** `GET /api/scanner` lists `.scanning-*` folders that still hold files (`stranded`).
  `POST /api/scanner/recover` converts leftover BMPs, places the pages under the run's own name and ingests them;
  a page that can't be decoded is never deleted and is reported as `kept`. The same recovery runs on launch
  (`scanner.recover_on_start`, default true).
- **Visible until dismissed [implemented]:** a failed run, a run that finished with a warning, or stranded runs show
  a bar under the header (`#scan-alert`, `role="alert"`) with **Recover scans** and **Dismiss**. Dismissing hides
  that exact state; anything new about it shows the bar again.

### Observed FF-680W behavior through SANE (10-photo test, 2026-09-14)
- `--adf-crp=yes` is **ignored**: every page is the full 5096 × 9283 px (8.5 × 15.5 in @600 dpi). The photo sits at the top
  on a blue-gray backing (≈ RGB 195–220 / 200–224 / 210–231), and the page below the scanned length is padded with pure white.
- The **back is delivered first** in each duplex pair (photos loaded face down), so `scanner.first_side = "back"` (default).
- Content orientation depends on how each photo was placed; SANE applies no rotation and writes no EXIF orientation.

### Count
`count: "all"` scans until the feeder is empty, capped at `scanner.max_feeder_count` (default 36, the FF-680W ADF
hopper's measured capacity) photos - `--batch-count={max_feeder_count * (2 if duplex else 1)}`. This is a ceiling,
not a forced count: `scanimage`'s own out-of-documents detection still stops early on a smaller stack, so nothing
changes for a normal-sized batch. If a run hits the cap exactly, the finished message adds "feeder capped at N,
scan again for more" so the operator knows to run **Scan feeder** again for the rest of a larger stack rather than
overloading the hopper in one pass. `count: "one"` adds `--batch-count=2` (duplex) or `1`.

`ScanRun.pages` (polled by `GET /api/scanner/scan`) is a raw SANE page count - two pages per duplex photo. The
finished-scan message already converts this to photos (`photos = (pages + 1) // 2` when duplex); the **live**
"Scanning…" / "Processing…" status in the header does the same conversion client-side (`GET /api/scanner` now
also returns `duplex`), so a photo mid-scan is never counted as two while its back is still being read.

## 4c. Crop and rotation (non-destructive)

**[implemented]** `banana/imaging.py`, `photo_bbox` in `banana/core` (C++ `native/src/photo_bbox.cpp` + NumPy reference, bit-identical)

- **Detection** (`detect_crop`): decode at about 1/8 scale (JPEG DCT scaling), then `photo_bbox(rgb)`:
  1. `scan_end` = first row of the trailing block where ≥98% of pixels are ≥250 on all channels (padding).
  2. Backing pixel: `510 ≤ R+G+B ≤ 720`, `4 ≤ B−R ≤ 40`, channel spread ≤ 45.
  3. Photo rows/columns = those with <85% backing pixels (within `[0, scan_end)`). Take the longest run, bridging gaps ≤ 1/33 of the dimension.
  4. Rejected if smaller than 1% of either side. Result is scaled to full resolution and inset one detection pixel per side.
     A box covering ≥97% of the page is treated as "no crop" (`null`).
  Measured on real scans: 20/20 pages detected as 6.0 × 4.0 in (4×6 prints). C++ 0.56 ms per page vs Python 9.6 ms.
- **Storage:** `scan.front_crop` / `back_crop` = `[x0, y0, x1, y1]` in stored-pixel coordinates (end-exclusive).
  `front_rotation` / `back_rotation` ∈ {0, 90, 180, 270}, clockwise. Archive files are never modified.
- **Previews** apply crop then rotation (cache key includes both; `?raw=true` shows the unedited page).
- **Export** writes a full-resolution render (crop → rotate) as JPEG q95, 4:4:4, keeping dpi and ICC. The library file is always `.jpg` when an edit exists.
  Unedited scans are copied byte-for-byte as before.
- **Analysis** (dHash, blank-back metrics) runs on the cropped image.
- **Swap sides** exchanges paths, crops and rotations and re-runs the checks without re-detecting crops.
  **Re-analyze** re-detects crops and keeps rotations.
- **[implemented] Automatic rotation, front + back** (`banana/analysis/ocr/orientation.py`,
  `apply_orientation_suggestion` in `banana/ingest/service.py`) - see 4d-3. **[planned]** deskew.

## 4d-3. Auto-rotate (front + back come out upright)

**[implemented]** `banana/analysis/ocr/orientation.py`, `apply_orientation_suggestion` in `banana/ingest/service.py`

Front and back of one photo are captured in the **same duplex pass**: the back is mirrored left-right relative
to the front, not independently rotated, so whichever rotation makes the back's text read upright is, in the
ordinary case (a label written the same way up as the photo displays), also the front's correct rotation. One
detected angle is applied as a suggestion to **both** `front_rotation` and `back_rotation`. No back, a blank
back, or no legible text at any angle: no signal, and none is guessed - front-only orientation (faces, horizon)
is out of scope; there's no reliable offline signal for it.

**Why a single OCR-confidence score per rotation doesn't work** (measured, not assumed): RapidOCR (PP-OCR) is
already close to rotation-invariant for *reading* text - its detector finds a line wherever it's turned, a
hard-coded step in the library re-rotates any detected crop that comes out taller than wide before recognition,
and its angle classifier auto-corrects upside-down text within its own line. Scoring recognition confidence at
each of the 4 rotations gave near-identical scores at every angle in testing - genuinely no signal there.

**Two-phase search** (`orientation.search`), each phase requiring a clear margin or returning "no signal":
1. **Line shape** picks the axis. A detected line's bounding box (in the *un-rotated-by-RapidOCR* image
   coordinates) is wide when text runs normally along the candidate rotation, narrow when it's off by 90°- real
   lines are wide, not tall. Compares the two widest-scoring rotations against the two narrowest.
2. **Recognition with the angle classifier off** (`reader.read(image, use_cls=False)`) picks the direction
   within the winning axis. Without that auto-correction, recognition reads confidently right-side up and
   produces little to nothing upside down (measured: full recognition at the correct angle, nothing at its
   180° opposite).

Runs once per scan: skipped once anything (operator or this search) has already decided a rotation, so it never
overrides a manual choice, and **Swap sides** / **Re-analyze** keep their "rotations are kept" contract for
free. The search itself runs at `analysis.orientation_search_max_side` (default 300px, small and cheap - up to
6 OCR passes) before the real full-resolution OCR pass runs once at `analysis.ocr_max_side`.

Recorded exactly like a crop suggestion: `corrections.suggest(scan, "rotation_front"/"rotation_back", angle,
"back-ocr-orientation@1")`, and `front_rotation`/`back_rotation` are set directly (there's no separate
"suggested" preview field for an image's rotation - the preview renders whatever the field currently holds).
The Front/Back **auto-rotated** chip (`data-state="suggested"` while unedited) only shows when a rotation was
actually applied (angle 0 - already upright - has nothing to flag); the manual Rotate buttons are always the
override on either side, independently.

Config: `analysis.orientation_search` (default `true`), `analysis.orientation_search_max_side` (default `300`).

## 4d. Text on back (OCR)

**[implemented, printed text]** `banana/analysis/ocr/__init__.py`, `read_back_text` in `banana/ingest/service.py`

- **Engine:** `RapidOcrReader` = PP-OCR detection + angle classifier + recognition (ONNX Runtime, CPU, fully offline;
  `ocr` extra `rapidocr_onnxruntime`). A shared, lazily created instance serialized by a lock. Handles upside-down labels.
  About 1.2–2 s per back at `analysis.ocr_max_side = 1800`.
- **When:** on ingest and re-analyze, for scans whose back isn't blank (`analysis.read_text = true`), and on demand with
  `POST /api/scans/{id}/read-text` (409 without a back, 503 without an engine). Runs on the **cropped + rotated** back.
- **Stored:** `scan.ocr_lines = [{text, score, box: [[x, y] × 4]}]` (box in the edited preview frame), `scan.ocr_engine`.
- **Prefill, never overwriting user input:**
  - `description`, if empty: lines with score ≥ 0.8 whose non-space characters are at least half letters (and ≥3 letters),
    joined with newlines. This drops lab and negative codes such as `E66817567/67`.
  - date, if precision is `unknown`: `parse_best` over lines with score ≥ 0.5, `date_source = "back_ocr"`.
- **Date parser:** long month names (≥7 letters) one typo away are corrected before matching (`Januaru` → `january`; see 4d-2).
- **Real result** (scan #1 label): `JimmyDawley / Chocolat Design - Field Trip / Januaru 12, 2006` gave date 2006-01-12 and the description filled.
- **[planned]** handwriting: line crops from the detector → TrOCR (`microsoft/trocr-*-handwritten`, GPU), choosing per line
  between PP-OCR and TrOCR by confidence; Ollama VLM fallback; people/places extraction (GLiNER); OCR on fronts for date imprints.

## 4d-2. Autocorrect

**[implemented]** `banana/analysis/autocorrect.py`, `POST /api/autocorrect`

Recurring misspellings on photo backs ("Januaru", "Thanksgivng") shouldn't have to be retyped every scan.
Rule-based, offline, no model. Two sources, applied in order:

1. **Learned dictionary** (`banana/core/dictionary.py`, Stage 2 of the learning loop) — a whole-line or per-word
   fix the operator already approved on an earlier scan.
2. **Vocabulary match** — a fixed list of months, weekdays, seasons and common photo-back occasions/relations
   (`banana.analysis.autocorrect.VOCABULARY`). A word is replaced only when exactly one vocabulary word is a
   single typo away (`one_edit_apart`: one substitution/insertion/deletion, or `_transposed`: two neighbouring
   letters swapped, e.g. `Summre` → `Summer`), so `Mary` never becomes `March`.

Safety: words shorter than `MIN_LENGTH` (5) are never touched; a word already in the vocabulary (plural/possessive
included, `Grandma's`) or a name/place already confirmed on another scan (`protected`) is left alone; two
vocabulary words equally close leaves the word untouched rather than guessing.

- `find_dates` / `_fix_month_typos` in `date_parse.py` calls the same `close_match` helper, so month-name typo
  tolerance and general autocorrect share one edit-distance rule (`date-rules@3`).
- Applied automatically to OCR lines during `read_back_text` (each fix recorded as `entry["autocorrect"]`, shown
  with the corrected word and the original struck through, same treatment as the dictionary's `auto-fixed` badge).
- **UI:** the Date and Description fields call `POST /api/autocorrect` on blur (not per keystroke, so a word
  mid-typing is never touched); a fix shows inline under the field with an **Undo** that restores exactly what
  was typed. Never auto-applied to a field the operator hasn't finished editing, and always reversible — the
  same provisional/committed distinction as everywhere else in the UI (CLAUDE.md UI DESIGN SYSTEM).
- Producer: `autocorrect@1`.

## 4e. People / Places / Events from the description

**[implemented]** `banana/analysis/entities.py`, `derive_entities` in `banana/ingest/service.py`, `POST /api/entities/extract`

- **Engine:** spaCy `en_core_web_sm` (offline, `ner` extra, 2–20 ms) + rules; rules-only when spaCy is missing (health: Entity extractor = warn).
- **Pipeline:**
  1. Split OCR-joined words (`JimmyDawley` → `Jimmy Dawley`; not Mac/Van/Von/Fitz… surnames).
  2. **Known vocabulary:** people/places/events already entered on *other* scans are matched first, case-insensitive, whole words.
  3. Family words: `Grandma`, `Dad`, … and `Uncle|Aunt|Cousin <Name>`.
  4. NER: PERSON → people (unless it's an event word or date-like); GPE/LOC/FAC/ORG → places (text after ` - ` and event words removed); EVENT → events.
  5. Event words (birthday, wedding, field trip, holidays, …), with ordinals kept (`5th Birthday`); `Xmas` → `Christmas`.
  6. Photo-lab noise dropped (`ORIGINAL`, `Kodak`, …); possessive `'s` removed; ALL-CAPS title-cased; de-duplicated.
- **Server:** `read_back_text` fills **empty** people/places/events after writing the description.
- **UI:** when a scan opens and while the description changes (1 s debounce; the same text is never asked twice for a
  scan), extraction runs. Fields that were empty when the scan opened and haven't been edited by hand follow the
  description as **provisional** (dashed) chips; otherwise new values appear as **suggestion chips** (click to add).
  **[implemented]** Provisional chips are not saved and don't mark the scan unsaved: only accepted values are sent.
  **Approve** with provisional chips asks "N suggestions not accepted": **Accept all**, **Leave them out**, or
  **Back to editing**. The server reuses the correction dictionary until a correction event or scan changes.
  A chip the operator removes is dismissed for that scan and not re-added. The chip text input stays mounted during redraws (focus kept).
- Real label result: `Jimmy Dawley` / `Chocolat Design` / `Field Trip`.
- **`POST /api/entities/extract` is read-only [implemented]:** it never writes. The suggestion the learning loop
  compares against is recorded by the `PATCH` that changes the description (or by the approval, if none was
  recorded yet), inside that same save.

### Concurrent edits **[implemented]**
- `scan.version` is SQLAlchemy's `version_id_col`: every UPDATE bumps it and one based on an older read fails
  (`StaleDataError` → 409 `{code: "conflict"}`).
- The editor sends only the fields that differ from what it loaded, plus the `version` it loaded. On a 409 it
  compares: if the other save changed different fields, it re-sends with the new version without asking
  (nothing can be lost, since only its own fields are written); if both changed the same field it asks:
  **Keep my changes**, **Use the saved version**, or **Not now** (changes stay on screen, unsaved).
- Read text and Re-analyze compute on the row and commit; if the operator saved meanwhile they re-run on the
  fresh row (they only fill empty fields), with autoflush off so the write lock is taken only by the commit.

## 4f. Learning loop

> **Data note (2026-10-01):** before this date, opening a scan auto-filled empty People/Places/Events and leaving
> it saved them, so entity events with action `kept` created before 2026-10-01 may never have been looked at.
> Stage 3 should treat person/place/event `kept` events older than that as unconfirmed (down-weight or exclude).
> Events are append-only, so the date is the marker.

Rules (from CLAUDE.md): local only; only **approved** scans produce training data; events are **append-only**;
suggestions fill empty fields only and are never auto-approved; nothing is applied silently; model promotion requires beating
the incumbent on held-out data (Stage 4).

### Stage 1: Correction capture **[implemented]** (`banana/core/corrections.py`)
- **Suggestions** are stored when produced: `scan.suggestions[key] = {value, producer, ...extra}` for
  `ocr_line`, `description`, `date`, `people`, `places`, `events`, `crop_front`/`crop_back`, `rotation_front`/
  `rotation_back` (angle, `back-ocr-orientation@1` when auto-detected from the back's OCR orientation, else `0`
  from `none@0`), `pairing`, `duplicate` (+ `distance`), `blank_back` (+ `edge_density`), and `immich_duplicate`
  (+ `asset_id`, `distance`) - a match against the operator's own Immich library, kept separate from the local
  `duplicate` field. The entities endpoint records the latest People/Places/Events suggestion when called with `scan_id`.
- **OCR lines** keep `raw` (engine output), `text` (after the dictionary; plus `dictionary` = version when changed),
  and the operator's `corrected` or `removed`. `scan.ocr_frame = {crop, rotation, max_side}` says which frame the boxes refer to.
- **On approval** (`PATCH status=approved`), `record_approval` appends one `correction_event` per item:

| field | per | action rules | asset_ref |
|---|---|---|---|
| `ocr_line` | line | removed (Not text) / edited (`corrected` ≠ text) / kept | bbox, frame, raw_ocr, score, **line image** (`<data_dir>/training/ocr_lines/*.png`) |
| `person` / `place` / `event` | value | suggested & kept → kept; suggested & gone → removed; new → added (producer `operator`) | – |
| `date` | scan | suggested vs approved label | – |
| `rotation` | side | 0 (`none@0`) or auto-detected (`back-ocr-orientation@1`) vs applied | side |
| `crop` | side | detected vs current | side |
| `pairing` | scan | ingest front/back names vs current (swap → edited) | – |
| `duplicate` | scan | flagged and approved → kept | dHash distance |
| `blank_back` | scan | suggested blank/content vs `keep_back` | edge_density |
| `immich_duplicate` | scan | app flag and operator flag → kept; app flag only → removed; operator flag only → added | Immich asset id, dHash distance |

- Every event has `producer` (`name@version`, see `producers()`; e.g. `rapidocr-ppocr@1.4.4`, `photo_bbox@1`,
  `dhash@1(max=6)`, `edge-density@1(threshold=0.001)`, `back-ocr-orientation@1`, `immich-dhash@1(max=6)`,
  `+dictionary@NlMw` when the dictionary changed a line).
- **Append-only:** SQLite triggers `correction_event_no_update` / `_no_delete` abort any UPDATE or DELETE.
  Re-approving an unchanged scan writes nothing. Any change writes a new set with a new `approval_id`.
- **Training filter** `training_events()`: the latest approval per scan, for scans currently `approved|exported|stacked`.
  A scan approved and later rejected contributes nothing.

### Stage 2: Improvements without training
- **Correction dictionary [implemented]** (`banana/core/dictionary.py`), rebuilt from training events when used:
  - line fixes: key = OCR line lower-cased without spaces/punctuation (`Xmas'84` = `Xmas '84`) → approved text;
  - word fixes: small token replacements from those edits (≤2 → ≤3 tokens, similarity ≥ 0.6), e.g. `JimmyDawley` → `Jimmy Dawley`;
  - entity suppression: a value removed ≥2 times and never kept or added is no longer suggested.
  Applied to OCR output before display, marked `auto-fixed` in the UI.
- **Threshold proposals [implemented]** (`banana/core/proposals.py`), from approved decisions only, ≥5 examples each:
  - blank-back `analysis.blank_edge_density`: midpoint between approved blank and non-blank edge densities, or the fewest-mistakes value when they overlap;
  - duplicate `analysis.dhash_max_distance`: below the closest flagged-but-approved distance.
  Shown in the Learning panel. **Apply** stores a `setting_override` (value, previous, evidence) and updates the running
  settings; overrides are re-applied at startup; **Revert** restores the previous value.
- **Label-format templates [planned].**

### Stages 3–4 **[planned]**
Retraining (TrOCR on line images, spaCy on entity events, orientation classifier) and a promotion gate with held-out data
split by batch and writer; the System/Learning panel will show active version, score and promotion date.

### API
| Method | Path | Purpose |
|---|---|---|
| PUT | `/api/scans/{id}/ocr-lines/{index}` | `{text?, removed?}`: correct or reject a line |
| GET | `/api/scans/{id}/corrections` | events for a scan, newest first |
| GET | `/api/learning` | counts (total, usable, approvals, by field/action), line images, dictionary, proposals, overrides, producers, stages |
| POST | `/api/learning/proposals/{key}/apply` \| `/revert` | explicit threshold change / rollback (409/404 when not applicable) |
`PATCH /api/scans/{id}` with `status=approved` returns `corrections_recorded`.

## 4b. Health checks

**[implemented]** `banana/health.py`, `GET /api/health` → `{overall, checks[{name, label, status, detail}]}`.
`overall` = `fail` if any check fails, else `warn` if any warns, else `ok`.

| name | ok | warn | fail | off |
|---|---|---|---|---|
| `api` | responding | | | |
| `database` | `SELECT 1` succeeds | | error | |
| `exiftool` | found, prints version (cached) | | not found | |
| `native` | `banana_core` loaded | Python fallback | | |
| `ocr` | text reader loaded | engine unavailable | | `analysis.read_text = false` |
| `inbox`, `archive`, `library`, `data` | exists and writable | | missing / not writable | |
| `sane` | `scanimage` on PATH | | not found | no scanner configured |
| `scanner` | TCP connect to host:port | | unreachable | not configured |
| `immich` | connected, N asset(s) | | HTTP error / unreachable | disabled or not configured |

## 5. Analysis

### dHash **[implemented]**
- Input: 2-D uint8 grayscale.
- Box-average into an 8×9 grid. Block bounds are `floor(i*size/parts)`.
- Bit = right block brighter than left, compared by integer cross-multiplication `sum_r*count_l > sum_l*count_r` (no floating point).
- 64 bits, row-major, MSB first. Similarity = popcount of XOR.
- The C++ result must equal the NumPy reference bit-for-bit (enforced by tests).

### Blank metrics **[implemented]**
- `mean`, `std` (population), `edge_density` = fraction of interior pixels with `|I[y,x+1]-I[y,x]| + |I[y+1,x]-I[y,x]| > 24`.
- Default blank threshold `edge_density < 0.001`, **tuned on synthetic images only**.

### Decode **[implemented]**
- C++: parses the JPEG SOF marker for the size, then a single `cv::imread` with `IMREAD_REDUCED_GRAYSCALE_{2,4,8}`, then `INTER_AREA` to `max_side`.
  Non-JPEG files decode at full size.
- Python: Pillow `draft()` + `thumbnail(BOX)`. The two decoders aren't bit-identical (mean abs diff < 6 is tested).

### Planned
| Stage | Spec |
|---|---|
| DINOv3 | `facebook/dinov3-vitb16-pretrain-lvd1689m` → ONNX fp16, L2-normalized pooled embedding, FAISS inner product, cosine ≥ 0.92 → duplicate edge, union-find groups, keeper = highest resolution, then largest file |
| Text line detection | PP-OCR DB detector (ONNX), orientation search 0/90/180/270 |
| Handwriting | `microsoft/trocr-large-handwritten` (`-base-` for 8 GB GPUs) ONNX, beam search, per-line confidence from token log-probs; `trocr-base-printed` for stamps |
| Entities | GLiNER (people/places/events), boosted by names already confirmed |
| Fallback | Ollama `qwen2.5vl:7b` / `qwen3-vl:8b` with a JSON-schema response, only for low-confidence backs, off by default |
| Watermark | classify "Kodak/Fuji paper" backs as `watermark_only` → drop |

---

## 6. Dates

### `PhotoDate` **[implemented]** (`banana/dates.py`)

| Precision | Required fields | `exif_datetime()` | `label()` | Folder |
|---|---|---|---|---|
| `day` | y, m, d (validated) | `Y:M:D 12:00:00` | `1984-12-25` | `YYYY/YYYY-MM-DD` |
| `month` | y, m | `Y:M:01 12:00:00` | `Dec 1984` | `YYYY/YYYY-MM` |
| `season` | y, season ∈ spring/summer/fall/winter | spring 04-15, summer 07-15, fall 10-15, winter **01-15** | `Summer 1979` | `YYYY/YYYY-<season>` |
| `year` | y | `Y:07:01 12:00:00` | `1984` / `c. 1984` | `YYYY/YYYY-undated` |
| `decade` | y (ends in 0) | `(Y+5):07:01 12:00:00` | `1980s` | `undated/<batch-slug>` |
| `unknown` | none | `None` (date tags deleted) | `unknown` | `undated/<batch-slug>` |

`is_approximate` = precision ≠ day **or** circa.

### Parser **[implemented]** (`find_dates`, `parse_best`)
Every regex yields candidates. They're sorted by (precision rank, confidence, span length) descending, and
**overlapping spans keep only the best**.

| Pattern | Example | Precision | Confidence |
|---|---|---|---|
| ISO | `1984-12-25`, `1984/12/25` | day | 0.95 |
| mon-day-year / day-mon-year | `Dec 25, 1984`, `25th of December 1984` | day | 0.90 |
| numeric | `12/25/84` (US order unless the first part > 12) | day | 0.85 |
| mon-year | `Dec 84`, `Dec. '84` | month | 0.85 |
| holiday | Xmas/Christmas (Eve), New Year's (Eve), Halloween, Valentine's, 4th of July, Easter (computed), Thanksgiving (4th Thursday of Nov) | day | 0.80 |
| season | `Summer of '79`; autumn → fall | season | 0.75 |
| circa | `c.`, `ca.`, `circa`, `about`, `approx`, `around`, `~` + 4-digit year | year (circa) | 0.70 |
| decade | `1980s`, `80s`, `'70's` | decade | 0.60 |
| year | 4-digit 18xx–20xx, not followed by `s` | year | 0.60 |
| apostrophe year | `'84` | year | 0.50 |

Two-digit years: `yy ≤ pivot → 20yy`, else `19yy`. The pivot is `dates.two_digit_year_pivot`, or the current year mod 100 if unset.
Valid years are 1850 to the current year.

---

## 7. Export contract (Immich)

**[implemented]** `Exporter.export` + `ExifToolWriter`

### Files
```
<sorted>/<relative_dir>/SCAN_{id:06d}_A<ext>        front
<sorted>/<relative_dir>/SCAN_{id:06d}_A<ext>.xmp    sidecar
<sorted>/<relative_dir>/SCAN_{id:06d}_B<ext>(.xmp)  back, only if keep_back and a back exists
```
The extension is lower-cased from the source file.

### Procedure (per scan)
1. Staging: `<sorted>/.staging/scan_{id:06d}/` is wiped and recreated. It's on the same filesystem, so `os.replace` is atomic.
2. For each side: copy the source → ExifTool writes embedded tags (`-E -overwrite_original -P`) →
   create the sidecar with `-tagsfromfile <img> -XMP:all <img>.xmp` → **read both back and compare** →
   sha256 of the staged image.
3. If a previous export exists at a different path (the date changed), delete the old image and sidecar.
4. Move the **sidecar first**, then the image, into place.
5. Remove staging. Update the `export` row and set the scan to `exported`.

Any verification mismatch raises `MetadataVerificationError`; nothing is moved.

### Tags written

| Tag (write group) | Value | Embedded | Sidecar |
|---|---|---|---|
| `XMP-xmp:CreatorTool` | `Photo Scanner` (guarantees a non-empty XMP packet) | ✓ | ✓ |
| `EXIF:DateTimeOriginal`, `EXIF:CreateDate` | `exif_datetime()` or deleted | ✓ | – |
| `XMP-exif:DateTimeOriginal`, `XMP-xmp:CreateDate` | same | ✓ | ✓ |
| `EXIF:ImageDescription` | full description | ✓ | – |
| `XMP-dc:Description` | full description | ✓ | ✓ |
| `XMP-dc:Subject` | leaf names, de-duplicated | ✓ | ✓ |
| `XMP-digiKam:TagsList` | `People/John` | ✓ | ✓ |
| `XMP-lr:HierarchicalSubject` | `People\|John` | ✓ | ✓ |

**Full description** = description + ` (date approx: <label>)` when approximate and not unknown.

**Hierarchical tags**, in order: `People/*`, `Places/*`, `Events/*`, user tags (split on `/`), `Box/<box_label>`,
`Scan/DateApprox` (if approximate), then `Scan/Back` (on the B file) or `Scan/HasBack` (on the A file when a back is exported).
In each segment `/` and `|` become `-` and whitespace is collapsed. Duplicates are removed case-insensitively.

Values are HTML-escaped and written with `-E`, so newlines survive the ExifTool `-stay_open` protocol.

### Immich requirements
- Mount `sorted` into Immich **read-only**. The library import path is the **container** path.
- Library exclusion pattern `**/.staging/**`.
- **[planned]** `POST /api/libraries/{id}/scan` → find assets by `originalPath` → `POST /api/stacks` (front primary) → optional album per batch → metadata refresh on re-export.

### Read-only duplicate check **[implemented, route-confirmation pending against a real server]**

`banana/immich/` (`client.py`, `dedup.py`, `settings.py`). Operator-triggered only, gated by `immich_setting.enabled`
(default off) - never runs on ingest or export automatically. The client has no `PUT`/`PATCH`/`DELETE` method at
all, and its one `_post` helper checks the target path against a hardcoded allow-list of exactly the two
documented non-mutating POST endpoints (`assets/bulk-upload-check`, `search/metadata`), raising before any
request otherwise. `build_client()` returns `None` whenever the setting is disabled or incomplete, so no network
call happens anywhere until the operator has explicitly enabled it and saved a URL + key.

Two independent passes, both read-only:
1. **Exact checksum, at export time.** `Exporter` re-encodes on export, so only the file actually written to
   `sorted` can ever byte-match Immich - this is *export verification*, not pre-export prevention. A SHA1 is
   computed alongside the existing SHA256 and checked against Immich via `POST /assets/bulk-upload-check`
   (chunked at 500 items, unconfirmed cap); a match's asset id is stored on `Export.immich_front_id`/`immich_back_id`,
   with `Export.immich_checked_at` distinguishing "never checked" from "checked, no match." Reset to `None` on
   every re-export.
2. **Perceptual, our own dHash on Immich's thumbnails.** Immich's Smart Search only accepts text or an existing
   Immich asset id as a similarity anchor - there is no API to ask "is this external image similar to anything
   in the library." So the library is enumerated via `POST /search/metadata` (paginated, `type: IMAGE` only so videos are
   skipped, `libraryId` sent only when a Library ID is configured), each new/changed
   asset's thumbnail is fetched (`GET /assets/{id}/thumbnail`) and hashed with the same native `core.dhash`
   already used for local rescan detection, and the result is cached in `immich_asset_hash` keyed by Immich's
   reported checksum. Every scan's `dhash_hex` is then compared against the cache with `core.hamming` and
   `analysis.dhash_max_distance` - the same threshold already used for local rescans, against a different pool.
   A match is recorded via `corrections.suggest(scan, "immich_duplicate", {asset_id, distance}, producer)`, kept
   entirely separate from the local-rescan `duplicate_group_id`/`operator_duplicate` fields.

Runs as a background thread inside the API process (`ImmichCheckController`, same polled state/phase/lock shape
as `sane.ScanController`) started by `POST /api/immich/check` (`409` if disabled or already running) and polled
via `GET /api/immich/check/status`. `GET /api/immich/status` is a separate, cheap live probe (calls
`assets/statistics`) used by "Test connection" and the health check - `OFF` with zero network calls when
disabled, `OK` with the asset count when reachable, `FAIL` with a plain-language reason (`HTTP {code} - check the
API key/library id`, or `Could not connect: ...`) otherwise.

**Genuine unknowns, pending a real server:** exact `search/metadata` pagination field names; exact
`assets/{id}/thumbnail` size query param; whether `bulk-upload-check` actually caps items per request; the exact
asset-count field name in `assets/statistics`'s response.

---

## 8. HTTP API

**[implemented]** FastAPI. OpenAPI at `/openapi.json`, interactive docs at `/docs`. **No authentication.**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{version, native_core}` |
| GET | `/api/health` | component health (section 4b) |
| GET | `/api/scanner` | `{configured, host, port, online, device, source, mode, resolution, after_scan, scan}` (online = TCP connect, 1.5 s) |
| POST | `/api/scanner/scan` | body `{destination?: "review"\|"inbox", count?: "all"\|"one"}`; starts a scan; 409 if one is running or no scanner is configured |
| POST | `/api/scans/{id}/swap-sides` | swap front/back (409 without a back) |
| POST | `/api/scans/{id}/reanalyze` | re-detect crops, re-run blank/duplicate checks and text reading |
| POST | `/api/scans/{id}/read-text` | OCR the back again; fills empty description/date |
| GET | `/api/scanner/scan` | `{state: idle\|scanning\|done\|failed, destination, run_name, started_at, finished_at, pages, files[], message, ingest}` |
| GET | `/api/summary` | scan counts per status, native flag, configured paths |
| GET | `/api/scans?status=&limit=` | list (default limit 500, max 5000), ordered by id |
| GET | `/api/scans/{id}` | one scan (includes `version`) |
| PATCH | `/api/scans/{id}` | change only the fields sent; with `version`, 409 `{code: "conflict", scan}` if it was saved elsewhere since **[implemented]** |
| PATCH | `/api/scans/{id}` | update (see below) |
| DELETE | `/api/scans/{id}` | delete a rejected scan's files + record; 409 unless rejected, unexported, and no correction history |
| GET | `/api/scans/{id}/image/{front\|back}?size=` | cached JPEG preview, 64–4096 px (default 1600); 404 if missing |
| GET | `/api/dates/parse?text=` | date candidates, best first |
| POST | `/api/ingest` | run inbox ingest → `{batch, created[], skipped[], unmatched[], possible_duplicates[]}` |
| POST | `/api/export` | export every `approved` scan, **synchronously** → `{exported[{id,front,back}], errors[{id,error}]}` |
| GET | `/api/immich/settings` | `{enabled, url, library_id, import_path_prefix, api_key_set, api_key_last4}` - never the raw key |
| PATCH | `/api/immich/settings` | body: any of `enabled, url, library_id, import_path_prefix, api_key`; omit `api_key` to keep it, `""` to clear it |
| GET | `/api/immich/status` | live probe, zero network calls when disabled → `{enabled, connected, asset_count, error}` |
| POST | `/api/immich/check` | start the read-only duplicate check in the background; 409 if disabled or already running |
| GET | `/api/immich/check/status` | poll the running/last check → `{state, phase, message, stats, ...}` |

**Scan JSON** = all scan columns except `ocr_lines`, plus `batch`, `box_label`, `has_back`, `export` (row or null) and
`date {precision, year, month, day, season, circa, label, exif, approximate}`.

**PATCH body** (all fields optional):
```json
{
  "date_text": "Xmas '84",            // "" = unknown; 422 if not understood
  "date": {"precision": "decade", "year": 1960},   // takes precedence over date_text
  "description": "…", "people": [], "places": [], "events": [], "tags": [],
  "keep_back": true,
  "status": "needs_review | approved | rejected"
}
```
`keep_back` is forced to false when the scan has no back. Dates set through the API get `date_source = "manual"`.

---

## 9. Web UI

**[implemented]** `banana/web/static/` is plain HTML/CSS/JS with no build step, no CDN and no network font loads.
The rules live in CLAUDE.md > **UI DESIGN SYSTEM** (direction: *Darkroom*). This section describes how the code meets them.

**Visual system: Darkroom**
- **Files:** `theme.css` holds every token (colors, fonts, type scale, spacing, radius, durations). It's loaded first.
  `app.css` has components only and references tokens exclusively. `app.js` sets no colors.
  Tokens added for the review UI (in a marked block in `theme.css`): `--scrim`, `--control-h`, `--control-h-sm`, `--thumb-w`,
  `--thumb-h`, `--frame-h`, `--frame-h-narrow`, `--queue-w`, `--line-row-h`, `--border-w`, `--focus-neutral`.
- **Surfaces:** true neutrals. Depth comes from surface elevation and hairline borders only; no gradients, glows or shadows.
- **Images:** photos and thumbnails sit only on `--image-well`. Captions sit `--space-5` below the well, so accent-colored controls, focus rings and the
  keep-back checkbox never come within `--space-5` of a photo. Queue selection and focus next to thumbnails are neutral (`--focus-neutral`).
- **Accent**: violet `--accent` (`#8B7CF6`, 5.0:1 on raised; `--on-accent` text on it 5.5:1) and blue `--focus-ring`
  (`#5B9BF0`, 5.9:1) mark interactive elements only: primary buttons, hover borders, focus rings, the active tab, in-progress state.
  They replaced amber on 2026-09-14 at the operator's request. Blue/purple is accent-only: surfaces and the image well stay neutral.
- **Status** uses three colors (`--status-ok` / `--status-fail` / `--status-idle`) plus shape and text. Status chips have a dot
  (hollow = to review). Warnings (`dup?`, flagged duplicate) are a triangle plus text. Health warnings read "Warning:".
  Learning bars: kept = ok, removed = fail, fixed = secondary text gray, added = strong border gray.
- **Typography:** IBM Plex Sans 400/600 for UI, IBM Plex Mono for IDs, batch/file names, OCR line text, producers and numbers
  (vendored latin woff2 + OFL in `static/fonts/`). Body and inputs are 16 px; `--text-sm` is used for labels and metadata only.
  `--text-muted` is used only for disabled text.
- **Suggested vs committed:** elements whose value the app proposed carry `data-state="suggested"` (dashed, secondary text) until
  committed: the date and description while they equal `scan.suggestions`, chips that match suggested entities, derived `+` chips,
  unfixed OCR lines, the crop / blank-back / duplicate chips. Editing a field, fixing a line, or approving commits it.
- **Refresh safety:** re-rendering the current scan after an async action skips any field that has focus or was edited
  (`state.edited`), chip fields that are focused, touched or mid-typing, and the OCR list while a line edit is open.
- **Skeletons** reserve the final box: the image well keeps its fixed height and shows a flat surface block; queue skeleton rows
  have the same size as queue items; OCR skeleton rows use `--line-row-h` and the current row count; the health skeleton uses the last check count.
- **Design and review skills** (vendored in `.claude/skills/`, each with `SOURCE.txt`; CLAUDE.md wins on conflicts):
  `redesign-existing-projects` (taste-skill redesign variant, with a PROJECT PRECEDENCE block overriding its fonts,
  gradients, grain, CDN images and motion defaults), `web-design-guidelines` (Vercel's rules pinned locally in
  `references/command.md`, no runtime fetch), and `playwright-cli` (Microsoft's `@playwright/cli`, installed globally
  with npm; `playwright-cli show --annotate` lets the operator mark up the live page for design feedback).
- **Developer walkthroughs:** `walkthrough-*.html` files in the project root are generated with the vendored skill in
  `.claude/skills/walkthrough/` (upstream source in `SOURCE.txt`). They load React/Tailwind/Mermaid/Shiki from CDNs, so they're docs
  only and are never served by the app. Current file: `walkthrough-project-overview.html`.
- **Irreversible actions** (`.btn.irreversible`, a heavier accent outline) open `#confirm-dialog`: **Export approved** (shows the count; a toast when nothing
  is approved) and **Apply** on a threshold proposal. Reversible actions (approve, reject, rotate, swap, revert) don't confirm.
- **Motion:** every animation and transition sits inside `@media (prefers-reduced-motion: no-preference)`. The one staggered reveal
  is on queue load (`animation-delay` from `--dur-reveal`). Transitions are color/border only, at `--dur-fast`.
- **Keyboard:** shortcuts are declared on controls with `data-shortcut`, rendered as a `.shortcut-hint` on the control, exposed as
  `aria-keyshortcuts`, and dispatched to the first visible, enabled control with that shortcut. They never fire while typing (except Ctrl+S).
  Review loop: Approve `A`, Next `J`, Previous `K`, Reject `X`, Flag duplicate `D`, Rotate right `R` / left `Shift+R`, keep back `B`,
  Read text `T`, Save `Ctrl+S`. Every one of these also has a visible labeled button.
- **Flag duplicate** toggles `scan.operator_duplicate`. On approval it becomes a `duplicate` correction event:
  app flag and operator flag → kept; app flag only → removed (false alarm); operator flag only → added.
- **Enforcement:** `tests/test_ui_rules.py` checks no emoji, no external refs, tokens only (no hex/rgb/hsl/named colors, font
  families only via `var(--font-*)`, no gradients or box-shadows), motion only under no-preference, and that fonts are vendored.
  These scan `static/vendor/**` too (emoji/external-refs only - vendored third-party CSS/JS is exempt from tokens-only,
  since its colors are overridden separately, see Guide tour below).
  `tests/ui/test_ui_live.py::test_ui_provisional_marking` covers provisional marking in the browser, alongside the tests for visible controls, refresh safety and export confirmation.
- **Learning panel** (top bar **Learning** with usable-event count): corrections total, per-field kept/fixed/removed/added
  bars, dictionary entries, threshold proposals with Apply/Revert, active producers, stage status. Health, Learning and
  Immich panels are mutually exclusive; Escape closes whichever is open.
- **Immich panel** (top bar **Immich**): settings form (server URL, API key as `type="password"` showing only
  `•••• <last4>` once saved, library ID, Enabled checkbox), **Test connection** (hits `/api/immich/status` without
  saving), and **Check Immich for duplicates** (goes through the same confirm dialog as Export/Delete, since it
  reaches a real external server; polls `/api/immich/check/status`). The panel's own copy states plainly: read-only,
  never uploads, edits, stacks, or triggers anything in Immich. Editor gets a matching `in Immich?` chip
  (`#scan-immich-dup`, dashed until confirmed) and a manual flag toggle, mirroring **Flag duplicate**'s
  kept/removed/added pattern but recorded as the separate `immich_duplicate` correction field.
- **Guide tour** (top bar **Guide**, shortcut `G`) - `banana/web/static/tour.js`, vendored **driver.js** 1.8.0 (MIT,
  `static/vendor/driver/`, `SOURCE.txt`; confirmed to make no network requests of its own). A 14-step walkthrough of the
  whole workflow (scan/ingest → queue → images → editor tools → text on back → date → description → chips →
  approve/reject → export → learning → health → recap); steps whose control isn't on screen (no scanner configured, no
  scan in the queue) are filtered out, and the tour opens the first queued scan itself if the editor is empty so the
  editor-only steps always have something to point at.
  - Popover styling is token-only (`.driver-popover.photo-scanner-tour` in `app.css`; `--driver-popover-font-family` and
    `--driver-animation-duration` are set from tokens on `:root`); the overlay color comes from `--scrim` when present,
    falling back to driver.js's own default rather than a hardcoded color.
  - `animate: !reduceMotion` - the fade/scale animation classes driver.js would otherwise add are skipped entirely under
    `prefers-reduced-motion: reduce`, so there's nothing to gate behind a `no-preference` media query in `driver.css`.
  - **Exit is tracked with a `MutationObserver` on `document.body`'s class list, not driver.js's `onDestroyed` config**:
    that hook turned out to only fire when driver.js still has an active element/step to report, which isn't true on the
    tour's first and last (center-screen) steps - Escape or Done from either of those silently skipped it. Removing the
    `driver-active` class happens on every exit path, so that's what marks `localStorage.tourCompleted` and refocuses
    the Guide button reliably.
  - Tests: `tests/ui/test_ui_live.py::test_guide_tour_opens_steps_through_and_completes` (all 14 titles unique, Done sets
    `tourCompleted` and refocuses Guide), `test_guide_tour_reopens_with_g_shortcut_and_closes_on_escape`,
    `test_guide_tour_at_400px`.
- **Text on back rows:** the line (click to add to description), confidence %, **Fix** (inline input, Enter saves, Escape cancels),
  **Not text** / **Restore**, badges `fixed` (with the original crossed out) and `auto-fixed` (dictionary).

- **Tabs:** status filter with counts (`/api/summary`).
- **Top bar:**
  - **System** indicator: a dot for the overall state from `/api/health` (polled every 30 s) with text "System OK" / "N warnings" / "N problems".
    Clicking it opens the System health panel (every check with status and detail), which has Recheck and Close; Escape also closes it.
  - Scanner status (online / offline / "Scanning, N pages").
  - **After scan** dropdown (`Add to review queue` = `review`, `Leave in inbox` = `inbox`). The choice is remembered per browser; the default comes from config.
  - **Scan feeder** is disabled while offline or scanning. Progress is polled every 1.5 s while a scan runs.
  Scanner controls are hidden when `scanner.host` is empty.
- **Queue:** thumbnail (160 px), id, date label, `dup?`, `back text` (back has content).
- **No emojis or pictographic icons** anywhere in the UI; use text labels and CSS shapes (checked on `.html/.css/.js` files).
- **Editor:** front/back previews, keep-back toggle, date field with live parse preview (150 ms debounce),
  description, chip inputs, Save / Approve / Reject, export location.
- Unsaved edits are saved automatically when switching scans or tabs, or before export. The browser warns on unload.
- Keys: see **Keyboard** above.

**[planned]** Duplicate resolver (side-by-side, pick keeper), bulk edit, OCR line boxes over the back, autocomplete from known names.

---

## 10. CLI

`banana` (Typer):

| Command | Purpose |
|---|---|
| `banana doctor [--config]` | native module, ExifTool, path existence |
| `banana export --manual-json FILE [--config]` | import a hand-written batch spec ([manual-export.md](manual-export.md)) and export it |
| `banana serve [--host 0.0.0.0] [--port 8000]` | API + UI (config from `BANANA_CONFIG`) |
| `banana worker [--config]` | job worker loop |
| `banana repair-duplicates [--config]` | fix any `duplicate_group_id` left pointing at a deleted scan; safe to re-run, no-op when nothing's dangling |

---

## 11. Configuration

Loaded from `--config`, else `$BANANA_CONFIG`, else `./config.toml`, else defaults. A UTF-8 BOM is tolerated.

| Key | Default | Meaning |
|---|---|---|
| `paths.inbox` | `/mnt/photo_vault/inbox` | scanner output |
| `paths.archive` | `/mnt/photo_vault/archive` | originals, per batch |
| `paths.sorted` | `/mnt/photo_vault/sorted` | Immich library root |
| `paths.data_dir` | `/srv/banana` | `banana.db`, `thumbs/` |
| `pairing.pattern` | `^(?P<base>.+_\d{4})(?P<suffix>_a\|_b)?\.(?:jpe?g\|tiff?)$` | case-insensitive |
| `pairing.front_variant` | `original` | or `enhanced` |
| `analysis.blank_edge_density` | `0.001` | |
| `analysis.dhash_max_distance` | `6` | |
| `analysis.read_text` | `true` | OCR backs during ingest/re-analyze |
| `analysis.ocr_max_side` | `1800` | px, long side given to OCR |
| `analysis.orientation_search` | `true` | auto-rotate front+back from the back's OCR orientation (needs `ocr` extra) |
| `analysis.orientation_search_max_side` | `300` | px, long side per rotation tried (up to 6 cheap passes) |
| `dates.two_digit_year_pivot` | current year mod 100 | |
| `scanner.host` | `""` | scanner address for the status indicator (FF-680W: `192.168.16.178`) |
| `scanner.port` | `1865` | Epson network scan port; ports 80/443 serve the Epson web config |
| `scanner.sane_device` | `epsonds:net:<host>` | SANE device name override |
| `scanner.scanimage` | `scanimage` | path to the SANE CLI |
| `scanner.source` | `ADF Duplex` | or `ADF Front` |
| `scanner.mode` | `Color` | `Color` \| `Gray` \| `Lineart` |
| `scanner.resolution` | `600` | dpi (50–600) |
| `scanner.auto_crop`, `scanner.skew_correction` | `true`, `true` | |
| `scanner.timeout_seconds` | `3600` | maximum run time for one scan |
| `scanner.max_feeder_count` | `36` | ADF hopper capacity; caps "Whole stack" (`--batch-count`), never a forced count |
| `scanner.after_scan` | `review` | default destination: `review` \| `inbox` |
| `scanner.first_side` | `back` | which page of a duplex pair is the photo's back (FF-680W: `back`) |
| `exiftool.path` | `exiftool` | |
| `immich.enabled` | `false` | hard opt-in; filling in `url`/`api_key` alone never starts any network activity |
| `immich.url`, `api_key`, `library_id`, `import_path_prefix` | | **[implemented]** pre-set for a headless deploy; once the operator saves anything in the Immich panel, the `immich_setting` DB row takes precedence |

---

## 12. Native module

`native/`: C++20, nanobind (`NB_STATIC`), CMake ≥ 3.18, scikit-build-core. OpenCV is optional (`HAVE_OPENCV`).

| Function | Signature | GIL |
|---|---|---|
| `dhash` | `(uint8[h,w] C-contiguous) -> int` | released |
| `blank_metrics` | `(uint8[h,w], edge_threshold=24) -> (mean, std, edge_density)` | released |
| `decode_gray` | `(path, max_side=1024) -> uint8[h,w]` (OpenCV builds only) | released during decode |

The dispatcher `banana.core` uses native when importable. `BANANA_DISABLE_NATIVE=1` forces the reference implementation.

Benchmark on a 5400×3600 JPEG (Core Ultra 9 275HX, WSL2):

| | Python | C++ |
|---|---|---|
| decode → 1024 px | 34 ms | 32 ms |
| dHash, full resolution | 84 ms | 27 ms |
| blank metrics, full resolution | 80 ms | 17 ms |

---

## 13. Deployment

- **Image** (`docker/Dockerfile`): stage 1 `ubuntu:24.04` builds the native wheel. The runtime is `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04`
  plus `libimage-exiftool-perl` and the OpenCV 4.6 runtime libraries. **Not yet built or tested.**
- **Compose** (`docker/compose.yaml`): `banana-api` (:8000), `banana-worker` (GPU), `ollama` (profile `vlm`).
  Volumes: `/mnt/photo_vault`, `/srv/banana`, `docker/config.toml` → `/etc/banana/config.toml`.
- **Host:** NVIDIA driver, Docker, nvidia-container-toolkit, NFS mount of the Synology share.
- **Dev:** WSL2 Ubuntu 24.04. `scripts/wsl_build.sh` rsyncs the Windows checkout to `~/Photo_Scanner_Banana`, builds and tests.

---

## 14. Testing

`pytest` (62 tests on Linux with native + ExifTool; natively-built and ExifTool tests are skipped when unavailable).

| File | Covers |
|---|---|
| `test_date_parse.py` | parser table, pivot, overlaps, no-date strings |
| `test_dates_layout.py` | precision rules, folders, filenames, tag building |
| `test_pairing.py` | FastFoto naming, orphans, unmatched |
| `test_core.py` | dHash/blank behavior, **native == reference**, native decode vs Pillow |
| `test_exporter.py` | ExifTool round trip, staging cleanup, originals untouched, re-export moves files |
| `test_jobs.py` | retries → failed |
| `test_api.py` | ingest → edit → approve → export over HTTP, scanner status, component health, no emojis in UI |
| `test_scanner.py` | fake `scanimage`: duplex pairing, front-only, empty feeder, one scan at a time, names match the ingest pattern |
| `tests/ui/test_ui_live.py` | **live component tests** in headless Chromium against a real server (temp folders, fake scanner): every button, field, chip input, checkbox, dropdown, tab and keyboard shortcut; responsive at 1440/1024/400 px with no horizontal scroll; System health panel; offline scanner state. Needs the `ui` extra + `playwright install --with-deps chromium` |

Live dev server for watching changes: `scripts/dev_live.sh` (uvicorn `--reload` from the Windows checkout plus page auto-reload via `/api/dev/reload-token` when `BANANA_DEV_RELOAD=1`).

---

## 15. Roadmap

| Phase | Status |
|---|---|
| 1 Skeleton, native core | done |
| 2 Export engine | done; **not yet verified in a real Immich instance** |
| 3 Watcher, DINOv3 + FAISS, detector + TrOCR, GLiNER, benchmark on real backs | next (needs a sample scan batch) |
| 4 Review UI | redesigned (cards, skeletons, bundled fonts); duplicate resolver and bulk edit pending |
| Learning loop 1–2 | correction capture, dictionary, threshold proposals: done |
| Learning loop 3–4 | retraining and promotion gate: pending (needs correction data) |
| 4b Immich read-only duplicate check (exact checksum + perceptual dHash) | done; route/param specifics **not yet verified against a real Immich server** |
| 5 Immich automation (scan, stacks, albums, refresh) | pending |
| 6 TensorRT, batching, profiling | pending |
