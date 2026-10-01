# Photo Scanner: pitfalls and hardening plan (rev 2)

## Context
The first production run worked: 595 scans, 574 exported to `C:\PhotoScanner\sorted`, 21 rejected. It also hit trouble:
- 12 scan runs left pages stuck in hidden `inbox\.scanning-*` folders.
- 20 requests failed with server errors.
- 543 of the exports are undated.

The operator asked for a holistic look at what can go wrong. Below are answers to their points 1-10, then a proposed fix for every other pitfall so they can direct each one.

**Before any code change:** this checkout is **2 commits behind `origin/main`**, and the running exe was built from `origin/main`. The two missing commits are:
- `27527f8` Scan through Epson's TWAIN driver on Windows;
- `37caff8` Require sign-in on every route.

Work must start from `git pull`. The local CLAUDE.md still says "NEVER use TWAIN"; origin's version allows it on Windows.

## Decisions (operator, 2026-09-30)
- **Scope this round:** pull origin, the rescue, then points 1-10. Library and export fixes (#11-18) come in the next round; #12 (keep the export path) is already decided.
- **Point 9 → option A.** Keep the CLAUDE.md rule. Opening a scan never saves, suggestions stay provisional, and Approve asks "N suggestions not accepted: Accept all / Leave out". CLAUDE.md gets one clarifying line saying so, and that browsing never writes.
- **Point 4 → the hands-off option:**
  - ingest runs automatically both after scans **and** for files dropped into the inbox. A watcher polls every 15 s and uses the readiness check from point 5;
  - everything goes through one ingest lock and queue;
  - the Ingest button stays as a manual "check now";
  - Export stays manual and confirmed.

---

## Operator's points, investigated

### 1. Why the 12 runs went unnoticed, and what caused them
- **Cause (found).** Scans on Windows go through `banana/scanner/twain_scan.py` (origin). Each page is written as `page_NNNN.bmp`, and an `after()` callback converts it to `.jpg` and deletes the BMP.
  - In all 12 stuck folders the **last page is still a .bmp** (17-25 MB, fully written). The driver call `src.acquire_file(...)` raised after the final transfer, before `after()` ran.
  - The exception skips `place_pages()`, so the run ends as "failed".
  - The `finally` block keeps the folder because pages remain, which is the intended "nothing is lost" behaviour.
  - The exact driver error was never recorded, because scan failures are not logged.
- **Why it went unnoticed.**
  - The only signal is a red toast that disappears after 6 s (`app.js:938-939`), shown only if that tab is still polling.
  - Nothing is logged: the server log has 0 scan errors.
  - Nothing on screen says "pages were kept"; the inbox folders are hidden, and no screen lists them.
- **The operator did notice and rescanned.** Each failed run is followed 1-3 minutes later by a successful one, often with the same page count. 145 of the 166 stuck JPEG pages (plus the 12 BMPs) match images already in the archive (dHash ≤ 6). The remaining **21 pages need a human look**; they may be real missing photos.
- **Fixes:**
  - **(a) Salvage on failure.** In the failure path, convert any leftover `page_*.bmp` to JPEG, then run `place_pages()` so the pages still reach the inbox, and mark the run "finished with a warning".
  - **(b) Log every run.** Log each run's outcome with the full driver exception and traceback.
  - **(c) Persistent warning.** Failure stays on screen as a banner on the Scanner panel ("Scan stopped after 33 pages: <reason>. 34 pages were kept.") with a **Recover** button. No more 6-second toast.
  - **(d) Recovery at startup.** On startup, find `.scanning-*` folders, run the same salvage, and show them in the banner.
  - **(e) Find the driver error.** Catch the TWAIN error around `acquire_file` and record its condition code. If it is the usual end-of-feeder or cancel condition after the last page, treat it as success.

### 2 + 3. Logged crashes, taken together
- **What the log shows.** The 28,926-line log holds exactly **20 server errors, all the same one:** `sqlite3.OperationalError: database is locked`.
  - 18 were the operator's saves (`PATCH /api/scans/N`) and 2 were entity extraction.
  - The only other non-200 is one 409, a second "Scan" click while scanning.
- **One root cause.** The ingest that runs after every scan holds one write transaction across the whole batch, including OCR and crop/dHash for each photo (`banana/ingest/service.py:41-83`). Every other write waits 5 s (`db.py:20`) and then fails.
- **Where it leads:**
  - failed saves: the operator retries, so the edit is not lost, but it is friction;
  - a crash during that window strands files in `archive\` with no database row;
  - entity extraction (point 10) adds writes during the same window.
- **Fix:** commit per photo, run the analysis outside the transaction (load, analyse, then a short write), and raise `busy_timeout` to 30 s as a safety net. The fix also adds a timestamped, rotating log, so the next incident can be dated.

### 4. Should ingest be automatic?
- **It already is.** With "send to review", the scan thread ingests when the feeder empties (`_ingest_after_scan`). The manual **Ingest inbox** button is for files dropped in by other software.
- **The pitfall** is that both can run at once with no lock.
- **Recommendation (chosen):** keep ingest automatic, and route both paths through **one ingest lock** with a queue. While an ingest runs, the button shows "Ingest running…".
  - **Inbox watcher (chosen):** a background thread in the server (not the dead `banana worker` queue) checks the inbox every 15 s. It is on by default, with a setting to turn it off. It shows "Watching inbox" and the time of the last pick-up in the System panel, so you can see it's alive.
  - **Export stays manual.** It writes to the library Immich will read, and CLAUDE.md treats it as irreversible with an explicit confirm.

### 5. Half-written inbox files: contain them and bypass
- **Who is affected.** Only files that other software writes directly into the inbox. The app's own scans arrive whole: written in `.scanning-*`, then `os.replace`d in. So it's not specifically "the first couple of tries"; it's any time a file is copied in while ingest runs.
- **Fix:**
  - **(a) Readiness check.** Ingest skips files modified in the last 10 s or whose size is still changing, and reports them as "not ready yet, will pick up next time".
  - **(b) Containment.** Each file is opened and decoded inside its own try. A file that fails is left in place and listed; one bad file no longer aborts the batch.
  - **(c) Bypass.** After 3 failed attempts or 10 minutes, the file moves to `inbox\_unreadable\` and appears in a visible list with **Retry** and **Show in folder** buttons. Nothing blocks the queue.

### 6. Reused file names: a persistence fix
- **Who is affected.** The app's own runs are unique (`scanYYYYMMDDHHMMSS_NNNN`). The risk is external software restarting at `_0001`.
- **Fix:** persist a content hash. Add `scan.source_sha256`. On ingest:
  - same name and same hash: a true re-drop, skipped;
  - same name and a different hash: a new photo, ingested, with a disambiguated `source_key` (name plus a short hash).
- **Migration:** existing rows get their hash filled in from the archive files, in the background, once.

### 7. Delete removes originals: intentional, with gates, and in the exe
**Confirmed in origin/main and in the exe:**
- only **rejected** scans can be deleted;
- not if the scan was exported;
- not if it has correction history;
- the UI asks for confirmation ("Delete scan #N permanently?").

The only residual flaw is ordering: files are unlinked **before** the database commit (`api.py` delete_scan). **Fix:** commit first, then unlink.

### 8. "Last save wins": what actually happens
**Outcomes, traced:**
- **(a) Opening a scan can save it**, because of point 9's auto-save on navigation. Any second tab or person with that scan open then overwrites, or is overwritten, without any warning.
- **(b) The full form is sent on every save** (`app.js` save → every field, including all chip lists). Two editors working on different fields still wipe each other's work.
- **(c) Slow endpoints commit a stale copy.** Read text and Re-analyze load the scan, run OCR for several seconds, then commit, overwriting a description or date typed meanwhile.

**Solution, in three layers:**
1. **Send only changed fields.** The client tracks which fields are dirty, and `PATCH` sends only those, so separate fields never collide. This fixes most real cases.
2. **Version check.** Add `scan.version` (an integer). PATCH carries the version it loaded; a mismatch returns 409 with the current values. The UI shows "Changed elsewhere: <field>. Keep mine / Take theirs". It never silently drops either side.
3. **Slow endpoints re-load before writing**, fill empty fields only, and bump the version.

### 9. Suggestions saved as committed, and CLAUDE.md
- **Evidence in the database:**
  - **893 entity values recorded as "kept"** (person 294, place 339, event 260), against 62 added by hand;
  - 543 of those 574 exported scans have no date.

  Because opening a scan auto-fills empty People/Places/Events and marks the form dirty, many "kept" values were probably never consciously accepted. The learning loop is now training on them as confirmed.
- **The code paths:**
  - `deriveEntities()` calls `markDirty()` (`app.js:243-261`);
  - leaving a scan auto-saves it (`confirmLeave`);
  - `chips.get()` returns provisional values too (`app.js:205`).
- **Two ways to reconcile the code and CLAUDE.md** (operator to choose, see the questions below):
  - **A. Keep the rule, fix the code.** Opening never saves, suggestions stay provisional, and Approve asks: "3 suggestions not accepted: Accept all / Leave out".
  - **B. Change the rule.** CLAUDE.md says "Approve accepts every suggestion still shown". Opening still never saves, and correction events record `approved_via="bulk"` so training can tell these apart.
- **Either way:** browsing must never write. The past "kept" events can't be edited (append-only). New events get a marker so training can down-weight entity "kept" events from before the fix date.

### 10. Entity extraction: a better way
- **Today:** 1,285 calls in one day. Each call rebuilds the learned dictionary, scans every other scan's names, runs spaCy, and **commits**. It fires on open, on every refresh, 400 ms after each description keystroke, and after autocorrect.
- **Better design:**
  - **(a) Compute once on the server**, when the description or OCR text changes (ingest, read text, save). Store it in `scan.suggestions` with a hash of the source text, and have the client simply render what's stored.
  - **(b) The live endpoint becomes read-only.** No commit. It runs only on description **blur**, or after 1.5 s idle, and is skipped if the text hash is unchanged.
  - **(c) Cache the dictionary and known names in memory**, invalidated on approve or save. Today they are rebuilt per call.
  - **(d) Suggestions for correction events** come from the stored record at approve time, not from whichever request ran last.
  - **Expected result:** about 1 call per edited description instead of about 2-3 per scan view, and zero writes from browsing.

---

## Remaining pitfalls, each with a possible fix

### Library and export
| # | Pitfall | Possible fix |
|---|---|---|
| 11 | Re-export deletes the old files before the new ones are placed (`exporter.py:69-72`) | Move the new files into place first, then remove only the old paths not reused. fsync staged files before `os.replace`. |
| 12 | A date change moves the file (Immich would see delete + new) | **Decided:** keep the first export path, update metadata in place. `export_scan` reuses `Export.front_rel` / `back_rel`. |
| 13 | `SCAN_{id}` names collide across stations or a rebuilt database | Prefix new exports with a station ID (generated once and stored in the database), e.g. `SCAN_A1F3_000042_A.jpg`. Existing files keep their names. |
| 14 | Double-clicking Export runs two exports over the same staging | Server-side export lock (409 "export already running"); unique staging folder per run; the button disabled while running. |
| 15 | Accented tags may be garbled (ExifTool encoding unconfirmed) | Pass `encoding="utf-8"` to `ExifToolHelper`, plus a round-trip test with "José Łódź / Zoë". No current exports are affected (no non-ASCII data). |
| 16 | Crash between moving files and saving → orphaned library file, later a duplicate | Write a small "pending export" row before moving and clear it after the commit; on startup, reconcile pending rows against disk. |
| 17 | Exported scans edited later silently go stale | Mark them "Edited since export" with a badge, and offer a re-export button for those scans only. |
| 18 | The sidecar and image are swapped in separately (a crash can leave a new .xmp next to an old image) | The startup reconcile from #16 checks that each sidecar's checksum matches its image and re-writes the sidecar if not. |

### Install, upgrade, runtime
| # | Pitfall | Possible fix |
|---|---|---|
| 19 | The ExifTool path breaks when the download folder is moved or upgraded | At startup, if the configured path is missing, use the bundled `_app_dir()/tools/exiftool/exiftool.exe` and rewrite that key. Merge new default keys into the existing config. |
| 20 | Upgrade silently keeps the old version running (fixed port 8420) | `/health` returns the version. A launch that finds a different version says "An older Photo Scanner is running: Quit it and start the new one?" |
| 21 | No Quit control; Task Manager kills cut off exports | A visible **Quit** button in the header. It waits for any running scan, ingest or export, or explains what is running. |
| 22 | Log: no timestamps, never rotates, logs every poll | Timestamped format, 10 MB × 5 rotation, polls logged at debug level. Scan, ingest and export outcomes at info and error. |
| 23 | Startup errors are invisible (windowed exe) | Write a `startup-error.html` and open it in the browser; it shows the error and the log path. |
| 24 | Absolute paths in the database; moving `C:\PhotoScanner` breaks everything | Store paths relative to the configured roots, with a one-time migration. Training-event paths stay as they are (append-only); a lookup re-bases them. |
| 25 | Schema changes have no backup | Copy `banana.db` to `banana.db.bak-<version>` before any column change; record the schema version in the database. |
| 26 | The exe is unsigned; antivirus may quarantine exiftool | Code-sign in CI (needs a certificate; the operator decides). Meanwhile the health check names "ExifTool missing, possibly quarantined". |
| 27 | CI smoke test only checks `/health` | Extend the frozen-exe test: ingest one fixture pair, OCR it, export it, and verify the result with ExifTool. |

### Review quality
| # | Pitfall | Possible fix |
|---|---|---|
| 28 | Lists cap at 500; the newest 95 scans are unreachable | Page by 200 with "Load more", or pass a high limit. Show "showing N of M" whenever capped. |
| 29 | 543 of 574 exports are undated | Approve warns on a missing date. Add an **Undated** filter, and **Set date for selected** (bulk per box or batch). Use box labels as the folder fallback instead of `inbox-<timestamp>`. |
| 30 | Faint backs judged blank are dropped silently | Approve warns when the back is "looks blank" but ink is detected. Show a dimmed back preview so the operator can tick "keep back". |
| 31 | "Possible rescan" doesn't block Approve; rejected scans count as matches | Approve asks "Possible rescan of #N: approve anyway?" Exclude rejected scans from matching, and keep the older scan as the original. |
| 32 | Save failure shows only "Internal Server Error" | Map errors to plain text ("Database busy, retrying…"), retry automatically once, and keep the unsaved flag visible on the scan. |
| 33 | No way back after approve or export | Add a **Back to review** control on approved scans; exported ones use #17. |
| 34 | Date parsing quirks (US order, current-year pivot, every save marks the date "manual") | Make the pivot and the D/M vs M/D order settings; set "manual" only when the date field itself was edited. |
| 35 | Autocorrect on blur races Approve (the fix arrives after the save) | Approve awaits any pending autocorrect before saving. |
| 36 | Swapping sides keeps the old OCR line boxes, which become bad training crops | Swap clears `ocr_lines` and `ocr_frame` and re-reads the new back. |
| 37 | Re-analyze overwrites the operator's "keep back" choice | Re-analyze changes `keep_back` only if the operator never touched it. |
| 38 | The job queue is dead code (`banana worker` fails every job) | Remove it, or wire it to the ingest and export lock from #4. Recommendation: remove it. |
| 39 | `banana serve` binds 0.0.0.0, and auth depends on env/setup | Default to 127.0.0.1 and refuse 0.0.0.0 unless sign-in is configured. Check that origin's new `auth.py` enforces this. |

### Operator tasks (no code)
- Back up `C:\PhotoScanner` and `%LOCALAPPDATA%\PhotoScanner\data` on a schedule. Everything is on one drive.
- Rescue: review the 21 unmatched stuck pages (see "Rescue" below).

---

## This round, in order
1. `git pull` to `origin/main`, and run the full suite as a baseline.
2. Rescue (below), a dry run first.
3. **Logging:** timestamped, rotating log (#22, pulled forward), so every later step is observable.
4. **Scanner (point 1):** TWAIN salvage, logging of the condition code, persistent banner plus Recover, startup recovery. Files: `banana/scanner/twain_scan.py`, `banana/scanner/sane.py`, `banana/web/api.py`, `banana/web/static/app.js` and `index.html`.
5. **Ingest (points 2-6):** per-photo commits, one lock and queue, inbox watcher, readiness check and `_unreadable`, and a `source_sha256` column with backfill. Files: `banana/ingest/service.py`, `banana/db.py`, `banana/models.py`, `banana/web/api.py`.
6. **Delete order (point 7):** commit, then unlink.
7. **Concurrency (point 8):** a `scan.version` column, partial PATCH, the 409 dialog, and slow endpoints re-loading before they write.
8. **Suggestions and entity extraction (points 9-10):** no writes on open; Approve prompts about suggestions; suggestions stored server-side; the endpoint read-only; an in-memory cache.
9. Docs, the B-n bug entries, the CLAUDE.md lines and the Playwright tests. CI then builds the exe, and the operator runs the on-PC checks.

## Rescue of today's stuck pages
1. **Dry-run report.** For each of the 12 folders: pages, the BMP converted, and the dHash match with archive scan IDs. Fronts and backs are checked separately, because blank backs match everything.
2. **Operator confirms.** The 21 unmatched pages, plus any whose front doesn't match, go into the inbox through `place_pages()` and are ingested. Matched duplicates move to `inbox\_recovered_duplicates\`; they are never deleted.

## Docs and tracking
- Each confirmed pitfall becomes a B-n entry in `docs/diagnostics&bugs.md` §4, starting with B-n for the TWAIN last-page BMP.
- Mark changes [implemented] in `docs/specifications.md`.
- Update the CLAUDE.md "Suggested is never committed" rule (point 9) and the scanner line, which origin already changed.
- New controls each get a 400px Playwright test in `tests/ui/test_ui_live.py`: Recover, Retry for unreadable files, Quit, conflict dialog, approve warnings, Undated filter.

## Verification
- **Unit tests:**
  - TWAIN salvage: the fake `twain` in `tests/test_twain_scan.py` raises after the last `before()`; pages get placed and the BMP converted;
  - startup recovery of a fixture `.scanning-*` folder;
  - per-photo ingest commit, with a PATCH during ingest succeeding;
  - concurrent ingests serialised;
  - a half-written file skipped, then `_unreadable` after 3 failures;
  - same name with a different hash ingests;
  - PATCH 409 on version mismatch, and partial PATCH preserving other fields;
  - opening a scan sends no PATCH (Playwright network assertion);
  - extract endpoint performs no writes;
  - re-export keeps the path and moves the new files before removing the old ones;
  - UTF-8 tag round-trip.
- **Full suite:** `scripts/wsl_build.sh`, plus `tests/ui`.
- **Packaged exe on this PC:**
  - scan a stack and pull the last photo mid-feed → banner plus recovered pages;
  - edit scans during an auto-ingest → no "database is locked" in the log;
  - date an undated exported scan, re-export, confirm the same path in `sorted` and the new date via ExifTool.
