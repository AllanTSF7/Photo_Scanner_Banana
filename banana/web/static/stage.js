"use strict";
/* The stage, the status strip, the batch-level label and the error wording. Rules: CLAUDE.md > UI DESIGN SYSTEM.
   - The stage is one area that changes with what is happening (idle, scanning, review, export, done, attention),
     with one primary action per state and plain language first, codes second. It shows whenever no scan is open.
   - The status strip is always visible; every item is text plus a mark plus colour, never colour alone.
   - Batch-level labelling fills empty fields on a whole stack; only the exceptions then need a look.
   - Every error says what happened, what happened to the scans, what to do, then a small copyable code,
     from the single table below. No error state is a dead end.
   Loaded after app.js (shares its globals: state, api, toast, $, ...); startup happens on DOMContentLoaded. */

// ---------- the error table: one place for every failure's wording ----------
const ERRORS = {
  API_UNREACHABLE: {
    title: "The app's server is not responding.",
    scans: "Scans on disk are unaffected; nothing was changed.",
    next: "Start the Photo Scanner service again, then reload this page.",
    action: { label: "Reload page", run: () => location.reload() },
  },
  SCAN_OFFLINE: {
    title: "The scanner is not answering.",
    scans: "Nothing was scanned and nothing changed.",
    next: "Check that the scanner is switched on and on the network, then scan again.",
    action: { label: "Open system health", run: () => toggleHealth(true) },
  },
  SCAN_FAILED: {
    title: "The scan stopped before it finished.",
    scans: "Pages already scanned stay in the inbox's scanning folder; nothing was deleted.",
    next: "Clear the feeder (a jam or a double feed is the usual cause), then scan again. Ingest inbox picks up any pages left behind.",
    action: { label: "Ingest inbox", run: () => $("btn-ingest").click() },
  },
  SCAN_NO_PAGES: {
    title: "The scanner returned no pages.",
    scans: "Nothing changed.",
    next: "Load photos into the feeder, face as the scanner expects, and scan again.",
    action: { label: "Scan feeder", run: () => $("btn-scan").click() },
  },
  INGEST_FAILED: {
    title: "The inbox could not be ingested.",
    scans: "Files that were not ingested are still in the inbox. Originals are never modified.",
    next: "Open system health to check the inbox and archive folders, then try Ingest inbox again.",
    action: { label: "Open system health", run: () => toggleHealth(true) },
  },
  EXPORT_PARTIAL: {
    title: "Some scans could not be exported.",
    scans: "Originals in the archive are untouched. Scans that exported and verified are in the library; the failed ones were not written there.",
    next: "Open each failed scan below, fix what the message says, and export again. Only approved scans are retried.",
    action: { label: "Show approved scans", run: () => selectTab("approved") },
  },
  EXPORT_FAILED: {
    title: "The export did not run.",
    scans: "Nothing was written to the library and originals are untouched.",
    next: "Check ExifTool and the library folder in system health, then export again.",
    action: { label: "Open system health", run: () => toggleHealth(true) },
  },
  FOLDER_PROBLEM: {
    title: "A folder the app needs is missing or not writable.",
    scans: "Nothing is moved or written until the folder is back.",
    next: "Open system health for the folder's path, create it or fix its permissions, then press Recheck.",
    action: { label: "Open system health", run: () => toggleHealth(true) },
  },
  DATE_UNREADABLE: {
    title: "That date was not understood.",
    scans: "Nothing was saved.",
    next: "Write it the way it is on the print: Dec 1984, '84, Summer 1979, 1960s.",
  },
  IMMICH_UNREACHABLE: {
    title: "Immich could not be reached.",
    scans: "Nothing was sent or changed; the check is read-only anyway.",
    next: "Check the server URL and API key in the Immich panel, then Test connection again.",
    action: { label: "Open Immich panel", run: () => toggleImmich(true) },
  },
  IMMICH_CHECK_FAILED: {
    title: "The Immich check stopped.",
    scans: "No scan was changed; matches found so far are kept as warnings.",
    next: "Run the check again from the Immich panel.",
    action: { label: "Open Immich panel", run: () => toggleImmich(true) },
  },
  REQUEST_FAILED: {
    title: "The app refused that request.",
    scans: "Nothing was changed.",
    next: "Read the message, adjust, and try again. If it keeps happening, open system health.",
  },
};

/** Code for a scan run that ended in "failed": the scanner's own message decides between the two scan failures. */
function scanFailureCode(message) {
  return /no pages/i.test(message || "") ? "SCAN_NO_PAGES" : "SCAN_FAILED";
}

/** Best code for an error the API or the browser produced. */
function errorCode(err) {
  if (err?.code) return err.code;
  const message = String(err?.message || "");
  if (/could not understand date/i.test(message)) return "DATE_UNREADABLE";
  if (/scanner.*(offline|not reachable|refused|timed out)/i.test(message)) return "SCAN_OFFLINE";
  if (/no pages/i.test(message)) return "SCAN_NO_PAGES";
  if (/exiftool/i.test(message)) return "EXPORT_FAILED";
  if (/immich/i.test(message)) return "IMMICH_UNREACHABLE";
  return "REQUEST_FAILED";
}

/** Toast wording: what happened (the detail), what to do, then the code. */
function explainError(err) {
  const code = errorCode(err);
  const entry = ERRORS[code] || ERRORS.REQUEST_FAILED;
  const detail = String(err?.message || "").trim();
  const lines = [detail && detail !== entry.title ? detail : entry.title];
  if (entry.next) lines.push(entry.next);
  lines.push(`[${code}]`);
  return lines.join("\n");
}

// ---------- status strip ----------
const FOLDER_CHECKS = ["inbox", "archive", "library", "data"];

function setStripItem(id, status, text) {
  const el = $(id);
  el.dataset.status = status;
  el.querySelector(".strip-text").textContent = text;
}

function runningJobs() {
  const jobs = [];
  if (lastScanner?.scan?.state === "scanning") jobs.push(lastScanner.scan.phase === "ingesting" ? "processing scans" : "scanning");
  if (lastImmichCheck?.state === "running") jobs.push("Immich check");
  return jobs;
}

function renderStrip() {
  const checks = lastHealth?.checks || [];
  const folders = checks.filter((c) => FOLDER_CHECKS.includes(c.name));
  if (!lastHealth) setStripItem("folders-status", "idle", "Folders");
  else if (folders.some((c) => c.status === "fail")) setStripItem("folders-status", "fail", "Folder problem");
  else if (folders.some((c) => c.status === "warn")) setStripItem("folders-status", "warn", "Folders: warning");
  else setStripItem("folders-status", "ok", "Folders OK");

  const immich = checks.find((c) => c.name === "immich");
  const immichEl = $("immich-status");
  immichEl.hidden = !immich || immich.status === "off";
  if (!immichEl.hidden) {
    setStripItem("immich-status", immich.status === "ok" ? "ok" : immich.status === "warn" ? "warn" : "fail",
      immich.status === "ok" ? "Immich on" : immich.status === "warn" ? "Immich: warning" : "Immich unreachable");
  }

  const jobs = runningJobs();
  setStripItem("jobs-status", jobs.length ? "busy" : "idle",
    jobs.length ? `${jobs.length === 1 ? "1 job" : `${jobs.length} jobs`} running: ${jobs.join(", ")}` : "No jobs running");
}

// ---------- the stage ----------
const SCAN_STEPS = [
  ["scan", "Scan"],
  ["crop", "Crop"],
  ["pair", "Pair front and back"],
  ["text", "Read text"],
  ["rescans", "Check for rescans"],
];
let stageEventState = null; // {kind: "done" | "attention", data}
let recentBatches = { at: 0, items: [], loading: null };

function stageEvent(kind, data) {
  stageEventState = { kind, data, at: Date.now() };
  renderStage();
}

/** Clear the stage's last outcome. With a kind, only an outcome of that kind (an API outage that has recovered). */
function clearStageEvent(kind = null) {
  if (!stageEventState) return;
  if (kind && stageEventState.data?.kind !== kind) return;
  stageEventState = null;
  renderStage();
}

function stageButton(id, spec) {
  const button = $(id);
  if (!spec) { button.hidden = true; button.onclick = null; return; }
  button.hidden = false;
  button.textContent = spec.label;
  button.className = `btn ${spec.cls || "primary"}`;
  button.disabled = !!spec.disabled;
  button.onclick = spec.run;
}

function paintStage({ kind, kicker, title, body = "", steps = null, detail = null, code = null, primary = null, secondary = null }) {
  const el = $("stage");
  el.dataset.stage = kind;
  $("stage-kicker").textContent = kicker;
  $("stage-title").textContent = title;
  $("stage-body").textContent = body;
  $("stage-body").hidden = !body;
  const list = $("stage-steps");
  list.hidden = !steps;
  if (steps) {
    list.replaceChildren(...SCAN_STEPS.map(([key, label]) => {
      const li = document.createElement("li");
      li.dataset.key = key;
      li.dataset.step = steps[key] || "pending";
      li.textContent = label;
      return li;
    }));
  }
  const detailEl = $("stage-detail");
  if (detail) detailEl.replaceChildren(detail); else detailEl.replaceChildren();
  $("stage-code").hidden = !code;
  $("stage-code-text").textContent = code || "";
  stageButton("stage-primary", primary);
  stageButton("stage-secondary", secondary);
}

function definitionList(pairs) {
  const dl = document.createElement("dl");
  for (const [term, value] of pairs) {
    if (value == null || value === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    if (value instanceof Node) dd.append(value); else dd.textContent = value;
    dl.append(dt, dd);
  }
  return dl;
}

function warningList(items) {
  const ul = document.createElement("ul");
  ul.className = "stage-warnings";
  for (const text of items) {
    const li = document.createElement("li");
    li.textContent = text;
    ul.append(li);
  }
  return ul;
}

async function loadRecentBatches() {
  if (Date.now() - recentBatches.at < 10000) return recentBatches.items;
  if (recentBatches.loading) return recentBatches.loading;
  recentBatches.loading = api("GET", "/api/batches?limit=5")
    .then((items) => { recentBatches = { at: Date.now(), items, loading: null }; renderStage(); return items; })
    .catch(() => { recentBatches.loading = null; return recentBatches.items; });
  return recentBatches.loading;
}

function recentBatchesList() {
  const wrap = document.createElement("div");
  const heading = document.createElement("p");
  heading.className = "muted";
  heading.textContent = recentBatches.items.length ? "Recent batches" : "No batches yet.";
  wrap.append(heading);
  if (!recentBatches.items.length) return wrap;
  const ul = document.createElement("ul");
  ul.className = "recent-batches";
  for (const b of recentBatches.items) {
    const li = document.createElement("li");
    li.dataset.batchId = b.id;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = b.name;
    const summary = document.createElement("span");
    summary.className = "muted";
    const parts = [plural(b.scans, "scan")];
    if (b.counts.needs_review) parts.push(`${b.counts.needs_review} to review`);
    if (b.counts.exported) parts.push(`${b.counts.exported} exported`);
    if (b.no_date) parts.push(`${b.no_date} without a date`);
    parts.push(b.audience);
    summary.textContent = parts.join(" · ");
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Open";
    open.setAttribute("aria-label", `Open batch ${b.name}`);
    open.onclick = () => selectTab(b.counts.needs_review ? "needs_review" : "", { batchId: b.id });
    li.append(name, summary, open);
    ul.append(li);
  }
  wrap.append(ul);
  return wrap;
}

function scannerConfigured() {
  return !!lastScanner?.configured;
}

function renderStage() {
  const el = $("stage");
  if (!$("editor").hidden) { el.hidden = true; return; }
  el.hidden = false;
  const scanner = lastScanner;
  const scanning = scanner?.scan?.state === "scanning";
  const cap = scanner?.max_feeder_count;

  if (scanning) {
    const phase = scanner.scan.phase;
    const photos = plural(scanPhotos(scanner), "photo");
    const steps = phase === "ingesting"
      ? { scan: "done", crop: "current", pair: "current", text: "current", rescans: "current" }
      : { scan: "current" };
    paintStage({
      kind: "scanning", kicker: phase === "ingesting" ? "Processing" : "Scanning",
      title: phase === "ingesting" ? `Processing ${photos}.` : `Scanning the feeder: ${photos} so far.`,
      body: phase === "ingesting"
        ? "Each page is cropped, paired front to back, read for text and checked against earlier scans. The originals go to the archive untouched."
        : "Photos appear in the review queue when the feeder is empty. A scan can't be interrupted from the app; let the feeder run out.",
      steps,
      primary: { label: "Open system health", cls: "ghost", run: () => toggleHealth(true) },
    });
    return;
  }

  const outcome = stageEventState;
  if (outcome?.kind === "attention") { renderAttention(outcome.data); return; }
  if (outcome?.kind === "done") { renderDone(outcome.data); return; }

  const toReview = state.counts.needs_review ?? 0;
  const approved = state.counts.approved ?? 0;
  const scanLabel = scannerConfigured() ? "Scan feeder" : "Ingest inbox";
  const scanRun = () => (scannerConfigured() ? $("btn-scan") : $("btn-ingest")).click();
  const scannerOffline = scannerConfigured() && !scanner.online;

  if (state.visible.length) {
    paintStage({
      kind: "review", kicker: "Review",
      title: "Select a scan to review.",
      body: "Every action has a button; the letters on buttons are keyboard shortcuts.",
      primary: { label: "Open first scan", run: () => selectScan(state.visible[0].id, true) },
    });
    return;
  }

  if (state.status === "needs_review" && state.batchId == null) {
    if (state.exceptionsOnly && state.scans.length) {
      paintStage({
        kind: "review", kicker: "Review",
        title: "No exceptions left to decide.",
        body: `${plural(state.scans.length, "scan")} in this batch are ready for a last look and approval.`,
        primary: { label: "Show all scans", run: () => { $("exceptions-only").checked = false; $("exceptions-only").dispatchEvent(new Event("change")); } },
      });
      return;
    }
    if (approved) {
      paintStage({
        kind: "export", kicker: "Ready to export",
        title: `${plural(approved, "approved scan")} ready to export.`,
        body: "Photos and sidecars are written into the library folder that Immich reads. Nothing is uploaded; Immich only reads that folder.",
        detail: definitionList([
          ["Goes to", "the library folder in system health (Library)"],
          ["Who may see it", "the tier set on each batch; private unless changed"],
          ["Originals", "stay in the archive, unchanged"],
        ]),
        primary: { label: `Export ${plural(approved, "scan")}`, cls: "irreversible", run: () => $("btn-export").click() },
        secondary: { label: scanLabel, cls: "ghost", run: scanRun, disabled: scannerOffline },
      });
      return;
    }
    loadRecentBatches();
    paintStage({
      kind: "idle", kicker: scannerOffline ? "Scanner offline" : "Idle",
      title: scannerOffline ? "The scanner is not answering." : "Load photos into the feeder.",
      body: scannerOffline
        ? "Check that it is switched on and on the network. You can still ingest files already in the inbox."
        : scannerConfigured()
          ? `Up to ${cap || 36} prints at a time, face as the scanner expects. Each photo is scanned front and back.`
          : "No scanner is configured. Put scan files in the inbox folder and ingest them.",
      detail: recentBatchesList(),
      code: scannerOffline ? "SCAN_OFFLINE" : null,
      primary: scannerOffline
        ? { label: "Ingest inbox", run: () => $("btn-ingest").click() }
        : { label: scanLabel, run: scanRun },
      secondary: scannerOffline ? { label: "Open system health", cls: "ghost", run: () => toggleHealth(true) }
        : scannerConfigured() ? { label: "Ingest inbox", cls: "ghost", run: () => $("btn-ingest").click() } : null,
    });
    return;
  }

  // Another tab (or one batch) with nothing in it.
  const label = { approved: "approved", exported: "exported", rejected: "rejected", "": "" }[state.status] ?? state.status;
  paintStage({
    kind: "review", kicker: state.batchId != null ? "Batch" : statusLabel(state.status),
    title: state.batchId != null ? "Nothing in this batch for this filter." : label ? `No ${label} scans yet.` : "No scans yet.",
    body: toReview ? `${plural(toReview, "scan")} are waiting in To review.` : "",
    primary: toReview
      ? { label: "Go to To review", run: () => selectTab("needs_review", { batchId: null }) }
      : { label: scanLabel, run: scanRun, disabled: scannerOffline },
    secondary: state.batchId != null ? { label: "Show all batches", cls: "ghost", run: () => selectTab(state.status, { batchId: null }) } : null,
  });
}

function renderDone(data) {
  const next = { label: scannerConfigured() ? "Next batch: scan feeder" : "Next batch: ingest inbox",
    run: () => { clearStageEvent(); (scannerConfigured() ? $("btn-scan") : $("btn-ingest")).click(); } };
  if (data.kind === "export") {
    const r = data.result;
    const warnings = (r.warnings || []).slice();
    paintStage({
      kind: "done", kicker: "Done",
      title: `Exported ${plural(r.exported.length, "scan")}.`,
      body: "Each photo was written with its metadata, verified by reading it back, then moved into place with its sidecar.",
      detail: (() => {
        const wrap = document.createElement("div");
        const files = r.exported.flatMap((e) => [e.front, e.back].filter(Boolean));
        wrap.append(definitionList([
          ["Written", `${plural(files.length, "file")} plus sidecars`],
          ["First file", files[0] || null],
          ["Immich", "reads the library folder on its next scan; nothing was uploaded"],
        ]));
        if (warnings.length) wrap.append(warningList(warnings));
        return wrap;
      })(),
      primary: next,
      secondary: { label: "Show exported scans", cls: "ghost", run: () => { clearStageEvent(); selectTab("exported"); } },
    });
    return;
  }
  if (data.kind === "scan-inbox") {
    paintStage({
      kind: "done", kicker: "Done",
      title: "Scan finished. The files are waiting in the inbox.",
      body: data.run.message,
      primary: { label: "Ingest inbox", run: () => { clearStageEvent(); $("btn-ingest").click(); } },
      secondary: { label: "Scan more", cls: "ghost", run: () => { clearStageEvent(); $("btn-scan").click(); } },
    });
    return;
  }
  clearStageEvent();
}

function renderAttention(data) {
  if (data.kind === "api") {
    const e = ERRORS.API_UNREACHABLE;
    paintStage({ kind: "attention", kicker: "Needs attention", title: e.title, body: `${e.scans} ${e.next}`,
      code: "API_UNREACHABLE", primary: { label: e.action.label, run: e.action.run } });
    return;
  }
  if (data.kind === "scan") {
    const message = data.run?.message || "";
    const code = scanFailureCode(message);
    const e = ERRORS[code];
    paintStage({
      kind: "attention", kicker: "Needs attention", title: e.title,
      detail: definitionList([["Scanner said", message], ["Your scans", e.scans], ["What to do", e.next]]),
      code,
      primary: { label: e.action.label, run: () => { clearStageEvent(); e.action.run(); } },
      secondary: { label: "Dismiss", cls: "ghost", run: () => clearStageEvent() },
    });
    return;
  }
  if (data.kind === "export") {
    const r = data.result;
    const e = ERRORS.EXPORT_PARTIAL;
    const wrap = document.createElement("div");
    wrap.append(definitionList([["Exported", plural(r.exported.length, "scan")], ["Failed", plural(r.errors.length, "scan")], ["Your scans", e.scans], ["What to do", e.next]]));
    wrap.append(warningList(r.errors.map((x) => `#${padId(x.id)}: ${x.error}`)));
    paintStage({
      kind: "attention", kicker: "Needs attention", title: e.title, detail: wrap, code: "EXPORT_PARTIAL",
      primary: { label: e.action.label, run: () => { clearStageEvent(); e.action.run(); } },
      secondary: { label: "Dismiss", cls: "ghost", run: () => clearStageEvent() },
    });
    return;
  }
  clearStageEvent();
}

$("btn-stage-copy").onclick = async () => {
  const code = $("stage-code-text").textContent;
  try { await navigator.clipboard.writeText(code); toast(`Copied ${code}`); }
  catch { toast(`Code: ${code}`); }
};

// ---------- batch filter chip in the queue (set from the stage's recent batches) ----------
function renderBatchFilter() {
  let chip = document.getElementById("queue-batch-filter");
  if (state.batchId == null) { chip?.remove(); return; }
  if (!chip) {
    chip = document.createElement("button");
    chip.type = "button";
    chip.id = "queue-batch-filter";
    chip.className = "chip";
    chip.title = "Showing one batch. Click to show every batch.";
    chip.onclick = () => selectTab(state.status, { batchId: null });
    $("queue-count").after(chip);
  }
  const known = recentBatches.items.find((b) => b.id === state.batchId);
  chip.textContent = `batch ${known ? known.name : `#${state.batchId}`} ×`;
}

// ---------- batch-level label: one label for the whole stack ----------
let batchCache = { id: null, data: null, stale: true };
let batchFormDirty = false;

function invalidateBatch() {
  batchCache.stale = true;
  recentBatches.at = 0; // the stage's recent-batches list is refetched on its next render
}

async function renderBatchCard(scan) {
  const card = $("batch-card");
  if (!scan || scan.batch_id == null) { card.hidden = true; return; }
  card.hidden = false;
  if (batchCache.id !== scan.batch_id) { batchFormDirty = false; $("batch-form").hidden = true; $("btn-batch-toggle").setAttribute("aria-expanded", "false"); }
  if (batchCache.id !== scan.batch_id || batchCache.stale) {
    let data;
    try { data = await api("GET", `/api/batches/${scan.batch_id}`); } catch { return; }
    batchCache = { id: scan.batch_id, data, stale: false };
  }
  if (state.current?.batch_id !== batchCache.id) return;
  const b = batchCache.data;
  $("batch-name").textContent = b.name;
  const parts = [plural(b.scans, "scan"), `${b.counts.needs_review} to review`];
  if (b.no_date) parts.push(`${b.no_date} without a date`);
  if (b.possible_rescans) parts.push(`${b.possible_rescans} possible rescan${b.possible_rescans === 1 ? "" : "s"}`);
  if (b.box_label) parts.push(b.box_label);
  parts.push(b.audience === "family" ? "shared with family" : "private");
  $("batch-summary").textContent = parts.join(" · ");
  if (!batchFormDirty) {
    $("batch-audience").value = b.audience;
    $("batch-box").value = b.box_label || "";
  }
  const n = b.counts.needs_review;
  $("btn-batch-apply").textContent = n ? `Apply to ${plural(n, "scan")}` : "Nothing left to review in this batch";
  $("btn-batch-apply").disabled = n === 0 && !batchFormDirty;
}

function toggleBatchForm(open = $("batch-form").hidden) {
  $("batch-form").hidden = !open;
  $("btn-batch-toggle").setAttribute("aria-expanded", String(open));
  $("btn-batch-toggle").textContent = open ? "Hide batch label" : "Label whole batch";
  if (open) $("batch-date").focus();
}
$("btn-batch-toggle").onclick = () => toggleBatchForm();
$("btn-batch-cancel").onclick = () => toggleBatchForm(false);
$("batch-form").addEventListener("input", () => { batchFormDirty = true; $("btn-batch-apply").disabled = false; });
$("batch-date-preview").dataset.emptyText = "Leave blank to keep each scan's own date.";
wireDatePreview($("batch-date"), $("batch-date-preview"));
renderDatePreview(null, null, $("batch-date-preview"));

const splitList = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);

$("batch-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (batchCache.id == null) return;
  const body = {
    audience: $("batch-audience").value,
    box_label: $("batch-box").value,
    date_text: $("batch-date").value.trim(),
    places: splitList($("batch-places").value),
    events: splitList($("batch-events").value),
    tags: splitList($("batch-tags").value),
    replace: $("batch-replace").checked,
  };
  if (body.audience === "family" && batchCache.data?.audience !== "family") {
    const ok = await confirmAction({
      title: "Mark this batch for the family?",
      body: "Records on the batch that these photos may be seen by the family. Nothing is moved or shared yet; the tier travels with the export.",
      confirmLabel: "Mark as family",
      note: "You can set it back to private here at any time.",
    });
    if (!ok) { $("batch-audience").value = batchCache.data?.audience || "private"; return; }
  }
  await withButton($("btn-batch-apply"), async () => {
    if (!(await confirmLeave())) return; // the open scan's own edits are saved first, never overwritten
    const r = await api("PATCH", `/api/batches/${batchCache.id}`, body);
    const parts = [];
    if (r.touched.date) parts.push(`date set on ${plural(r.touched.date, "scan")}`);
    for (const f of ["places", "events", "tags"]) if (r.touched[f]) parts.push(`${f} ${body.replace ? "replaced" : "added"} on ${r.touched[f]}`);
    toast(parts.length ? `Batch labelled: ${parts.join(", ")}` : "Batch saved. No scan needed a change.");
    batchFormDirty = false;
    $("batch-state").textContent = parts.length ? `applied to ${plural(r.updated, "scan")}` : "";
    for (const id of ["batch-date", "batch-places", "batch-events", "batch-tags"]) $(id).value = "";
    renderDatePreview(null, null, $("batch-date-preview"));
    invalidateBatch();
    recentBatches.at = 0;
    await refresh();
    if (state.selectedId != null) await selectScan(state.selectedId, true, true);
  });
});
