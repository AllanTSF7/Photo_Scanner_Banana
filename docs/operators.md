# Photo Scanner: Operator Guide

For whoever scans photos and reviews them in the Photo Scanner review page. No programming needed.

> **Current version (0.1):** ingest, review, approve and export all work. Reading handwriting
> automatically, confirming duplicates with AI, and stacking fronts with backs in Immich are **not built yet**.
> For now you type the date and names yourself.

---

## 1. What the system does

```
Scanner ──► inbox folder ──► Photo Scanner (you review) ──► photo library folder ──► Immich
```

1. The Epson FF-680W scans the front and back of each photo into the **inbox** folder.
2. **Ingest** pairs each front with its back, moves the files into the **archive**, and flags
   blank backs and possible rescans.
3. **You** check each photo: fill in the date, names, places and a description, then **Approve**
   or **Reject** it.
4. **Export** copies approved photos into the **library** folder in dated sub-folders, with the date
   and tags written into the file itself.
5. **Immich** shows them on its timeline with the right date and searchable tags.

Your original scans are never changed. They stay in the archive folder even if you reject a photo.

---

## 2. Before you start

| You need | Where |
|---|---|
| The review page | `http://<server>:8000` (local test on the laptop: <http://localhost:8000>) |
| The inbox folder | NAS share `photo_vault/inbox` (local test: `C:\Users\TSF2\Banana_Test\inbox`) |
| Epson FF-680W | network address `192.168.16.178`, powered on and on the network |

The top bar shows **System OK** with a green dot when everything the app needs is working. An amber dot ("N warnings")
means it works but something is degraded; a red dot ("N problems") means something is broken.
Click it to see every component (database, folders, ExifTool, scanner, …) with details. **Recheck** tests again.

The top bar of the review page also shows the scanner:
- **Scanner online** (green dot): ready. The **Scan feeder** button is enabled.
- **Scanner offline** (red dot): the app can't reach the scanner. Check that it's powered on and on Wi-Fi.
- **Scanning, N pages** (pulsing yellow dot): a scan is running.

The app scans directly from the scanner. You don't need Epson ScanSmart.
(Putting scan files into the inbox folder and clicking **Ingest inbox** still works as an alternative.)

**Scan settings** are set by the administrator in the config: both sides (duplex), color, 600 dpi, auto-crop and
skew correction. 600 dpi is the highest this scanner offers through the app. Blank backs are detected
and dropped later, so duplex is always on.

---

## 3. Daily workflow

### Step 1: Scan
1. Load a stack of photos into the feeder, following the scanner's loading guide.
   Keep photos of similar size together and don't overload the feeder. **Whole stack** stops on its own at
   36 photos even if more are loaded, so the hopper is never pushed past what it's rated for - if you have
   more than that, scan in batches of up to 36.
2. Choose **Feed**: **Whole stack** scans until the feeder is empty (or the 36-photo cap). **One photo** scans a
   single photo (front and back), which is handy for testing or rescanning one print. The button then reads
   **Scan one photo**.
3. Choose **After scan**:
   - **Add to review queue** (usual): photos go straight into **To review** when the scan finishes.
   - **Leave in inbox**: files are only saved into the inbox folder. Click **Ingest inbox** when you want to review them.
   The page remembers your choice.
4. Click **Scan feeder**. The top bar shows **Scanning, N photos**, then **Processing N photos**.
5. When the feeder is empty you see a message like "Scanned 10 photo(s), 20 page(s). Ingested 10 scan(s)"
   (or "…20 file(s) left in the inbox"). If it stops at "feeder capped at 36, scan again for more," load the
   rest and click **Scan feeder** again - nothing from the first batch is lost.

Each photo becomes one front and one back (duplex). A message about an "odd page count" means the last photo
has no back image. Check that photo.

**Using files instead:** put scan files in the inbox folder. The app checks the inbox every 15 seconds and adds
new photos to the review queue by itself once they've finished saving; **Ingest inbox** does it right away.
The file names must follow `<name>_0001.jpg` / `<name>_0001_b.jpg`; don't rename them.

### Step 2: Check the ingest message
The message shows how many scans came in, how many possible rescans were flagged, and any files that were
ignored because the name wasn't recognized.

### Step 3: Review each photo
The **To review** tab lists every new scan. Click one, or press <kbd>J</kbd> to start.

- **Left:** the queue. `back text` means the back has writing. `dup?` means it may be a rescan of an earlier photo.
- **Middle:** the front and back images.
- **Below:** the form.

Fill in:

| Field | What to enter | Example |
|---|---|---|
| **Date** | What's written on the back, or your best knowledge. The line below it shows how the app read it. | `Xmas '84` |
| **Description** | Text from the back, or a short note | `Christmas morning at Grandma's` |
| *(automatic)* | **People, Places and Events fill in from the description** as you type. Dashed chips (`+ Name`) are extra suggestions: click to add. Remove a wrong chip with ×; it won't come back for that photo. Names you've used on earlier photos are recognized better. | `Jimmy Dawley`, `Chocolat Design`, `Field Trip` |
| **People** | One name per chip. Press <kbd>Enter</kbd> or a comma after each. | `Grandma`, `John` |
| **Places** | Same as People | `Lake Erie` |
| **Events** | Same as People | `Christmas` |
| **Tags** | Anything else. Use `/` to group tags. | `Family/Smith` |

**Back image:**
- "has writing" backs are kept, and will be stacked under the front in Immich (once stacking is built).
- "looks blank" backs are dropped automatically. If the back actually has something, tick **keep & stack in Immich**.
- Press <kbd>B</kbd> to toggle.

**Text on back:** the app reads **printed** text on the back (labels, stickers, lab stamps) and, when the Date
and Description are still empty, fills them in. You'll see the date line say e.g. "Reads as 2006-01-12". Always check it:
- The **Text on back** panel lists every line it read with a confidence %. Faded lines are uncertain.
  Green-bordered lines are already in the description. **Click a line** to add it to the description.
- **Read text** reads the back again. Use it after rotating or swapping sides. It never overwrites what you typed.
- **Handwriting isn't read reliably yet.** Type handwritten notes yourself for now.

**Autocorrect.** A common misspelling on the label or in your own typing ("Januaru", "Thanksgivng", "Summre") is
fixed automatically in the Date and Description fields when you move on to the next field — you'll see the fix
listed underneath with the original crossed out. Click **Undo** right there to put back exactly what you typed.
It only touches words it's confident about (months, weekdays, seasons, common occasions) and never a name.

**Dashed means suggested.** Anything the app filled in or proposed (date, description, names, text lines,
"cropped", "has writing", "dup?") is drawn with a dashed outline until you change it or approve the scan. A solid
outline means you've confirmed it. Suggested names are not saved just because you looked at a photo: if any are
still dashed when you click **Approve**, the app asks whether to **Accept all** or **Leave them out**.

**Buttons and shortcuts.** Every action has a button. The letter shown on a button is its keyboard shortcut:
**Previous** `K`, **Next** `J`, **Approve** `A`, **Reject** `X`, **Flag duplicate** `D`, **Rotate right** `R`,
**Rotate left** `Shift+R`, keep back `B`, **Read text** `T`, **Save** `Ctrl+S`. **Export approved** and applying a
threshold ask you to confirm first.

**Teach the app (learning loop):** in **Text on back**, every line has two buttons:
- **Fix**: correct what was read (type the right text, press Enter; Escape cancels). The line shows *fixed* with the
  original crossed out.
- **Not text**: for lab codes, smudges or stamps that aren't real writing. **Restore** undoes it.

Fixes only count when you **approve** the scan; the message then says how many corrections were recorded.
After that, the same misreading is fixed automatically on later scans (shown as *auto-fixed*), and names you keep
removing stop being suggested. Nothing you haven't approved is ever used, and nothing leaves this computer.

The **Learning** button in the top bar shows how much the app has learned. **Threshold proposals** appear there once
there's enough evidence (for example, when to call a back "blank"). They change nothing until you press **Apply**, and
**Revert** undoes them.

**Fix the images** (these never change the original scan; they're applied when exporting):
- **Auto-rotate:** when the back has readable text, the app usually straightens the photo for you - both the front
  and the back come out upright without you touching Rotate. You'll see an **auto-rotated** chip next to whichever
  side it adjusted; it's dashed like any other suggestion until you approve the scan. If it guessed wrong (rare -
  it only rotates when it's confident), fix it with Rotate exactly as before. No back, a blank back, or writing
  it can't read at all: nothing is guessed, and rotation stays manual for that scan.
- **Rotate left / Rotate right** under the front or back image. Scans come out in whatever direction the photo was fed,
  so upside-down or sideways photos are normal. <kbd>R</kbd> rotates the front right, <kbd>Shift</kbd>+<kbd>R</kbd> left.
- **Swap front/back** (top right of the scan) when the picture shows up as the back.
- **Re-detect crop** when the photo isn't trimmed right. The chip next to "Front" says **cropped** or **full page**.

**Then decide:**
- **Approve** (<kbd>A</kbd>): the photo is ready to export. The next photo opens.
- **Reject** (<kbd>X</kbd>): the photo won't be exported. Use this for rescans and failed or blurry scans.
  The original stays in the archive - rejecting never deletes anything.
- **Save** (<kbd>Ctrl</kbd>+<kbd>S</kbd>): keep your edits and decide later.

Switching photos saves your edits automatically.

**Deleting a rejected photo for good:** open the **Rejected** tab and select it - a **Delete permanently**
button appears there (nowhere else). It asks you to confirm, then removes the original front/back files from
the archive and the record itself; there's no undo. It's refused if the photo was ever approved (even briefly)
or already exported, since that scan may carry corrections the learning loop depends on. Most rejects
(duplicates, misfeeds, blurry scans) are fine to delete; when in doubt, just leave it rejected - it costs
nothing to keep.

### Step 4: Export
Click **Export approved**. Approved photos move to the **Exported** tab, and the form shows where
each file went (for example `1984/1984-12-25/SCAN_000001_A.jpg`).

### Step 5: Refresh Immich
In Immich go to *Administration → External Libraries → Scan*. (This will become automatic later.)

---

## 4. Entering dates

Type what you know, the way you'd write it. The app keeps track of how exact the date is.

| You type | Understood as | Placed on the Immich timeline at | Folder |
|---|---|---|---|
| `12/25/84`, `Dec 25, 1984`, `1984-12-25` | exact day | 1984-12-25 | `1984/1984-12-25` |
| `Xmas '84`, `Christmas Eve 1990`, `Easter 1985`, `4th of July 1976` | exact day (holiday) | that day | `1984/1984-12-25` |
| `Dec 84`, `June 1979` | month | 1st of the month | `1984/1984-12` |
| `Summer of '79`, `Fall 1988` | season | mid-season (Jul 15, Oct 15, Apr 15, **Jan 15 for winter**) | `1979/1979-summer` |
| `1984`, `'84` | year | July 1 | `1984/1984-undated` |
| `circa 1950`, `~1950`, `about 1950` | approximate year | July 1 | `1950/1950-undated` |
| `the 1960s`, `70's` | decade | mid-decade (1965-07-01) | `undated/<batch>` |
| *(empty)* | unknown | **the export date** (see below) | `undated/<batch>` |

Anything less exact than a day gets "(date approx: …)" added to its description and a `Scan/DateApprox` tag,
so you can find those photos later in Immich.

**Watch out for:**
- **Numbers-only dates are read US-style:** `03/04/85` means March 4. Type `4 Mar 1985` if you mean 4 March.
- **Two-digit years:** `'24` means 2024 and `'27` means 1927. The cut-off is the current year.
- **Unknown dates:** these show up in Immich on the day you exported them. Even a rough guess
  (`1970s`) is better than leaving the field empty.
- **Unreadable dates:** if the line under the Date field turns red, the app couldn't read it.
  Reword it (`Dec 1984`) or leave the field empty.

---

## 5. Duplicates (`dup?`)

The app compares each new front with every photo already ingested. A `dup?` mark means the
photo *looks similar* to an earlier one. It may be a rescan, a reprint, or just a similar-looking photo.

- Open both scans. The warning names the other scan ("possible rescan of #1").
- **Real duplicate:** reject the worse copy and approve the better one.
- **Not a duplicate:** ignore the warning and review normally.

The check is simple for now and gives some false alarms, especially for photos with similar layouts.
A smarter comparison is planned.

---

## 6. Checking Immich for duplicates

An optional, separate check: "have I already scanned and imported this photo into Immich?" It's off until you
turn it on, and it only ever **reads** from Immich - it never uploads, edits, stacks, or triggers a library scan
there.

**Turning it on:** open the **Immich** button in the top bar, fill in your Immich server URL, an API key
(*Immich → Account Settings → API Keys*), then tick **Enabled** and **Save**. **Library ID** is optional: leave it blank
to compare against every photo the key can see, or enter an External Library's ID to limit the check to that one library.
Only photos are compared, never videos. The first run reads every photo's thumbnail (roughly 80,000 photos can take
an hour or more); later runs only look at new or changed photos, and the message under the button shows progress.
Use **Test connection** to check the details work before relying on it. The key is never shown again once
saved - only its last 4 characters, so you can tell which key is in use.

**Running it:** click **Check Immich for duplicates**. It runs in the background (you can keep reviewing while
it works) and does two things:
- Checks whether the exact file you already **exported** matches something already in Immich (byte-for-byte).
- Compares every scan's photo against thumbnails already in your Immich library, the same "looks similar" check
  used for local rescans (see section 5), just pointed at your Immich library instead.

A match shows as an **in Immich?** chip on that scan, dashed until you confirm or clear it with the flag button
next to it - the same kept/removed/added pattern as **Flag duplicate**.

**What it can't do:** it can't tell you a photo is a duplicate before you've exported it (export changes the
file, so only the exported copy can ever match byte-for-byte), and it can't ask Immich "have you seen anything
like this?" directly - Immich has no such feature, so the "looks similar" half of the check is computed here,
not by Immich.

---

## 7. Tags in Immich

What you enter becomes these Immich tags:

| Form field | Immich tag |
|---|---|
| People: `John` | `People/John` |
| Places: `Lake Erie` | `Places/Lake Erie` |
| Events: `Christmas` | `Events/Christmas` |
| Tags: `Family/Smith` | `Family/Smith` |
| Batch box label | `Box/Attic Box 3` |
| (automatic) | `Scan/HasBack`, `Scan/Back`, `Scan/DateApprox` |

People names become **tags**, not Immich "People" (faces). Name faces in Immich itself.
A `/` inside a name is changed to `-`, because `/` separates tag levels.

---

## 8. Do and don't

**Do**
- Finish scanning a stack before you ingest it.
- If a bar appears under the header about a scan, read it: **Recover scans** brings kept pages into the queue.
- Reject bad scans instead of deleting files.
- Add at least a decade to undated photos.

**Don't**
- Rename, edit or delete files in the **archive** or **library** folders by hand. The app tracks them.
- Edit photo details in Immich and expect them in the app. Changes only flow from the app to Immich.
- Put non-scan files in the inbox. They're ignored and left where they are.

---

## 9. Keyboard shortcuts

| Key | Action |
|---|---|
| <kbd>J</kbd> / <kbd>K</kbd> | next / previous photo |
| <kbd>A</kbd> | approve |
| <kbd>X</kbd> | reject |
| <kbd>B</kbd> | keep / drop the back |
| <kbd>R</kbd> / <kbd>Shift</kbd>+<kbd>R</kbd> | rotate the front right / left |
| <kbd>Ctrl</kbd>+<kbd>S</kbd> | save |
| <kbd>Enter</kbd> or <kbd>,</kbd> | add a chip (People, Places, Events, Tags) |
| <kbd>Backspace</kbd> in an empty chip box | remove the last chip |

Shortcuts don't fire while you're typing in a field. Click outside the field first.

Something not working? See [diagnostics&bugs.md](diagnostics&bugs.md).
