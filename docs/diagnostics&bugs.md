# Diagnostics & Bugs

Troubleshooting for operators and maintainers, plus the list of known bugs and limitations.
Sections marked [shell] need shell access to the server (or WSL on the dev laptop).

- [1. Quick health check](#1-quick-health-check)
- [2. Symptoms and fixes](#2-symptoms-and-fixes)
- [3. Inspecting data](#3-inspecting-data)
- [4. Known bugs](#4-known-bugs)
- [5. Known limitations](#5-known-limitations)
- [6. Dev environment issues](#6-dev-environment-issues)
- [7. Reporting a bug](#7-reporting-a-bug)

---

## 1. Quick health check

Start with the **System** indicator in the top bar of the review page. Click it to see each component's status and details.

| Check | How | Healthy result |
|---|---|---|
| All components | open `/api/health`, or the System panel | `"overall": "ok"` |
| Server is up | open `http://<server>:8000/health` | `{"version":"0.1.0","native_core":true}` |
| Counts and paths | open `/api/summary` | the paths are the ones you expect |
| Components [shell] | `banana doctor` | `native banana_core : yes`, `exiftool : /usr/bin/exiftool`, every path `ok` |
| Tests [shell] | `wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/wsl_build.sh` | `62 passed` |

`native_core: false` is not an error: everything still works on the slower Python code. It means the C++ module wasn't built or failed to import.

---

## 2. Symptoms and fixes

### The page doesn't load
- **Server not running.** Start it: `banana serve` (server), or `scripts/serve_local.sh` / `docker compose up -d` on the dev laptop.
- **Port already in use** (`address already in use` in the log). Stop the old process with [shell] `pkill -f "banana serve"`, or choose another port.
- **Dev laptop + WSL:** if `curl localhost:8000/health` works inside WSL but not in a Windows browser, run `wsl --shutdown`, then start the server again.
  Windows PowerShell's `Invoke-RestMethod` can hang against WSL forwarding; `curl.exe` is more reliable for checks.

### System panel shows a problem

| Component | Red means | Fix |
|---|---|---|
| Database | can't open `banana.db` | check `paths.data_dir` exists and has disk space |
| ExifTool | not installed / wrong path | [shell] `apt install libimage-exiftool-perl` or fix `exiftool.path` |
| Inbox / Archive / Library / Data folder | folder missing or not writable | create it, check the NFS mount, check permissions |
| SANE | `scanimage` missing | [shell] `apt install sane-utils` (already in the Docker image) |
| Scanner | no answer on `192.168.16.178:1865` | scanner on and on Wi-Fi? same network as the server? (see below) |

C++ core amber = slower Python fallback, still works. Immich hollow dot = not connected yet (expected).

### Scanner offline / Scan feeder disabled
1. Scanner powered on, not asleep, Wi-Fi light on.
2. Open `https://192.168.16.178` from the server's network. The Epson web page should load.
3. [shell] `scanimage -d epsonds:net:192.168.16.178 -A` lists the options when SANE can reach it.
4. The address changed? Update `scanner.host` (a DHCP reservation on the router prevents this).
5. In Docker, the container must reach the LAN (default bridge network is fine; outbound TCP 1865).

### Scan failed: "No photos in the feeder"
Load the photos and click **Scan feeder** again.

### Scan failed: "Scanner is busy"
Another program (Epson ScanSmart on a PC, or a second scan) is using the scanner. Close it, wait a few seconds, retry.

### Scan message says "odd page count"
In duplex mode every photo should produce two pages. The last photo has no back image. Usually a misfeed or jam:
check that photo, rescan it if needed.

### Scan message says "feeder capped at 36, scan again for more"
Expected, not a bug: **Whole stack** stops on its own at `scanner.max_feeder_count` (default 36, the FF-680W ADF
hopper's measured capacity) so a larger stack can't jam the feeder. The first 36 photos scanned fine; load the
rest and click **Scan feeder** again.

### Scans are tall pages with the photo at the top ("vertical")
The scanner doesn't crop through SANE (B-8), so the app detects the photo itself. If a scan shows **full page**:
click **Re-detect crop**. If it's still wrong, the photo may be on a background the detector doesn't recognize
(for example, a photo with a large blue-gray area touching its edges). Report it with the scan id; the original stays in the archive.

### Text on back is empty or wrong
- **"No text read yet."**: the scan was ingested before text reading existed, or `analysis.read_text` is off. Click **Read text**.
- **Button error "no text reader available"**: the OCR engine isn't installed. [shell] `pip install "photo-scanner-banana[ocr]"`
  (included in the Docker image); check the **Text reader** row in the System panel.
- **"No text found on the back."** with visible writing: most likely **handwriting** (B-12), or very faint pencil.
- **Garbled lines**: check the back isn't swapped with the front and that the crop is right; then **Read text** again.
- **Date not filled although it's on the label**: the date field wasn't empty, or the format isn't recognized.
  Type it yourself and report the exact text.
- Reading takes about 1–2 s per back, so ingesting a large stack takes a little longer.

### Sharing the dev laptop's app with other devices on the network
The server runs inside WSL, which other devices can't reach directly. It has **no login (B-3)**, so share only on a trusted network.
1. Start the server so it accepts forwarded connections. For device testing use the stable server (the live-reload one reloads
   phones whenever code changes): [shell] `BANANA_HOST=0.0.0.0 BANANA_SKIP_TESTS=1 bash scripts/serve_local.sh`.
   (`BANANA_HOST=0.0.0.0 bash scripts/dev_live.sh` also works for live edits.)
2. In an **administrator** PowerShell: `powershell -ExecutionPolicy Bypass -File C:\Users\TSF2\Photo_Scanner_Banana\scripts\share_on_lan.ps1`.
   It forwards port 8000 on the PC's LAN address to WSL and opens the firewall for the **local subnet only**, then prints the URL.
3. Stop sharing: the same command with `-Remove`.
- **Works on the PC, not on the phone:** WSL's address changes after a reboot or `wsl --shutdown`. Run the script again.
  Check that both devices are on the same Wi-Fi (not a guest network with client isolation).
- **Still blocked:** `Get-Service iphlpsvc` must be Running (Windows uses it for port forwarding).

### Learning panel shows 0 corrections
Corrections are recorded only when you **approve** a scan (and only while it stays approved). Scans approved before
this feature existed have none (B-19). Open one, change anything, and approve again.

### A threshold proposal says "Needs at least 5…"
Proposals need at least 5 approved examples of each kind. Keep reviewing; the numbers update on **Refresh**.

### An applied threshold made things worse
Open **Learning**, find the proposal, click **Revert to …**. [shell] Applied values are rows in the `setting_override` table.

### "correction events are append-only" error
Something tried to edit or delete stored corrections, which the database refuses by design. Corrections can't be changed:
re-approve the scan to record a newer set.

### Photo is upside down or sideways
Normal: the scanner can't tell which way is up. Use **Rotate left / right** (or <kbd>R</kbd>). Automatic rotation is planned.

### Front and back are swapped
Use **Swap front/back**. If *every* scan is swapped, `scanner.first_side` doesn't match how photos are loaded:
change it between `back` (FF-680W, photos face down) and `front`.

### Leftover `.scanning-scan…` folder in the inbox
Kept only when a scan stopped with pages still inside (for example the server restarted mid-scan). The pages
are `page_0001.jpg`, … in scan order. Move them out and rename them to `<name>_0001.jpg` / `<name>_0001_b.jpg` pairs,
or delete the folder and rescan.

### "Inbox has no new scans" after scanning
1. Check that the Epson software saves into **the same folder** shown under `paths.inbox` in `/api/summary`.
2. Look at the message for `Ignored: …` file names. Those names don't match the pairing pattern (next item).
3. If the files are there with the right names, they were already ingested: same base name as an earlier scan. See [Re-scanning a photo](#re-scanning-a-photo-with-the-same-file-name).

### Files are listed as "Ignored"
The pairing pattern expects `<prefix>_<4 digits>[_a|_b].jpg|tif`, for example `Attic3_0001_b.jpg`.
If your Epson software names files differently, note a few real file names and change `pairing.pattern` in the config.
The pattern must keep the `(?P<base>…)` and `(?P<suffix>…)` groups. Ignored files stay in the inbox.

### A back was dropped as "looks blank" but has writing
Very light pencil or small stamps may fall below the edge threshold. In the review page, tick **keep & stack in Immich**.
If it happens often, lower `analysis.blank_edge_density` (default `0.001`; try `0.0005`).

### A blank back is kept ("has writing")
Paper-brand watermarks ("Kodak paper"), dust or scratches count as content. Untick **keep & stack**.
Raise `analysis.blank_edge_density` if it happens a lot. Automatic watermark detection is planned.

### `dup?` on photos that aren't duplicates
Expected sometimes. See [bug B-1](#b-1-dhash-flags-similar-looking-photos-as-rescans). Ignore the warning.

### Date field turns red / "could not understand date"
The parser didn't find a date. Reword it: `Dec 1984`, `'84`, `Summer 1979`, `1960s`. See the date table in [operators.md](operators.md#4-entering-dates).
If a common way of writing dates isn't recognized, report it (section 7) with the exact text.

### Date read wrong
- `03/04/85` becomes **March 4**: US order is assumed. Type `4 Mar 1985`.
- `'27` becomes **1927**, `'24` becomes **2024**: two-digit years use a cut-off at the current year. Change it with `dates.two_digit_year_pivot`.
- `Winter 1984` becomes **Jan 15, 1984**, not December.

### Export shows errors
The message lists `#id: error` for each failed scan. Other scans still export.

| Error contains | Cause | Fix |
|---|---|---|
| `MetadataVerificationError` | ExifTool read back a different value than written | Rare. Report it with the scan id and the text you entered. |
| `ExifToolExecuteError` / `exiftool` not found | ExifTool missing or `exiftool.path` wrong | [shell] `banana doctor`; install `libimage-exiftool-perl` |
| `Permission denied` | the server can't write to `paths.sorted` | fix share/NFS permissions for the banana user |
| `No such file` on a source | the archived original was moved or deleted | restore it into `archive/<batch>/` |

Failed scans stay **Approved**; click **Export approved** again after fixing the cause.

### A photo shows the wrong date in Immich
1. Check what's in the file (section 3, [shell] `exiftool`).
2. If the file is correct, run *Scan* for the library in Immich. Immich only re-reads metadata when a file changes or on refresh.
3. If Immich shows the **export date**, the scan had no date (unknown). Add one and export again.

### Leftover `.staging` folder in the library
Created during export and normally removed. It can remain if the server was killed mid-export. It's safe to delete when no export is running.
Make sure Immich excludes `**/.staging/**`.

### Re-scanning a photo with the same file name
Ingest skips base names it has already seen, so a rescan saved as `Attic3_0001` stays in the inbox.
Rename the new files with another prefix or number (for example `Attic3r_0001`), then ingest.

---

## 3. Inspecting data

### What's written in an exported file [shell]
```bash
exiftool -G1 -s -DateTimeOriginal -XMP-dc:all -XMP-digiKam:TagsList -XMP-lr:HierarchicalSubject \
    /mnt/photo_vault/sorted/1984/1984-12-25/SCAN_000001_A.jpg
exiftool -G1 -s -XMP:all /mnt/photo_vault/sorted/1984/1984-12-25/SCAN_000001_A.jpg.xmp
```

### Find a scan's files
- Review page: the header shows `batch · original base name`; exported scans show the library path.
- API: `GET /api/scans/{id}` → `front_path`, `back_path` (archive) and `export.front_rel` (library).

### Database [shell]
SQLite at `<data_dir>/banana.db` (the `sqlite3` CLI may not be installed, so this uses Python):
```bash
python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("/srv/banana/banana.db")
for row in db.execute("select status, count(*) from scan group by status"): print(row)
for row in db.execute("select id, type, state, attempts, error from job where state != 'done'"): print(row)
EOF
```
Don't edit the database while the server is running unless you know what you're doing. Stop it first and keep a copy of the file.

### Logs [shell]
- `banana serve` logs every request to stdout: `docker compose logs banana-api`, or the terminal running `serve_local.sh`.
- Exceptions during export are also returned in the Export response.

### Preview cache
`<data_dir>/thumbs/` can be deleted at any time; previews are regenerated on demand.

---

## 4. Known bugs

| ID | Summary | Severity | Status |
|---|---|---|---|
| B-1 | dHash flags similar-looking photos as rescans | low | open, fixed by the planned DINOv3 step |
| B-2 | Ingest can grab a scan that is still being saved | medium | open |
| B-3 | No authentication on the web page or API | high (if exposed) | open |
| B-4 | Export runs inside the web request | low (large batches) | open |
| B-5 | Re-export after a date change makes Immich see a new asset | medium | open, by design until the Immich integration |
| B-6 | A scan in progress is lost if the server restarts | low | open |
| B-7 | Scanner limited to 600 dpi, no enhance/auto-rotate through SANE | low | limitation of the `epsonds` backend |
| B-8 | `--adf-crp` ignored: every page is the full 8.5 × 15.5 in canvas | high | worked around: software crop (`photo_bbox`) |
| B-9 | Scanned front/back arrived swapped | high | fixed: `scanner.first_side = "back"`; existing scans use **Swap front/back** |
| B-10 | Scan progress showed "0 pages" while ingesting | low | fixed: page count kept, phase shown as "Processing" |
| B-11 | 9/10 real scans flagged `dup?` | medium | fixed: dHash runs on the cropped photo (was comparing blank page areas) |
| B-12 | Handwriting on backs isn't read (PP-OCR is trained on printed text) | high | open: TrOCR handwriting stage planned |
| B-13 | Text on backs wasn't read at all ("text detection not working") | high | fixed for printed text: RapidOCR/PP-OCR on ingest + **Read text** |
| B-14 | People/Places/Events had to be typed although they're in the description | medium | fixed: derived from the description (spaCy + rules + known names) |
| B-15 | Typing in a chip field lost letters when suggestions refreshed | medium | fixed: input no longer re-mounted on redraw (found by live UI tests) |
| B-16 | Unknown first names can be tagged as places ("Tommy" at Yellowstone) | low | open: remove the chip; once a name is used on any scan it's recognized as a person |
| B-17 | Learned line fix not reused on a rescan ("Xmas'84" vs "Xmas '84") | medium | fixed: dictionary line keys ignore spacing/punctuation (found by live UI test) |
| B-18 | Live dev server can stop when `wsl_build.sh` reinstalls the native module underneath it | low | open: restart `dev_live.sh` after a full build |
| B-19 | Scans approved before learning capture existed have no correction events | low | by design: re-approve (after any edit) to record them |
| B-20 | Chip-field inputs (People/Places/Events/Tags) rendered in Arial 13.3px instead of Plex 16px | low | fixed: inputs had no `type`, so `input[type=text]` missed them; all controls now get the UI font; `test_every_control_renders_in_plex_at_readable_size` |
| B-21 | After a Windows restart, phones can't reach the app although the port-forward rule exists | medium | open: IP Helper starts before Wi-Fi has its address and never listens on it. Rerun `share_on_lan.ps1` as admin (or `Restart-Service iphlpsvc`) |
| B-22 | Every page load logged a 404 for `/api/dev/reload-token` on the normal server | low | fixed: endpoint always exists and returns `enabled: false` outside dev (found with the Playwright CLI) |
| B-23 | Approve then Reject pressed quickly: the second action was silently dropped | medium | fixed: status actions queue and apply to the scan shown next; selection is never cleared during re-render; `test_rapid_approve_then_reject_acts_on_the_next_scan` |

#### B-6 Scan interrupted by a server restart
Scans run inside the server process. Restarting it (deploy, crash, or the live dev server reloading after a
code change) stops `scanimage` and leaves pages in `inbox/.scanning-…`. Don't restart during a scan. See section 2
for recovering the pages. (Planned: run scans in the worker process.)

#### B-7 SANE limits
`epsonds` offers up to 600 dpi for the FF-680W (Epson's own software also has 1200 dpi) and no FastFoto
auto-enhance or auto-rotate. Long-page mode is reported not to work. Rotate photos in the review step
(planned) or in Immich.

#### B-1 dHash flags similar-looking photos as rescans
**Symptom:** `dup?` on unrelated photos with a similar layout (same horizon, same white border, dark/light halves).
Seen in testing: demo scan #5 was flagged against an unrelated photo.
**Cause:** dHash compares an 8×9 brightness grid, so similar compositions collide.
**Workaround:** ignore the warning. Lowering `analysis.dhash_max_distance` (e.g. 4) reduces false alarms but misses more real rescans.
**Fix:** DINOv3 similarity confirms or clears the match.

#### Ingest grabbed a scan that was still being saved
**B-2. Symptom:** a photo with a broken or partial image, or a front whose back arrives later and is ignored.
**Cause:** ingest takes whatever is in the inbox. The planned "file size stable" check isn't implemented.
**Workaround:** click **Ingest inbox** only after the Epson software finishes the whole stack.
A back left behind in the inbox can't currently be attached to its already-ingested front; move it aside and report it.

#### B-3 No authentication
Anyone who can reach port 8000 can edit and export. **Only run it on a trusted LAN, or bound to `127.0.0.1`**
(`serve_local.sh` does that). Don't port-forward it to the internet.

#### B-4 Export runs inside the web request
Exporting hundreds of scans at once can take minutes, and the button stays disabled until it finishes. A browser or proxy timeout
may show an error even though the export continues and completes on the server. Refresh the page to see the result.
Exporting in smaller batches avoids this. (Planned: run exports as background jobs.)

#### B-5 Re-export after a date change
Changing an exported scan's date moves its files to a new folder. Immich treats the moved file as a **new asset**:
faces, favorites, albums and Immich-side edits on the old asset are lost, and the old one goes offline.
**Workaround:** get dates right before exporting, and fix wrong dates before organizing photos in Immich.

---

## 5. Known limitations

- **Not yet in this version:** handwriting reading (TrOCR), AI duplicate detection (DINOv3), watermark detection, Immich stacks/albums, automatic Immich scan, duplicate resolver screen, bulk edit.
- **Backs appear as separate photos in Immich** until stacking is implemented.
- **Unknown dates** get no EXIF date, so Immich places them at the file time (the export day).
- **People are tags, not Immich People.**
- **Date parser is English-only.** Numeric dates use US order.
- **Limited database migrations.** New columns are added automatically; renamed or removed columns aren't. Keep backups of the database and the archive.
- **Docker image not yet built or tested** on the Ubuntu server.
- **Export not yet verified against a real Immich instance** (verified with ExifTool readback only).
- **Blank-back threshold** is tuned on synthetic images and needs calibration on real scans.
- **Rejected scans** can't be deleted from the UI (by design, originals are kept). Rejected scans can be re-opened from the *Rejected* tab and approved.

---

## 6. Dev environment issues

| Problem | Cause | Fix |
|---|---|---|
| `wsl -l -v`: "no installed distributions"; `wsl --status`: "virtualization is not enabled" | Windows feature *Virtual Machine Platform* pending a reboot | Restart. If `HKLM\...\Component Based Servicing\RebootPending` still exists, restart again and let "Working on updates" finish, then `wsl --install -d Ubuntu-24.04` |
| `Win32_Processor.VirtualizationFirmwareEnabled` = False while WSL works | known to misreport when Hyper-V/VBS is running | ignore; `HypervisorPresent: True` is the reliable signal |
| `wsl: Failed to translate 'E:\data\node'` | Windows PATH entries on missing drives | harmless warning |
| `TOMLDecodeError: Invalid statement (at line 1, column 1)` | config saved with a BOM by old tooling | fixed in 0.1.0 (BOM tolerated) |
| Slow builds / pip on `/mnt/c` | 9P filesystem | build via `scripts/wsl_build.sh` (rsyncs to Linux FS) |
| Edits made in `~/Photo_Scanner_Banana` inside WSL disappear | `wsl_build.sh` rsyncs **from** the Windows checkout with `--delete` | edit only the Windows checkout |
| Test warnings about `httpx2` / `BlockingPortal` | Starlette/AnyIO deprecations | harmless |
| Native module: `HAVE_OPENCV = False` | `libopencv-dev` missing at build time | `apt install libopencv-dev`, rebuild |
| Windows-only run skips 8 tests | no C++ toolchain on Windows | expected; run the suite in WSL |

---

## 7. Reporting a bug

Include:
1. What you did, what you expected, and what happened (screenshot of the page and the message).
2. Scan id(s) and the exact text typed (for date problems).
3. Output of `/health` and `/api/summary`.
4. [shell] The last 50 lines of the server log, and `banana doctor`.
5. For metadata problems [shell]: the `exiftool -G1 -s …` output from section 3.

Add confirmed bugs to the table in section 4 with the next `B-n` id.
