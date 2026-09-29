# CLAUDE.md - Photo Scanner

Offline photo-scan review pipeline: Epson FF-680W scans → review web UI → EXIF + XMP export → Immich External Library.

> **Technical Docs:** `docs/specifications.md` | **Operator Reference:** `docs/operators.md` | **Bug Tracker:** `docs/diagnostics&bugs.md`

---

## STRICT RULES & NON-NEGOTIABLES

* **Branding & Naming:**
  - Product name in UI/Docs MUST be **Photo Scanner**.
  - `photo_scanner_banana`, `banana`, and `BANANA_*` are code/package names ONLY.
  - NEVER display "Banana" in user UI, API titles, exported metadata, or operator docs.
* * **UI Design & Layout Rules:**
  - **Tech Stack:** Plain HTML/CSS/JS ONLY. No build steps, no external CDNs,
    no network font loads. The UI MUST render identically with networking
    disabled (`test_ui_no_external_refs`).
  - **No Emojis/Pictographic Icons:** text labels, inline SVG, or CSS shapes
    ONLY (`test_ui_has_no_emoji`).
  - **Tokens only:** every color, font, radius, spacing step and duration comes
    from `banana/web/static/theme.css`. No hardcoded values in templates or
    components (`test_ui_tokens_only`).
  - **Responsiveness:** mobile-first, fully usable down to 400px viewports.
  - **Accessibility floor:** body text >= 16px at >= 4.5:1 contrast. All motion
    inside `@media (prefers-reduced-motion: no-preference)`.
  - Full visual system and component contracts: see **UI DESIGN SYSTEM** below.
* **Data & Hardware Constraints:**
  - Originals are NEVER modified; ingestion moves them to `archive/<batch>/`.
  - Scanner goes through **SANE** on Linux (`epsonds:net:<host>`, TCP 1865) or **Epson's TWAIN driver (Epson Scan 2)** on Windows (`scanner.backend = "auto"` picks per platform; `banana/scanner/twain_scan.py`). NEVER use ScanSmart or eSCL/AirScan. The scanner's address is found by name over mDNS (`_scanner._tcp`), not hard-coded. SANE limitations: $\le$ 600 dpi, no working auto-crop, no auto-rotate; the TWAIN driver offers 1200 dpi, auto-crop, deskew and auto-rotate.
  - Recognition is **offline only** (TrOCR primary, local Ollama VLM fallback). Cloud OCR/LLM APIs are strictly forbidden.
  - Do NOT connect to NAS or Immich unless explicitly instructed.
* **Learning & Model Training:**
  - Training and fine-tuning run **locally only** (server GPU). No cloud
    training, no dataset upload, no third-party ML services. Corrections and
    scan images NEVER leave the operator's machines.
  - Only **approved** scans produce training data. Pending and rejected scans
    are never used.
  - Correction events are **append-only**. Originals are still never modified,
    and an event is never edited or deleted in place — re-editing a scan writes
    a new event.
  - Suggestions fill **empty fields only** and are NEVER auto-approved. This
    holds for every model version, however well it scores.
  - A new model version is promoted only if it beats the incumbent on a
    held-out set. The previous version is retained for rollback. No silent
    model swaps.
## LEARNING LOOP

The app improves from operator corrections. Nothing leaves the machine, and the
operator stays in control of every field. Staged spec with [implemented] /
[planned] flags lives in `docs/specifications.md`.

### What improves today
- **Stage 1 [implemented]:** approvals write append-only correction events (DB triggers) with producer and, for
  OCR lines, the line image in `<data_dir>/training/ocr_lines/`. Line **Fix** / **Not text** in the Text on back panel.
- **Stage 2 [implemented]:** correction dictionary (line + word fixes, applied to new OCR as `auto-fixed`;
  entity values removed ≥2× stop being suggested) and blank-back / duplicate threshold proposals with explicit
  Apply/Revert (`setting_override`). Label-format templates are [planned].
- Known names: a name saved on any scan becomes a known name for later scans (B-16).
- **Stages 3–4 [planned]:** retraining and promotion gate. They need correction data first.
- Code: `banana/core/{corrections,dictionary,proposals}.py`; API `/api/learning*`, `/api/scans/{id}/ocr-lines/{i}`;
  UI **Learning** panel. Tests: `tests/test_learning.py`, `test_learning_loop_fix_line_approve_and_reuse`.

### Stage 1 — Correction capture (prerequisite for all later stages)
`banana/core/corrections.py`. On scan approval, emit one append-only event per
field the operator touched:

| field | records |
|---|---|
| `scan_id`, `batch`, `created_at` | provenance |
| `field` | `ocr_line` / `person` / `place` / `event` / `date` / `rotation` / `crop` / `pairing` / `duplicate` / `blank_back` |
| `action` | `kept` / `edited` / `removed` / `added` |
| `suggested`, `approved` | what the app proposed vs what was committed (either may be null) |
| `producer` | name + version of the model or rule that made the suggestion |
| `asset_ref` | for `ocr_line`, the line bbox and cropped line image; for `crop`/`rotation`, the transform applied |

`producer` is mandatory — without it, corrections from a retired model cannot be
filtered out of a later training set.

Requires direct line-level text editing in the **Text on back** panel; a
corrected line is worthless as training data without its line image.

### Stage 2 — Improvements with no training
Effective immediately once Stage 1 has data:
- **Correction dictionary:** a normalized `suggested → approved` map per field
  type, applied before display. "JimmyDawley" → "Jimmy Dawley" once, forever.
- **Threshold tuning:** recompute blank-back and duplicate thresholds from
  operator decisions. **Proposed in the System panel, never applied silently.**
- **Label-format templates:** recognize recurring print formats (e.g. Chocolat
  Design backs) and parse them structurally.

### Stage 3 — Periodic retraining
Background job on the server GPU, nightly or on demand:
- **Handwriting (TrOCR):** fine-tuned on corrected line images. Highest payoff —
  a few hundred corrected lines measurably improves a given writer.
- **Names (spaCy):** retrained on chips kept, removed and added.
- **Orientation:** small classifier trained on applied rotations.

### Stage 4 — Promotion gate
- **Split held-out data by batch and by writer, never randomly by line.** The
  same handwriting appearing in both train and test inflates scores and is the
  easiest way to ship a worse model that benchmarks better.
- Metrics: character error rate (OCR), precision/recall (names), accuracy
  (orientation).
- Promote only on improvement over the incumbent. Keep the previous version.
- The System panel shows the active version, its score, and its promotion date.

---

## COMMANDS & WORKFLOWS

### WSL2 Build & Test Environment (Ubuntu-24.04)
```bash
# Full build + C++ native compilation + all test suites
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/wsl_build.sh

# Run Playwright UI tests only
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/wsl_build.sh tests/ui

# Watch live UI edits (Runs on 127.0.0.1:8000 with auto-reload BANANA_DEV_RELOAD=1)
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/dev_live.sh

# Run local test server without NAS/Immich (Target path: C:\Users\TSF2\Banana_Test\)
scripts/serve_local.sh

banana doctor                      # Environment & hardware diagnostics
banana serve                       # Start main API + UI web server
banana export --manual-json FILE   # Process batch export manifest
banana worker                      # Background ingestion & OCR task queue
banana train <target>              # Local fine-tune: ocr | names | orientation
banana models                      # List versions, scores, active; roll back


ARCHITECTURE & DATA PATTERNS
Native Hot Paths (native/):

C++ (nanobind) reserved exclusively for per-image CPU hot paths.

Every native function MUST have a bit-for-bit identical NumPy reference in banana/core/reference.py verified by test suites.

Metadata Export Pipeline:

Written and verified using ExifTool (matching Immich's parser).

Export stages files in <sorted>/.staging, verifies by readback, then uses atomic os.replace (moving .xmp sidecar first).

Tag hierarchy: People/, Places/, Events/, Box/, Scan/ stored in digiKam:TagsList, lr:HierarchicalSubject (| delimited), and dc:subject.

Scanner Batch Files (banana/scanner/sane.py):

Outputs to inbox/.scanning-<run>/, renamed to <run>_NNNN.jpg / <run>_NNNN_b.jpg before auto-ingest.

TESTING & DOCUMENTATION CONVENTIONS
Live UI Component Testing (tests/ui/test_ui_live.py):

Uses Playwright + headless Chromium against a live temporary server.

Every new component, button, text field, or dropdown MUST include a test verifying it is pressable/fillable and functions at 400px width.

Docs Maintenance:

Synchronize features in docs/specifications.md using [implemented] or [planned] flags.

Log confirmed bugs in docs/diagnostics&bugs.md (Section 4) with sequential B-n IDs.

Pairing file pattern is unconfirmed for real FastFoto scans—do not assume naming formats without checking raw scan outputs. 

## UI DESIGN SYSTEM

### Who operates this
Primary operator is experienced and works at volume; family members also review
and tag. **Every action must be reachable by a visible, labeled control.**
Keyboard shortcuts are accelerators layered on top, never the only path. Where
density and legibility conflict, legibility wins.

### The direction: Darkroom
Neutral dark surfaces, a violet accent with a blue focus ring (changed from amber on 2026-09-14 at the operator's request), IBM Plex. The chrome recedes
so the photographs carry the screen. This direction is **settled** — do not
introduce a new palette, typeface, or visual language. Consistency with what
already ships outranks novelty on every screen. Compose from existing tokens;
if a token is missing, add it to `theme.css` and say so in your summary.

### Semantic contracts
These are behavioral rules, not style preferences. Breaking them changes what
the UI means.

- **Surfaces are true neutrals** (R=G=B). No blue-tinted "dark mode" grays
  anywhere. A color cast in the chrome biases judgment of the scan.
- **`--image-well` is a fixed mid neutral (~18% gray)** and is the only
  background permitted directly behind a photo or thumbnail. Never near-black
  (washes images out) and never white (makes them read dark). Do not restyle it
  per-screen.
- **The accent (violet `--accent`, blue `--focus-ring`) means interactive, and nothing else.**
  Primary actions, focus rings, and in-progress state. It is never a warning, never
  decorative, and never placed within `--space-5` of image content — use the neutral
  scrim or place the badge outside the image bounds. Surfaces and `--image-well` stay
  neutral: blue/purple is accent-only.
- **Status uses three colors only:** ok / fail / idle. Warnings are expressed
  with text and shape, not a fourth color, so the accent stays unambiguous.
- **Suggested is never rendered as committed.** Any value the app proposed
  carries `data-state="suggested"` and renders provisionally (dashed border,
  secondary text) until the operator commits it. This is a data-integrity rule:
  the learning loop's `suggested` vs `approved` distinction is worthless if the
  operator cannot see it at a glance. Enforced by
  `test_ui_provisional_marking`.
- **An async refresh NEVER replaces a field the operator is editing.** Any
  element with focus, or with uncommitted input, is exempt from suggestion
  refreshes and re-renders. (This was a real bug — chip-field keystrokes were
  lost on refresh.)
- **Skeletons reserve the exact final dimensions.** No layout shift when OCR
  results, thumbnails, or health checks land.
- **Irreversible actions** (approve → export, apply a threshold proposal) get a
  distinct treatment and an explicit confirm. Reversible ones must not.

### Typography
IBM Plex Sans for UI, IBM Plex Mono for filenames, EXIF values, batch IDs, OCR
line text and any column of numbers (`font-variant-numeric: tabular-nums`).
Both OFL, both **vendored** as subset `woff2` in
`banana/web/static/fonts/` — no webfont is fetched at runtime, ever. Declare a
real fallback stack; `font-display: swap`.

`--text-muted` fails AA at body size. Disabled and decorative use only.

### Motion
One staggered reveal on batch/grid load (`animation-delay` off
`--dur-reveal`). Everywhere else, transitions are functional and under
`--dur-base`. Depth comes from surface elevation and hairline borders — no
gradients, no glows, no glassmorphism.

### Keyboard
Shortcuts declared on the element via `data-shortcut` so they are discoverable,
testable, and rendered as a visible hint on the control itself. Required on the
review loop at minimum: approve, next/prev, rotate, flag duplicate.

### Enforcement
| Test | Enforces |
|---|---|
| `test_ui_has_no_emoji` | no pictographic characters |
| `test_ui_no_external_refs` | no `http://`, `https://`, or `//` in `src`, `href`, `@import`, `url()` |
| `test_ui_tokens_only` | no hex, `rgb()`, `hsl()` or font-family outside `theme.css` |
| `test_ui_provisional_marking` | every suggested value carries `data-state="suggested"` |
| `tests/ui/test_ui_live.py` | every new control is pressable/fillable at 400px |