"use strict";
/* Photo Scanner review UI: the queue and the scan editor. Rules: CLAUDE.md > UI DESIGN SYSTEM.
   - Every action has a visible labeled control; shortcuts are declared with data-shortcut on that control.
   - Suggested values carry data-state="suggested" until the operator commits them (edit or approve).
   - An async refresh never replaces a field that has focus or uncommitted input, and never rebuilds the queue DOM:
     rows are updated in place by id, so selection, scroll and focus survive every poll.
   - Irreversible actions (export, apply threshold, delete) ask for confirmation.
   The status strip, the state-driven stage, the batch-level label and the error wording live in stage.js;
   the guided tour in tour.js. Startup runs on DOMContentLoaded, after every script has loaded. */

const STATUSES = [
  ["needs_review", "To review"],
  ["approved", "Approved"],
  ["exported", "Exported"],
  ["rejected", "Rejected"],
  ["", "All"],
];
const CHIP_FIELDS = ["people", "places", "events", "tags"];
const ENTITY_FIELDS = ["people", "places", "events"];
const COMMITTED = ["approved", "exported", "stacked"];

const state = {
  status: "needs_review",
  batchId: null, // optional queue filter: one batch only (set from the stage's recent batches)
  scans: [], // everything the current tab holds
  visible: [], // what the queue shows (exceptions filter applied); navigation uses this list
  exceptionsOnly: false,
  counts: {},
  noDate: 0,
  selectedId: null,
  dirty: false,
  current: null, // last scan object shown in the editor
  edited: new Set(), // fields the operator changed on the current scan (date, description, chip fields, ocr)
};
const $ = (id) => document.getElementById(id);

async function api(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw Object.assign(new Error("The app's server is not responding."), { code: "API_UNREACHABLE", cause: err });
  }
  if (res.status === 401) {  // session expired or signed out elsewhere
    location.replace("/login");
    throw new Error("Signed out");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* not JSON */ }
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw Object.assign(new Error(message), { status: res.status });
  }
  return res.json();
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3000);
}
/** Failure toast in the house style: what happened, then what to do, then the code (stage.js owns the table). */
function failToast(err) {
  toast(typeof explainError === "function" ? explainError(err) : err.message, true);
}

const statusLabel = (s) => (STATUSES.find(([k]) => k === s) || [s, s])[1];
const isCommitted = (scan) => COMMITTED.includes(scan?.status);
const sameValue = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const padId = (id) => String(id).padStart(6, "0");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/** Mark an element as provisional (suggested by the app) or committed. */
function setSuggested(el, suggested) {
  if (suggested) el.dataset.state = "suggested";
  else delete el.dataset.state;
}

// ---------- confirm dialog (irreversible actions only) ----------
function confirmAction({ title, body, confirmLabel, note = "This can't be undone from the app." }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-ok").textContent = confirmLabel;
    dialog.querySelector(".confirm-note").textContent = note;
    dialog.returnValue = "";
    dialog.onclose = () => resolve(dialog.returnValue === "confirm");
    dialog.showModal();
    $("confirm-cancel").focus();
  });
}

// ---------- keyboard shortcuts: declared on the control, rendered on it, dispatched to it ----------
const KEY_NAMES = { ctrl: "Ctrl", shift: "Shift" };
const formatShortcut = (combo) => combo.split("+").map((p) => KEY_NAMES[p] || p.toUpperCase()).join("+");

function renderShortcutHints(root = document) {
  root.querySelectorAll("[data-shortcut]").forEach((el) => {
    const host = el.tagName === "INPUT" ? el.closest("label") : el;
    if (!host || host.querySelector(":scope > .shortcut-hint")) return;
    const hint = document.createElement("kbd");
    hint.className = "shortcut-hint";
    hint.setAttribute("aria-hidden", "true");
    hint.textContent = formatShortcut(el.dataset.shortcut);
    host.append(hint);
    el.setAttribute("aria-keyshortcuts", el.dataset.shortcut.replace("ctrl", "Control").split("+")
      .map((p) => p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1)).join("+"));
  });
}

function usable(el) {
  return !el.disabled && !el.closest("[hidden]") && el.getClientRects().length > 0;
}

document.addEventListener("keydown", (e) => {
  if (e.altKey || $("confirm-dialog").open) return;
  if (e.key === "Escape") {
    if (!$("health-panel").hidden) toggleHealth(false);
    if (!$("learning-panel").hidden) toggleLearning(false);
    if (!$("immich-panel").hidden) toggleImmich(false);
    return;
  }
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push("ctrl");
  if (e.shiftKey) parts.push("shift");
  parts.push(e.key.toLowerCase());
  const combo = parts.join("+");
  const typing = e.target.closest("input:not([type=checkbox]), textarea, select, [contenteditable]");
  if (typing && combo !== "ctrl+s") return; // shortcuts never fire while typing, except Save
  const target = [...document.querySelectorAll(`[data-shortcut="${combo}"]`)].find(usable);
  if (!target) return;
  e.preventDefault();
  target.click();
});

// ---------- chip fields ----------
function chipsInput(container, field) {
  let values = [];
  let provisional = new Set(); // lower-cased values the app suggested and the operator hasn't committed
  let extra = []; // derived values not in the field: offered as "+ value"
  const dismissed = new Set();
  let touched = false;
  let autoFill = true;
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "add…";
  input.setAttribute("aria-label", `Add ${field}`);
  const has = (list, value) => list.some((v) => v.toLowerCase() === value.toLowerCase());

  const touch = () => {
    touched = true;
    provisional.clear(); // editing a field commits what's left in it
    state.edited.add(field);
  };

  const render = () => {
    const pending = extra.filter((s) => !has(values, s) && !dismissed.has(s.toLowerCase()));
    // Keep the text input mounted: re-inserting it would drop focus and lose what's being typed.
    [...container.children].forEach((child) => { if (child !== input) child.remove(); });
    if (input.parentNode !== container) container.append(input);
    input.before(
      ...values.map((v, i) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = v;
        setSuggested(chip, provisional.has(v.toLowerCase()));
        const x = document.createElement("button");
        x.type = "button";
        x.textContent = "×";
        x.title = `Remove ${v}`;
        x.setAttribute("aria-label", `Remove ${v}`);
        x.onclick = () => {
          values.splice(i, 1);
          dismissed.add(v.toLowerCase());
          touch();
          render();
          markDirty();
        };
        chip.append(x);
        return chip;
      }),
      ...pending.map((s) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip suggestion";
        chip.dataset.state = "suggested";
        chip.textContent = `+ ${s}`;
        chip.title = `Suggested from the description: add ${s}`;
        chip.onclick = () => { values.push(s); touch(); render(); markDirty(); };
        return chip;
      }),
    );
    container.dispatchEvent(new CustomEvent("chips-rendered", { detail: { field, count: values.length + pending.length } }));
  };
  const commit = () => {
    const parts = input.value.split(",").map((s) => s.trim()).filter(Boolean);
    for (const p of parts) if (!has(values, p)) values.push(p);
    input.value = "";
    if (parts.length) { touch(); render(); markDirty(); input.focus(); }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
    else if (e.key === "Backspace" && !input.value && values.length) {
      dismissed.add(values.pop().toLowerCase());
      touch();
      render();
      markDirty();
      input.focus();
    }
  });
  input.addEventListener("blur", commit);
  container.addEventListener("click", (e) => { if (e.target === container) input.focus(); });

  return {
    field,
    get: () => { commit(); return [...values]; },
    /** Busy = focus or uncommitted typing: async refreshes must leave the field alone. */
    busy: () => container.contains(document.activeElement) || input.value.trim() !== "" || touched,
    set: (v, suggestedValues = [], committed = false) => {
      values = [...(v || [])];
      autoFill = values.length === 0;
      const suggestedLower = new Set((suggestedValues || []).map((s) => s.toLowerCase()));
      provisional = committed ? new Set() : new Set(values.map((x) => x.toLowerCase()).filter((x) => suggestedLower.has(x)));
      extra = [];
      dismissed.clear();
      touched = false;
      render();
    },
    /** Derived values: auto-added (provisional) to a field that was empty and untouched, otherwise offered with "+". */
    derive: (found) => {
      const fresh = (found || []).filter((s) => !dismissed.has(s.toLowerCase()));
      extra = fresh;
      let added = false;
      if (autoFill && !touched) {
        values = values.filter((v) => has(fresh, v)); // follow the description while it's being typed
        provisional = new Set(values.map((x) => x.toLowerCase()));
        for (const s of fresh) {
          if (!has(values, s)) { values.push(s); provisional.add(s.toLowerCase()); added = true; }
        }
      }
      render();
      return added;
    },
  };
}
const chips = Object.fromEntries(
  [...document.querySelectorAll(".chips-input")].map((el) => [el.dataset.field, chipsInput(el, el.dataset.field)]),
);
// People sit off the main path (Immich groups faces after import). The section opens by itself whenever it has
// something to show, so a suggested or saved value is never hidden from the operator.
document.querySelector('.chips-input[data-field="people"]').addEventListener("chips-rendered", (e) => {
  if (e.detail.count > 0) $("more-fields").open = true;
});

// ---------- people / places / events suggested from the description ----------
let deriveTimer;
let deriveSeq = 0;

async function deriveEntities() {
  const scanId = state.selectedId;
  const text = $("description").value;
  const seq = ++deriveSeq;
  if (scanId == null) return;
  let found = { people: [], places: [], events: [] };
  if (text.trim()) {
    try {
      found = await api("POST", "/api/entities/extract", { text, scan_id: scanId });
    } catch {
      return; // a convenience; never block editing
    }
  }
  if (seq !== deriveSeq || scanId !== state.selectedId) return;
  let changed = false;
  for (const f of ENTITY_FIELDS) changed = chips[f].derive(found[f]) || changed;
  $("derive-hint").hidden = !ENTITY_FIELDS.some((f) => (found[f] || []).length);
  if (changed) markDirty();
}

function scheduleDerive(delay = 400) {
  clearTimeout(deriveTimer);
  deriveTimer = setTimeout(deriveEntities, delay);
}

// ---------- queue ----------
async function loadSummary() {
  const data = await api("GET", "/api/summary");
  state.counts = data.counts;
  state.noDate = data.no_date ?? 0;
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
  const tabs = $("tabs");
  for (const [key, label] of STATUSES) {
    let b = tabs.querySelector(`[data-status="${key}"]`);
    if (!b) {
      b = document.createElement("button");
      b.className = "tab";
      b.dataset.status = key;
      b.onclick = () => selectTab(key);
      tabs.append(b);
    }
    b.classList.toggle("active", key === state.status);
    b.setAttribute("aria-pressed", String(key === state.status));
    const count = key ? data.counts[key] ?? 0 : total;
    if (b.dataset.rendered !== `${label}|${count}`) {
      b.innerHTML = `${label}<span class="count">${count}</span>`;
      b.dataset.rendered = `${label}|${count}`;
    }
  }
}

function showListSkeleton(show) {
  const skeleton = $("scan-list-skeleton");
  if (!show) { skeleton.replaceChildren(); return; }
  skeleton.replaceChildren(
    ...Array.from({ length: Math.max(3, Math.min(state.scans.length || 6, 8)) }, () => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="skeleton thumb"></span><span><span class="skeleton skeleton-row" style="width:55%"></span><span class="skeleton skeleton-row" style="width:80%"></span></span>`;
      return li;
    }),
  );
}

/** Scans that need a decision: no date, possible rescan, no back, blank back. The queue can show only these. */
function isException(s) {
  return s.date.precision === "unknown" || !!s.duplicate_group_id || !!s.operator_duplicate
    || !!s.immich_duplicate_asset_id || !!s.operator_immich_duplicate || !s.has_back || s.back_type === "blank";
}

function queueSubline(s) {
  const marks = [`<span>${s.date.precision === "unknown" ? "no date" : escapeHtml(s.date.label)}</span>`];
  if (state.status === "") marks.push(`<span class="chip status ${s.status}">${statusLabel(s.status)}</span>`);
  if (s.duplicate_group_id || s.operator_duplicate) marks.push(`<span class="chip warn">dup?</span>`);
  if (s.immich_duplicate_asset_id || s.operator_immich_duplicate) marks.push(`<span class="chip warn">in Immich?</span>`);
  if (!s.has_back) marks.push(`<span class="chip">no back</span>`);
  else if (s.back_type === "content") marks.push(`<span class="chip">back text</span>`);
  else if (s.back_type === "blank") marks.push(`<span class="chip">blank back</span>`);
  return marks.join("");
}

/** Update a queue row in place; only the parts that changed are touched, so a poll never resets scroll or focus. */
function syncQueueRow(li, s) {
  const version = encodeURIComponent(s.updated_at);
  const src = `/api/scans/${s.id}/image/front?size=160&v=${version}`;
  const img = li.querySelector("img");
  if (img.getAttribute("src") !== src) img.src = src;
  const sub = queueSubline(s);
  if (li.dataset.sub !== sub) { li.querySelector(".sub").innerHTML = sub; li.dataset.sub = sub; }
  li.classList.toggle("selected", s.id === state.selectedId);
}

function queueRow(s, index) {
  const li = document.createElement("li");
  li.dataset.id = s.id;
  li.tabIndex = 0;
  li.className = "fresh";
  li.style.setProperty("--i", index);
  li.innerHTML = `<img loading="lazy" alt=""><div><div class="title">#${padId(s.id)}</div><div class="sub"></div></div>`;
  li.onclick = () => selectScan(s.id);
  li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectScan(s.id); } };
  li.addEventListener("animationend", () => li.classList.remove("fresh"), { once: true });
  syncQueueRow(li, s);
  return li;
}

function renderQueue() {
  state.visible = state.exceptionsOnly ? state.scans.filter(isException) : state.scans;
  const list = $("scan-list");
  const existing = new Map([...list.children].map((li) => [Number(li.dataset.id), li]));
  const wanted = new Set(state.visible.map((s) => s.id));
  for (const [id, li] of existing) if (!wanted.has(id)) li.remove();
  let cursor = list.firstElementChild;
  let fresh = 0;
  for (const s of state.visible) {
    let li = existing.get(s.id);
    if (li) syncQueueRow(li, s);
    else li = queueRow(s, fresh++);
    if (li !== cursor) list.insertBefore(li, cursor);
    else cursor = cursor.nextElementSibling;
  }
  const exceptions = state.scans.filter(isException).length;
  $("queue-count").textContent = state.exceptionsOnly
    ? `${state.visible.length} of ${plural(state.scans.length, "scan")}`
    : `${plural(state.scans.length, "scan")}${exceptions ? ` · ${exceptions} to decide` : ""}`;
  $("empty-list").hidden = state.visible.length > 0;
  $("empty-list").textContent = state.exceptionsOnly && state.scans.length ? "No exceptions left in this list." : "No scans here.";
  updateNavButtons();
}

let listLoads = 0;
async function loadList() {
  const params = new URLSearchParams();
  if (state.status) params.set("status", state.status);
  if (state.batchId != null) params.set("batch_id", state.batchId);
  const query = params.toString();
  const url = "/api/scans" + (query ? `?${query}` : "");
  const load = ++listLoads;
  const slow = setTimeout(() => { if (load === listLoads && !$("scan-list").children.length) showListSkeleton(true); }, 120);
  try {
    state.scans = await api("GET", url);
  } finally {
    clearTimeout(slow);
    if (load === listLoads) showListSkeleton(false);
  }
  if (load !== listLoads) return; // a newer load is already rendering
  renderQueue();
}

async function refresh() {
  if (typeof invalidateBatch === "function") invalidateBatch();
  await Promise.all([loadSummary(), loadList()]);
  if (typeof renderStage === "function") renderStage();
}

async function selectTab(status, { batchId = state.batchId } = {}) {
  if (!(await confirmLeave())) return;
  state.status = status;
  state.batchId = batchId;
  if (typeof renderBatchFilter === "function") renderBatchFilter();
  await refresh();
  if (state.visible.length) selectScan(state.visible[0].id, true);
  else showEditor(null);
}

function updateNavButtons() {
  const i = state.visible.findIndex((s) => s.id === state.selectedId);
  $("btn-prev").disabled = i <= 0;
  $("btn-next").disabled = i < 0 || i >= state.visible.length - 1;
}

// ---------- editor ----------
function markDirty(dirty = true) {
  state.dirty = dirty;
  $("save-state").textContent = dirty ? "unsaved changes" : "";
}

async function confirmLeave() {
  if (!state.dirty) return true;
  try { await save(); return true; } catch (e) { failToast(e); return false; }
}

/** Show a skeleton in the image well until the preview has decoded. The well keeps its size either way. */
function setFrameImage(side, src) {
  const frame = $(`frame-${side}`);
  const img = $(`img-${side}`);
  if (!src) {
    img.hidden = true;
    img.removeAttribute("src");
    frame.classList.remove("loading");
    return;
  }
  img.hidden = false;
  if (img.getAttribute("src") === src && img.complete) return;
  frame.classList.add("loading");
  img.classList.remove("developed");
  img.onload = () => { frame.classList.remove("loading"); img.classList.add("developed"); };
  img.onerror = () => frame.classList.remove("loading");
  img.src = src;
}

function fieldBusy(el, field) {
  return document.activeElement === el || state.edited.has(field);
}

function showEditor(scan, { full = false } = {}) {
  $("editor").hidden = !scan;
  if (!scan) {
    state.selectedId = null;
    state.current = null;
    document.querySelectorAll("#scan-list li.selected").forEach((li) => li.classList.remove("selected"));
    updateNavButtons();
    if (typeof renderBatchCard === "function") renderBatchCard(null);
    if (typeof renderStage === "function") renderStage();
    return;
  }

  // Refresh = same scan re-rendered after an async action (keeps fields being edited). `full` forces a clean render
  // without ever clearing state.selectedId, so a shortcut pressed mid-transition still has a scan to act on.
  const refreshing = !full && scan.id === state.selectedId;
  if (!refreshing) state.edited = new Set();
  state.selectedId = scan.id;
  state.current = scan;
  const committed = isCommitted(scan);
  const sug = scan.suggestions || {};
  document.querySelectorAll("#scan-list li").forEach((li) => li.classList.toggle("selected", +li.dataset.id === scan.id));
  updateNavButtons();
  if (typeof renderStage === "function") renderStage();
  if (typeof renderBatchCard === "function") renderBatchCard(scan);

  $("scan-title").textContent = `Scan #${padId(scan.id)}`;
  $("scan-status").className = `chip status ${scan.status}`;
  $("scan-status").textContent = statusLabel(scan.status);
  $("scan-batch").textContent = [scan.batch, scan.source_key].filter(Boolean).join(" · ");
  $("scan-dup").hidden = !scan.duplicate_group_id;
  $("scan-dup").textContent = scan.duplicate_group_id ? `possible rescan of #${scan.duplicate_group_id}` : "";
  setSuggested($("scan-dup"), !!scan.duplicate_group_id && !committed);
  $("scan-operator-dup").hidden = !scan.operator_duplicate;
  $("btn-flag-dup").setAttribute("aria-pressed", String(!!scan.operator_duplicate));
  $("btn-flag-dup").firstChild.textContent = scan.operator_duplicate ? "Unflag duplicate" : "Flag duplicate";
  $("scan-immich-dup").hidden = !scan.immich_duplicate_asset_id;
  const immichMatch = sug.immich_duplicate?.value;
  const immichDistance = Number.isInteger(immichMatch?.distance)
    ? `differs by ${immichMatch.distance} of 64` : "";
  $("scan-immich-dup").textContent = immichDistance ? `in Immich? (${immichDistance})` : "in Immich?";
  setSuggested($("scan-immich-dup"), !!scan.immich_duplicate_asset_id && !committed);
  showImmichCompare(scan, immichDistance, encodeURIComponent(scan.updated_at));
  $("scan-operator-immich-dup").hidden = !scan.operator_immich_duplicate;
  $("btn-flag-immich-dup").setAttribute("aria-pressed", String(!!scan.operator_immich_duplicate));
  $("btn-flag-immich-dup").firstChild.textContent = scan.operator_immich_duplicate ? "Unflag as in Immich" : "Flag as in Immich";

  const version = encodeURIComponent(scan.updated_at);
  setFrameImage("front", `/api/scans/${scan.id}/image/front?v=${version}`);
  $("front-crop").textContent = scan.front_crop ? "cropped" : "full page";
  setSuggested($("front-crop"), !committed && sug.crop_front !== undefined && sameValue(sug.crop_front.value, scan.front_crop));
  // Only shown when a rotation was actually applied (0 means "already upright", nothing to flag).
  $("front-rotation").hidden = !(sug.rotation_front && sug.rotation_front.value);
  setSuggested($("front-rotation"), !committed && sug.rotation_front && sug.rotation_front.value === scan.front_rotation);
  $("btn-swap").disabled = !scan.has_back;
  document.querySelectorAll('[data-rotate="back"]').forEach((b) => (b.disabled = !scan.has_back));
  const noBack = $("frame-back").querySelector(".no-back");
  if (noBack) noBack.remove();
  if (scan.has_back) {
    setFrameImage("back", `/api/scans/${scan.id}/image/back?v=${version}`);
  } else {
    setFrameImage("back", null);
    const div = document.createElement("div");
    div.className = "no-back";
    div.textContent = "No back scanned";
    $("frame-back").append(div);
  }
  $("back-type").textContent = scan.back_type ? (scan.back_type === "blank" ? "looks blank" : "has writing") : "";
  $("back-type").hidden = !scan.back_type;
  setSuggested($("back-type"), !committed && sug.blank_back !== undefined && sug.blank_back.value === scan.back_type);
  $("back-rotation").hidden = !(sug.rotation_back && sug.rotation_back.value);
  setSuggested($("back-rotation"), !committed && sug.rotation_back && sug.rotation_back.value === scan.back_rotation);
  $("keep-back").checked = scan.keep_back;
  $("keep-back").disabled = !scan.has_back;
  $("fig-back").classList.toggle("dropped", scan.has_back && !scan.keep_back);

  // Fields: never overwrite one that has focus or uncommitted input.
  const d = scan.date;
  if (!refreshing || !fieldBusy($("date-text"), "date")) {
    $("date-text").value = d.precision === "unknown" ? "" : d.label;
    renderDatePreview(d.precision === "unknown" ? null : [d], null, $("date-preview"), scan.date_source);
    setSuggested($("date-text"), !committed && !!sug.date && sug.date.value === (d.precision === "unknown" ? null : d.label));
  }
  if (!refreshing || !fieldBusy($("description"), "description")) {
    $("description").value = scan.description || "";
    setSuggested($("description"), !committed && !!sug.description && sug.description.value === (scan.description || ""));
  }
  if (!refreshing) $("more-fields").open = (scan.people || []).length > 0;
  for (const f of CHIP_FIELDS) {
    if (refreshing && chips[f].busy()) continue;
    chips[f].set(scan[f], sug[f]?.value || [], committed);
  }
  if (!(refreshing && $("ocr-lines").contains(document.activeElement))) renderOcr(scan);
  if (!refreshing) {
    $("derive-hint").hidden = true;
    document.querySelectorAll(".autocorrect-note").forEach((n) => (n.hidden = true));
  }

  const info = $("export-info");
  info.hidden = !scan.export;
  if (scan.export) info.textContent = `Exported to ${scan.export.front_rel}${scan.export.back_rel ? `  and  ${scan.export.back_rel}` : ""}`;
  // Delete is only ever offered for a scan already rejected - never reachable from the normal review flow.
  $("btn-delete").hidden = scan.status !== "rejected";
  if (!refreshing) markDirty(false);
  scheduleDerive(0);
}

async function selectScan(id, force = false, full = false) {
  if (!force && id === state.selectedId) return;
  if (!(await confirmLeave())) return;
  try {
    const scan = await api("GET", `/api/scans/${id}`);
    showEditor(scan, { full: full || id !== state.selectedId }); // a different scan always renders fully
  } catch (e) {
    failToast(e);
  }
}

const DATE_SOURCES = { back_ocr: "read from the back", front_imprint: "read from the front", batch: "set on the whole batch", manual: "typed by you" };

function renderDatePreview(candidates, error, el = $("date-preview"), source = null) {
  if (error) { el.innerHTML = `<span class="bad">${escapeHtml(error)}</span>`; return; }
  if (!candidates) {
    el.textContent = el.dataset.emptyText || "No date yet. The photo stays in the “no date” count; Immich would show the export time.";
    return;
  }
  if (!candidates.length) { el.innerHTML = `<span class="bad">Not understood. Try “Dec 1984”, “'84”, “Summer 1979”…</span>`; return; }
  const [best, ...others] = candidates;
  el.innerHTML = `
    <span>Reads as</span> <span class="ok">${escapeHtml(best.label)}</span>
    <span class="chip">${best.precision}${best.approximate ? ", approximate" : ""}</span>
    ${source && DATE_SOURCES[source] ? `<span>${DATE_SOURCES[source]}</span>` : ""}
    <span>timeline: <span class="mono">${escapeHtml(best.exif ?? "none")}</span></span>
    ${others.length ? `<span>also found: ${others.map((o) => escapeHtml(o.label)).join(", ")}</span>` : ""}`;
}

/** Live "read as …" preview under a free-text date field (150 ms debounce). Shared by the scan and batch forms. */
function wireDatePreview(input, preview, onInput) {
  let timer;
  input.addEventListener("input", () => {
    onInput?.();
    clearTimeout(timer);
    const text = input.value.trim();
    if (!text) { renderDatePreview(null, null, preview); return; }
    timer = setTimeout(async () => {
      try { renderDatePreview(await api("GET", `/api/dates/parse?text=${encodeURIComponent(text)}`), null, preview); }
      catch (e) { renderDatePreview(null, e.message, preview); }
    }, 150);
  });
}

// ---------- autocorrect (recurring misspellings: "Januaru" -> "January") ----------
// Runs on blur, not on every keystroke, so it never fights a word still being typed.
function wireAutocorrect(el, field, after) {
  const note = document.querySelector(`.autocorrect-note[data-for="${field}"]`);
  el.addEventListener("blur", async () => {
    const before = el.value;
    if (!before.trim()) { note.hidden = true; return; }
    let data;
    try { data = await api("POST", "/api/autocorrect", { text: before, scan_id: state.selectedId }); }
    catch { return; }
    if (el.value !== before || !data.fixes.length) return; // field changed again, or nothing to fix
    el.value = data.text;
    note.replaceChildren();
    for (const fix of data.fixes) {
      const span = document.createElement("span");
      span.className = "fix";
      span.innerHTML = `<span class="was">${escapeHtml(fix.from)}</span> &rarr; ${escapeHtml(fix.to)}`;
      note.append(span);
    }
    const undo = document.createElement("button");
    undo.type = "button";
    undo.textContent = data.fixes.length > 1 ? "Undo all" : "Undo";
    undo.onclick = () => { el.value = before; note.hidden = true; el.dispatchEvent(new Event("input")); el.focus(); };
    note.append(undo);
    note.hidden = false;
    state.edited.add(field);
    markDirty();
    after?.();
  });
  el.addEventListener("input", () => { if (!note.hidden) note.hidden = true; });
}
wireAutocorrect($("date-text"), "date", () => {
  const text = $("date-text").value.trim();
  if (text) api("GET", `/api/dates/parse?text=${encodeURIComponent(text)}`).then((c) => renderDatePreview(c)).catch(() => {});
});
wireAutocorrect($("description"), "description", () => scheduleDerive(0));

wireDatePreview($("date-text"), $("date-preview"), () => {
  state.edited.add("date");
  setSuggested($("date-text"), false);
  markDirty();
});
$("description").addEventListener("input", () => {
  state.edited.add("description");
  setSuggested($("description"), false);
  markDirty();
  scheduleDerive();
});
$("keep-back").addEventListener("change", () => {
  $("fig-back").classList.toggle("dropped", !$("keep-back").checked);
  markDirty();
});

function formBody(extra = {}) {
  const body = {
    date_text: $("date-text").value,
    description: $("description").value,
    keep_back: $("keep-back").checked,
    ...extra,
  };
  for (const f of CHIP_FIELDS) body[f] = chips[f].get();
  return body;
}

async function save(extra = {}) {
  if (state.selectedId == null) return null;
  const scan = await api("PATCH", `/api/scans/${state.selectedId}`, formBody(extra));
  markDirty(false);
  return scan;
}

// Status changes run one at a time: an Approve/Reject pressed while the previous one is still moving to the next scan
// waits, then applies to the scan now on screen (never to the one that was just approved).
let statusQueue = Promise.resolve();
function setStatus(status) {
  statusQueue = statusQueue.then(() => applyStatus(status));
  return statusQueue;
}

async function applyStatus(status) {
  if (state.selectedId == null) return;
  const currentIndex = state.visible.findIndex((s) => s.id === state.selectedId);
  try {
    const scan = await save({ status });
    const recorded = scan.corrections_recorded;
    toast(`#${scan.id} ${statusLabel(status).toLowerCase()}${recorded ? ` · ${recorded} corrections recorded for learning` : ""}`);
    if (recorded) loadLearningCount();
    await refresh();
    const next = state.visible.find((s) => s.id === scan.id)
      ? scan.id
      : state.visible[Math.min(currentIndex, state.visible.length - 1)]?.id;
    if (next != null) await selectScan(next, true, true); // full render, selection never cleared
    else showEditor(null);
  } catch (e) {
    failToast(e);
  }
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const scan = await save();
    toast("Saved");
    await refresh();
    showEditor(scan);
  } catch (err) {
    failToast(err);
  }
});

// ---------- text on back ----------
function renderOcr(scan) {
  const lines = scan.ocr_lines || [];
  $("btn-read-text").disabled = !scan.has_back;
  $("ocr-engine").textContent = scan.ocr_engine ? `read by ${scan.ocr_engine}` : "";
  $("ocr-empty").hidden = lines.length > 0;
  $("ocr-empty").textContent = scan.has_back
    ? scan.ocr_engine ? "No text found on the back." : "No text read yet."
    : "No back scanned.";
  $("ocr-hint").hidden = lines.length === 0;
  const description = $("description").value;
  const list = $("ocr-lines");
  list.classList.remove("ocr-lines-loading");
  list.setAttribute("aria-busy", "false");
  list.replaceChildren(...lines.map((line, index) => ocrRow(scan, line, index, description)));
}

function ocrRow(scan, line, index, description) {
  const shown = line.corrected ?? line.text;
  const li = document.createElement("li");
  li.className = `ocr-row${line.removed ? " removed" : ""}`;
  li.dataset.index = index;

  const button = document.createElement("button");
  button.type = "button";
  button.className = `ocr-line${line.score < 0.8 ? " low" : ""}${!line.removed && description.includes(shown) ? " used" : ""}`;
  button.innerHTML = `<span>${escapeHtml(shown)}</span><span class="score">${Math.round(line.score * 100)}%</span>`;
  button.title = line.removed ? "Marked as not text" : "Add to description";
  button.disabled = !!line.removed;
  setSuggested(button, line.corrected == null && !line.removed && !isCommitted(scan));
  button.onclick = () => {
    const current = $("description").value.trimEnd();
    if (!current.includes(shown)) {
      $("description").value = current ? `${current}\n${shown}` : shown;
      state.edited.add("description");
      setSuggested($("description"), false);
      markDirty();
      scheduleDerive(0);
    }
    button.classList.add("used");
  };
  li.append(button);

  if (line.corrected != null) {
    li.insertAdjacentHTML("beforeend", `<span class="badge fixed">fixed</span><span class="was">${escapeHtml(line.text)}</span>`);
  } else if (line.dictionary) {
    li.insertAdjacentHTML("beforeend", `<span class="badge auto" title="Corrected by the learned dictionary (${escapeHtml(line.dictionary)})">auto-fixed</span><span class="was">${escapeHtml(line.raw)}</span>`);
  }

  const tools = document.createElement("span");
  tools.className = "line-tools";
  const fix = document.createElement("button");
  fix.type = "button";
  fix.className = "btn small line-fix";
  fix.textContent = "Fix";
  fix.title = "Correct what was read";
  fix.disabled = !!line.removed;
  fix.onclick = () => startLineEdit(li, scan, index, shown);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn small ghost line-remove";
  remove.textContent = line.removed ? "Restore" : "Not text";
  remove.title = line.removed ? "Use this line again" : "This isn't real text (lab code, smudge)";
  remove.onclick = () => updateOcrLine(index, { removed: !line.removed }, line.removed ? "Line restored" : "Marked as not text");
  tools.append(fix, remove);
  li.append(tools);
  return li;
}

function startLineEdit(li, scan, index, shown) {
  li.replaceChildren();
  const input = document.createElement("input");
  input.type = "text";
  input.className = "line-edit";
  input.value = shown;
  input.setAttribute("aria-label", `Correct line ${index + 1}`);
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn small primary line-save";
  saveBtn.textContent = "Save fix";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn small ghost line-cancel";
  cancel.textContent = "Cancel";
  const commit = () => updateOcrLine(index, { text: input.value }, "Line fixed. It becomes a learning example when you approve.");
  saveBtn.onclick = commit;
  cancel.onclick = () => renderOcr(scan);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); renderOcr(scan); }
  });
  li.append(input, saveBtn, cancel);
  input.focus();
  input.select();
}

async function updateOcrLine(index, body, message) {
  document.activeElement?.blur(); // the edited line is committed; allow the panel to re-render
  await applyScanAction(async () => {
    await save();
    return api("PUT", `/api/scans/${state.selectedId}/ocr-lines/${index}`, body);
  }, message);
}

function showOcrSkeleton() {
  const list = $("ocr-lines");
  const rows = Math.max(list.children.length, 3); // reserve the space the current lines take
  $("ocr-empty").hidden = true;
  list.classList.add("ocr-lines-loading");
  list.setAttribute("aria-busy", "true");
  list.replaceChildren(...Array.from({ length: rows }, () => {
    const li = document.createElement("li");
    li.className = "skeleton skeleton-line";
    return li;
  }));
}

// ---------- scan actions ----------
async function applyScanAction(run, message) {
  if (state.selectedId == null) return;
  try {
    const scan = await run();
    await refresh();
    showEditor(scan);
    if (message) toast(message);
  } catch (e) {
    failToast(e);
  }
}

async function withButton(button, fn) {
  button.disabled = true;
  button.classList.add("busy");
  button.setAttribute("aria-busy", "true");
  try { await fn(); } catch (e) { failToast(e); } finally {
    button.disabled = false;
    button.classList.remove("busy");
    button.setAttribute("aria-busy", "false");
  }
}

async function rotate(side, step) {
  if (state.selectedId == null) return;
  const scan = await api("GET", `/api/scans/${state.selectedId}`);
  const field = `${side}_rotation`;
  const next = (((scan[field] || 0) + step) % 360 + 360) % 360;
  await applyScanAction(() => save({ [field]: next }));
}

function move(delta) {
  if (!state.visible.length) return;
  const i = state.visible.findIndex((s) => s.id === state.selectedId);
  const next = state.visible[Math.max(0, Math.min(state.visible.length - 1, i + delta))];
  if (next && next.id !== state.selectedId) selectScan(next.id);
}

document.querySelectorAll("[data-rotate]").forEach((button) => {
  button.onclick = () => rotate(button.dataset.rotate, Number(button.dataset.step));
});
$("btn-prev").onclick = () => move(-1);
$("btn-next").onclick = () => move(1);
$("btn-flag-dup").onclick = () => applyScanAction(async () => {
  const flagged = !state.current?.operator_duplicate;
  const scan = await save({ operator_duplicate: flagged });
  toast(flagged ? "Flagged as duplicate" : "Duplicate flag cleared");
  return scan;
});
$("btn-flag-immich-dup").onclick = () => applyScanAction(async () => {
  const flagged = !state.current?.operator_immich_duplicate;
  const scan = await save({ operator_immich_duplicate: flagged });
  toast(flagged ? "Flagged as already in Immich" : "Immich flag cleared");
  return scan;
});
$("btn-swap").onclick = () => applyScanAction(async () => {
  await save();
  return api("POST", `/api/scans/${state.selectedId}/swap-sides`);
}, "Front and back swapped");
$("btn-redetect").onclick = () => withButton($("btn-redetect"), () => applyScanAction(async () => {
  await save();
  return api("POST", `/api/scans/${state.selectedId}/reanalyze`);
}, "Crop detected again"));
$("btn-read-text").onclick = () => withButton($("btn-read-text"), () => applyScanAction(async () => {
  await save();
  showOcrSkeleton();
  return api("POST", `/api/scans/${state.selectedId}/read-text`);
}, "Text read from the back"));
$("btn-approve").onclick = () => setStatus("approved");
$("btn-reject").onclick = () => setStatus("rejected");

async function deleteScan() {
  if (state.selectedId == null) return;
  const id = state.selectedId;
  const currentIndex = state.visible.findIndex((s) => s.id === id);
  const ok = await confirmAction({
    title: `Delete scan #${padId(id)} permanently?`,
    body: "Removes the original front and back files from the archive, and this record. The scan won't come back.",
    confirmLabel: "Delete permanently",
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/scans/${id}`);
    toast(`#${padId(id)} deleted permanently`);
    await refresh();
    const next = state.visible[Math.min(currentIndex, state.visible.length - 1)]?.id;
    if (next != null) await selectScan(next, true, true); // full render, selection never cleared
    else showEditor(null);
  } catch (e) {
    failToast(e);
  }
}
$("btn-delete").onclick = () => withButton($("btn-delete"), deleteScan);

// ---------- queue filter ----------
try {
  state.exceptionsOnly = localStorage.getItem("exceptionsOnly") === "1";
} catch { /* storage unavailable */ }
$("exceptions-only").checked = state.exceptionsOnly;
$("exceptions-only").addEventListener("change", async () => {
  state.exceptionsOnly = $("exceptions-only").checked;
  try { localStorage.setItem("exceptionsOnly", state.exceptionsOnly ? "1" : "0"); } catch { /* ignore */ }
  renderQueue();
  if (typeof renderStage === "function") renderStage();
  if (state.visible.length && !state.visible.some((s) => s.id === state.selectedId)) await selectScan(state.visible[0].id, true);
  else if (!state.visible.length) showEditor(null);
});

// ---------- pipeline ----------
$("btn-ingest").onclick = () => withButton($("btn-ingest"), async () => {
  const r = await api("POST", "/api/ingest");
  const lines = [r.created.length ? `Ingested ${r.created.length} scan(s) into ${r.batch}` : "Inbox has no new scans"];
  if (r.possible_duplicates.length) lines.push(`${r.possible_duplicates.length} possible rescan(s) flagged`);
  if (r.unmatched.length) lines.push(`Ignored: ${r.unmatched.join(", ")}`);
  toast(lines.join("\n"));
  if (typeof clearStageEvent === "function") clearStageEvent();
  await selectTab("needs_review", { batchId: null });
});

async function exportApproved() {
  if (!(await confirmLeave())) return;
  await loadSummary();
  const approved = state.counts.approved ?? 0;
  if (!approved) { toast("Nothing approved to export"); return; }
  const ok = await confirmAction({
    title: `Export ${approved} approved scan${approved === 1 ? "" : "s"}?`,
    body: "Photos and their metadata are written to the library folder that Immich reads.",
    confirmLabel: `Export ${approved} scan${approved === 1 ? "" : "s"}`,
  });
  if (!ok) return;
  const r = await api("POST", "/api/export");
  if (r.errors.length) {
    toast(`Exported ${r.exported.length}, failed ${r.errors.length}:\n` + r.errors.map((x) => `#${x.id}: ${x.error}`).join("\n") + "\n[EXPORT_PARTIAL]", true);
  } else {
    toast(r.exported.length ? `Exported ${r.exported.length} scan(s) to the Immich library` : "Nothing approved to export");
  }
  if (typeof stageEvent === "function") stageEvent(r.errors.length ? "attention" : "done", { kind: "export", result: r });
  await refresh();
  if (state.selectedId != null) await selectScan(state.selectedId, true, true);
}
$("btn-export").onclick = () => withButton($("btn-export"), exportApproved);

window.addEventListener("beforeunload", (e) => { if (state.dirty) e.preventDefault(); });

// ---------- scanner ----------
let scanPoll = null;
let destinationChosen = false;
let lastScanner = null; // last /api/scanner payload: the stage and the strip read it
try {
  const saved = localStorage.getItem("scanDestination");
  if (saved === "review" || saved === "inbox") { $("scan-destination").value = saved; destinationChosen = true; }
} catch { /* storage unavailable */ }
$("scan-destination").addEventListener("change", () => {
  destinationChosen = true;
  try { localStorage.setItem("scanDestination", $("scan-destination").value); } catch { /* ignore */ }
});
try {
  const savedCount = localStorage.getItem("scanCount");
  if (savedCount === "all" || savedCount === "one") $("scan-count").value = savedCount;
} catch { /* storage unavailable */ }
$("scan-count").addEventListener("change", () => {
  try { localStorage.setItem("scanCount", $("scan-count").value); } catch { /* ignore */ }
  $("btn-scan").textContent = $("scan-count").value === "one" ? "Scan one photo" : "Scan feeder";
});

/** Front and back are one photo, not two: count pages the way the finished-scan message does. */
function scanPhotos(s) {
  const rawPages = s.scan?.pages ?? 0;
  return s.duplex ? Math.ceil(rawPages / 2) : rawPages;
}

function renderScanner(s) {
  lastScanner = s;
  const el = $("scanner-status");
  const button = $("btn-scan");
  const destination = $("scan-destination-wrap");
  const count = $("scan-count-wrap");
  if (!s.configured) {
    el.hidden = true; button.hidden = true; destination.hidden = true; count.hidden = true;
  } else {
    const scanning = s.scan?.state === "scanning";
    el.hidden = false;
    button.hidden = false;
    destination.hidden = false;
    count.hidden = false;
    $("scan-destination").disabled = scanning;
    $("scan-count").disabled = scanning;
    if (!destinationChosen && s.after_scan) $("scan-destination").value = s.after_scan;
    el.className = `strip-item scanner-status ${scanning ? "busy" : s.online ? "online" : "offline"}`;
    const label = plural(scanPhotos(s), "photo");
    el.textContent = !scanning
      ? `Scanner ${s.online ? "online" : "offline"}`
      : s.scan.phase === "ingesting" ? `Processing ${label}` : `Scanning, ${label}`;
    el.title = `${s.device} · ${s.source} · ${s.mode} · ${s.resolution} dpi`;
    button.disabled = scanning || !s.online;
    button.textContent = scanning ? "Scanning" : $("scan-count").value === "one" ? "Scan one photo" : "Scan feeder";
  }
  if (typeof renderStrip === "function") renderStrip();
  if (typeof renderStage === "function") renderStage();
}

async function loadScannerStatus() {
  try {
    const s = await api("GET", "/api/scanner");
    renderScanner(s);
    if (s.scan?.state === "scanning") watchScan();
  } catch {
    $("scanner-status").hidden = true;
    $("btn-scan").hidden = true;
  }
}

function watchScan() {
  if (scanPoll) return;
  scanPoll = setInterval(async () => {
    let run;
    try { run = await api("GET", "/api/scanner/scan"); } catch { return; }
    const s = await api("GET", "/api/scanner").catch(() => null);
    if (s) renderScanner(s);
    if (run.state === "scanning") return;
    clearInterval(scanPoll);
    scanPoll = null;
    loadHealth();
    if (run.state === "failed") {
      const code = typeof scanFailureCode === "function" ? scanFailureCode(run.message) : "SCAN_FAILED";
      toast(`Scan failed: ${run.message}\nPages already scanned stay in the inbox; nothing was deleted. [${code}]`, true);
      if (typeof stageEvent === "function") stageEvent("attention", { kind: "scan", run });
    } else if (run.state === "done" && run.destination === "inbox") {
      toast(`${run.message}\nClick Ingest inbox to review them.`);
      if (typeof stageEvent === "function") stageEvent("done", { kind: "scan-inbox", run });
    } else if (run.state === "done") {
      const created = run.ingest?.created?.length ?? 0;
      const dups = run.ingest?.possible_duplicates?.length ?? 0;
      toast(`${run.message}\nIngested ${created} scan(s)${dups ? `, ${dups} possible rescan(s)` : ""}`);
      if (typeof clearStageEvent === "function") clearStageEvent();
      await selectTab("needs_review", { batchId: null });
    }
  }, 1500);
}

async function startScan() {
  const destination = $("scan-destination").value;
  if (typeof clearStageEvent === "function") clearStageEvent();
  await api("POST", "/api/scanner/scan", { destination, count: $("scan-count").value });
  toast(destination === "inbox"
    ? "Scanning started. Files will be left in the inbox."
    : "Scanning started. Photos appear in the review queue when the feeder is empty.");
  await loadScannerStatus();
  watchScan();
}
$("btn-scan").onclick = () => withButton($("btn-scan"), startScan);

// ---------- system health ----------
let lastHealthRows = 13;
let lastHealth = null; // last /api/health payload (null while unreachable): the strip reads it

async function loadHealth() {
  const button = $("btn-health");
  const dot = button.querySelector(".dot");
  let data;
  try {
    data = await api("GET", "/api/health");
  } catch {
    dot.className = "dot fail";
    $("health-text").textContent = "API unreachable";
    lastHealth = null;
    if (typeof renderStrip === "function") renderStrip();
    if (typeof stageEvent === "function") stageEvent("attention", { kind: "api" });
    return;
  }
  lastHealth = data;
  const problems = data.checks.filter((c) => c.status === "fail").length;
  const warnings = data.checks.filter((c) => c.status === "warn").length;
  dot.className = `dot ${data.overall}`;
  $("health-text").textContent = data.overall === "ok"
    ? "System OK"
    : problems ? `${problems} problem${problems === 1 ? "" : "s"}` : `${warnings} warning${warnings === 1 ? "" : "s"}`;
  button.title = data.checks.filter((c) => c.status !== "ok").map((c) => `${c.label}: ${c.detail}`).join("\n") || "All components healthy";
  lastHealthRows = data.checks.length;
  const list = $("health-list");
  for (const c of data.checks) {
    let li = list.querySelector(`li[data-name="${c.name}"]`);
    if (!li) { li = document.createElement("li"); li.dataset.name = c.name; list.append(li); }
    li.dataset.status = c.status;
    const html = `<span class="dot ${c.status}"></span><strong>${escapeHtml(c.label)}</strong><span class="health-detail">${escapeHtml(c.detail)}</span>`;
    if (li.innerHTML !== html) li.innerHTML = html;
  }
  [...list.children].forEach((li) => { if (!li.dataset.name || !data.checks.some((c) => c.name === li.dataset.name)) li.remove(); });
  list.setAttribute("aria-busy", "false");
  $("health-checked").textContent = `checked ${new Date().toLocaleTimeString()}`;
  if (typeof renderStrip === "function") renderStrip();
  if (typeof clearStageEvent === "function") clearStageEvent("api");
}

function panelSkeleton(list, rows) {
  if (list.querySelector("[data-name], [data-key]")) return; // keep real content while refreshing
  list.replaceChildren(...Array.from({ length: rows }, () => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="skeleton skeleton-row" style="width:100%"></span>`;
    return li;
  }));
}

function toggleHealth(open = $("health-panel").hidden) {
  $("health-panel").hidden = !open;
  $("btn-health").setAttribute("aria-expanded", String(open));
  if (open) {
    toggleLearning(false);
    toggleImmich(false);
    panelSkeleton($("health-list"), lastHealthRows);
    loadHealth();
  }
}
$("btn-health").onclick = () => toggleHealth();
$("btn-health-close").onclick = () => toggleHealth(false);
$("btn-health-recheck").onclick = () => withButton($("btn-health-recheck"), loadHealth);

// ---------- learning loop ----------
const FIELD_LABELS = {
  ocr_line: "Text lines", person: "People", place: "Places", event: "Events", date: "Dates",
  rotation: "Rotation", crop: "Crop", pairing: "Front/back", duplicate: "Duplicates", blank_back: "Blank backs",
};
const PRODUCER_LABELS = {
  ocr_line: "Text reader", description: "Description filter", date: "Date rules", entities: "Names (NER)",
  entities_rules_only: "Names (rules only)", crop: "Crop detection", rotation: "Rotation", pairing: "Front/back pairing",
  duplicate: "Duplicate check", blank_back: "Blank-back check",
};

async function loadLearningCount() {
  try {
    const data = await api("GET", "/api/learning");
    $("learning-count").textContent = data.events_usable;
    return data;
  } catch {
    return null;
  }
}

async function loadLearning() {
  $("learning-body").setAttribute("aria-busy", "true");
  const data = await loadLearningCount();
  $("learning-body").setAttribute("aria-busy", "false");
  if (!data) { toast("Couldn't load the learning status", true); return; }

  $("learning-usable").textContent = data.events_usable;
  $("learning-usable-note").textContent =
    `usable events from ${data.scans_with_corrections} approved scan${data.scans_with_corrections === 1 ? "" : "s"} · ${data.ocr_line_images} line images · ${data.events_total} recorded in total`;

  const fields = Object.entries(data.by_field);
  $("learning-fields").replaceChildren(...(fields.length ? fields.map(([field, counts]) => {
    const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
    const li = document.createElement("li");
    li.dataset.field = field;
    li.innerHTML = `<span>${escapeHtml(FIELD_LABELS[field] || field)}</span>
      <span class="bar" title="kept ${counts.kept}, fixed ${counts.edited}, removed ${counts.removed}, added ${counts.added}">
        ${["kept", "edited", "removed", "added"].map((k) => `<span class="${k}" style="width:${(counts[k] / total) * 100}%"></span>`).join("")}
      </span><span class="mono muted">${total}</span>`;
    return li;
  }) : [Object.assign(document.createElement("li"), { className: "muted", textContent: "Nothing yet. Approve a scan to record its first corrections." })]));

  const dict = data.dictionary;
  $("dictionary-version").textContent = dict.version;
  const entries = [
    ...dict.lines.map((e) => ({ ...e, kind: "line" })),
    ...dict.words.map((e) => ({ ...e, kind: "word" })),
    ...Object.entries(dict.suppressed).flatMap(([field, values]) => values.map((v) => ({ from: v, to: `not a ${field}`, kind: "suppressed" }))),
  ];
  $("dictionary-empty").hidden = entries.length > 0;
  $("dictionary-list").replaceChildren(...entries.map((e) => {
    const li = document.createElement("li");
    li.dataset.kind = e.kind;
    li.innerHTML = `<span class="chip">${e.kind}</span><span class="from">${escapeHtml(e.from)}</span><span class="muted">becomes</span><span class="to">${escapeHtml(e.to)}</span>`;
    return li;
  }));

  const overrides = Object.fromEntries(data.overrides.map((o) => [o.key, o]));
  $("proposal-list").replaceChildren(...data.proposals.map((p) => {
    const li = document.createElement("li");
    li.dataset.key = p.key;
    const applied = overrides[p.key];
    const proposed = p.proposed == null ? "no change proposed" : `<strong>${p.proposed}</strong>`;
    li.innerHTML = `<div><strong>${escapeHtml(p.label)}</strong></div>
      <div class="values">current ${p.current} · proposed ${proposed}${applied ? ` · applied (was ${applied.previous})` : ""}</div>
      <p class="reason">${escapeHtml(p.reason)}</p>`;
    const actions = document.createElement("div");
    actions.className = "proposal-actions";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn small irreversible proposal-apply";
    apply.textContent = "Apply";
    apply.disabled = p.proposed == null;
    apply.onclick = () => withButton(apply, async () => {
      const ok = await confirmAction({
        title: `Apply ${p.label.toLowerCase()}?`,
        body: `Changes ${p.key} from ${p.current} to ${p.proposed} for every scan analyzed from now on.`,
        confirmLabel: "Apply threshold",
        note: "Scans already analyzed keep their result. You can revert to the previous value from this panel.",
      });
      if (!ok) return;
      await api("POST", `/api/learning/proposals/${encodeURIComponent(p.key)}/apply`);
      toast(`${p.label} set to ${p.proposed}`);
      await loadLearning();
    });
    actions.append(apply);
    if (applied) {
      const revert = document.createElement("button");
      revert.type = "button";
      revert.className = "btn small proposal-revert";
      revert.textContent = `Revert to ${applied.previous}`;
      revert.onclick = () => withButton(revert, async () => {
        await api("POST", `/api/learning/proposals/${encodeURIComponent(p.key)}/revert`);
        toast(`${p.label} reverted`);
        await loadLearning();
      });
      actions.append(revert);
    }
    li.append(actions);
    return li;
  }));

  $("producer-list").replaceChildren(...Object.entries(data.producers).map(([key, value]) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(PRODUCER_LABELS[key] || key)}</span><span>${escapeHtml(value)}</span>`;
    return li;
  }));
  $("stage-list").replaceChildren(...data.stages.map((s) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="mono muted">${s.stage}</span><span>${escapeHtml(s.name)}</span><span class="chip status state ${s.status === "implemented" ? "approved" : "needs_review"}">${s.status}</span>`;
    return li;
  }));
}

function toggleLearning(open = $("learning-panel").hidden) {
  $("learning-panel").hidden = !open;
  $("btn-learning").setAttribute("aria-expanded", String(open));
  if (open) {
    toggleHealth(false);
    toggleImmich(false);
    panelSkeleton($("proposal-list"), 2);
    loadLearning();
  }
}
$("btn-learning").onclick = () => toggleLearning();
$("btn-learning-close").onclick = () => toggleLearning(false);
$("btn-learning-refresh").onclick = () => withButton($("btn-learning-refresh"), loadLearning);

// ---------- Immich (read-only duplicate check) ----------
function toggleImmich(open = $("immich-panel").hidden) {
  $("immich-panel").hidden = !open;
  $("btn-immich").setAttribute("aria-expanded", String(open));
  if (open) {
    toggleHealth(false);
    toggleLearning(false);
    loadImmichSettings();
    resumeImmichCheckDisplay().catch(() => {});
  }
}
$("btn-immich").onclick = () => toggleImmich();
$("btn-immich-close").onclick = () => toggleImmich(false);

let immichBaseUrl = null;

// The matched Immich photo, shown beside this scan's front without leaving the app (the server fetches it).
function showImmichCompare(scan, distanceText, version) {
  const assetId = scan.immich_duplicate_asset_id;
  $("immich-compare").hidden = !assetId;
  if (!assetId) { setFrameImage("immich", null); setFrameImage("match-scan", null); showImmichMatchLink(null); return; }
  setFrameImage("match-scan", `/api/scans/${scan.id}/image/front?v=${version}`);
  setFrameImage("immich", `/api/immich/asset/${encodeURIComponent(assetId)}/image`);
  $("immich-compare-note").textContent = distanceText;
  $("immich-compare-note").hidden = !distanceText;
  showImmichMatchLink(assetId);
}

async function showImmichMatchLink(assetId) {
  const link = $("link-immich-dup");
  link.hidden = true;
  if (!assetId) return;
  try {
    if (immichBaseUrl === null) immichBaseUrl = ((await api("GET", "/api/immich/settings")).url || "").replace(/\/+$/, "");
  } catch { return; }
  if (!immichBaseUrl || state.current?.immich_duplicate_asset_id !== assetId) return;
  link.href = `${immichBaseUrl}/photos/${encodeURIComponent(assetId)}`;
  link.hidden = false;
}

let immichFormDirty = false;
$("immich-settings-form").addEventListener("input", () => { immichFormDirty = true; });

async function loadImmichSettings() {
  const s = await api("GET", "/api/immich/settings");
  if (!immichFormDirty) {  // a refresh never overwrites what the operator is typing
    $("immich-url").value = s.url;
    $("immich-library-id").value = s.library_id;
    $("immich-enabled").checked = s.enabled;
    $("immich-api-key").value = "";
  }
  $("immich-api-key").placeholder = s.api_key_set ? `•••• ${s.api_key_last4}` : "paste a new key to change it";
  $("immich-key-state").textContent = s.api_key_set ? "A key is saved. Leave blank to keep it." : "No key saved yet.";
  renderImmichConnection(s);
}

function renderImmichConnection(s) {
  $("immich-connection").textContent = s.enabled ? "Enabled" : "Disabled - turn on Enabled and save to check";
  $("btn-immich-check").disabled = !s.enabled;
}

$("immich-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    enabled: $("immich-enabled").checked,
    url: $("immich-url").value.trim(),
    library_id: $("immich-library-id").value.trim(),
  };
  if ($("immich-api-key").value) body.api_key = $("immich-api-key").value;
  try {
    const saved = await api("PATCH", "/api/immich/settings", body);
    immichBaseUrl = null;
    immichFormDirty = false;
    $("immich-save-state").textContent = "Saved";
    setTimeout(() => ($("immich-save-state").textContent = ""), 2000);
    await loadImmichSettings();
    renderImmichConnection(saved);
    loadHealth();
  } catch (e2) {
    failToast(e2);
  }
});

$("btn-immich-test").onclick = () => withButton($("btn-immich-test"), async () => {
  const status = await api("GET", "/api/immich/status");
  if (!status.enabled) toast("Turn on Enabled and save first");
  else if (status.connected) toast(`Connected${status.asset_count != null ? ` – ${status.asset_count} asset(s)` : ""}`);
  else toast(`${status.error || "Could not connect"}\nCheck the URL and key, then Test connection again. [IMMICH_UNREACHABLE]`, true);
});

let immichCheckPoll = null;
let lastImmichCheck = null; // last /api/immich/check/status payload: the strip's job count reads it

function renderImmichProgress(status) {
  lastImmichCheck = status;
  const wrap = $("immich-progress-wrap");
  const reading = status.state === "running" && status.phase === "assets";
  wrap.hidden = !(reading || (status.state === "done" && status.total));
  if (typeof renderStrip === "function") renderStrip();
  if (wrap.hidden) return;
  const total = status.total || 0;
  const done = status.state === "done" ? total || status.done : Math.min(status.done, total || status.done);
  $("immich-progress").max = Math.max(total, done, 1);
  $("immich-progress").value = done;
  $("immich-progress-text").textContent = total
    ? `${done.toLocaleString()} of ${total.toLocaleString()} photos (${Math.floor((100 * done) / total)}%)`
    : `${done.toLocaleString()} photos`;
}

function pollImmichCheck() {
  clearInterval(immichCheckPoll);
  const tick = async () => {
    const status = await api("GET", "/api/immich/check/status");
    $("immich-check-message").textContent = status.message || (status.state === "running" ? "Checking…" : "");
    renderImmichProgress(status);
    $("btn-immich-check").disabled = status.state === "running";
    if (status.state !== "running") { clearInterval(immichCheckPoll); immichCheckPoll = null; }
    return status;
  };
  immichCheckPoll = setInterval(async () => {
    const status = await tick();
    if (status.state === "done") { toast("Immich check finished"); await refresh(); }
    else if (status.state === "failed") toast(`${status.message || "Immich check failed"}\nNothing was changed. Check the Immich settings and run it again. [IMMICH_CHECK_FAILED]`, true);
  }, 1000);
}

// Opening the panel while a check is already running (started earlier, or from another tab) picks its progress up.
async function resumeImmichCheckDisplay() {
  const status = await api("GET", "/api/immich/check/status");
  renderImmichProgress(status);
  if (status.state === "running") {
    $("immich-check-message").textContent = status.message || "Checking…";
    if (!immichCheckPoll) pollImmichCheck();
  }
}

$("btn-immich-check").onclick = () => withButton($("btn-immich-check"), async () => {
  const ok = await confirmAction({
    title: "Check Immich for duplicates?",
    body: "Contacts your Immich server (read-only) to compare exported scans and library photos. This can take a while for a large library.",
    confirmLabel: "Check now",
  });
  if (!ok) return;
  try {
    await api("POST", "/api/immich/check");
  } catch (e) {
    failToast(e);
    return;
  }
  pollImmichCheck();
});

// ---------- account ----------
$("btn-logout").onclick = async () => {
  try { await api("POST", "/api/auth/logout"); } catch { /* already signed out */ }
  location.replace("/login");
};

// ---------- dev live reload (only when the server runs with BANANA_DEV_RELOAD=1) ----------
(async function devReload() {
  let token = null;
  try {
    const res = await fetch("/api/dev/reload-token");
    if (!res.ok) return;
    const data = await res.json();
    if (!data.enabled) return; // normal server: no live reload
    token = data.token;
  } catch { return; }
  setInterval(async () => {
    try {
      const next = (await (await fetch("/api/dev/reload-token", { cache: "no-store" })).json()).token;
      if (next !== token && !state.dirty && !document.querySelector("dialog[open]")) location.reload();
    } catch { /* server restarting */ }
  }, 1000);
})();

// ---------- start (after every script has loaded: stage.js and tour.js add to this page) ----------
document.addEventListener("DOMContentLoaded", () => {
  renderShortcutHints();
  // Sign-in is optional (off in the single-computer desktop build): without it there is no account to show or end.
  api("GET", "/api/auth/state").then((s) => {
    $("user-chip").textContent = s.required ? s.username || "" : "";
    $("user-chip").hidden = !s.required;
    $("btn-logout").hidden = !s.required;
  }).catch(() => {});
  loadHealth();
  setInterval(loadHealth, 30000);
  loadScannerStatus();
  setInterval(loadScannerStatus, 30000);
  loadLearningCount();
  resumeImmichCheckDisplay().catch(() => {});
  selectTab(state.status);
});
