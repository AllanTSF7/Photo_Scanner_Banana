"use strict";
/* Guided workflow tour (driver.js, vendored in vendor/driver/). Rules: CLAUDE.md > UI DESIGN SYSTEM.
   - Never starts by itself: the operator opens it with the Guide button (or "G").
   - Popovers are styled from theme.css tokens in app.css; motion follows prefers-reduced-motion.
   - Steps whose control isn't on screen (no scanner, no scan selected) are skipped, so the tour never
     points at nothing. It selects the first scan when the editor is empty, changing nothing else. */

(function () {
  const TOUR_KEY = "tourCompleted";
  const $ = (id) => document.getElementById(id);
  const visible = (el) => !!el && !el.closest("[hidden]") && el.getClientRects().length > 0;

  function step(selector, title, description, options = {}) {
    return { element: selector, popover: { title, description, ...options } };
  }

  function buildSteps() {
    const scannerOn = visible($("btn-scan"));
    const steps = [
      step(undefined, "The review workflow",
        "Photos come in from the scanner, the app suggests a date, text and names, you check each one, and approved scans are exported for Immich. This guide walks through that, in order. Use Next, or press Escape to leave."),
      scannerOn
        ? step("#btn-scan", "1. Scan photos",
            "Load the feeder and click Scan feeder. <strong>Feed</strong> chooses the whole stack or a single photo; <strong>After scan</strong> decides whether new photos land in the review queue or just in the inbox. Each photo is scanned front and back.")
        : step("#btn-ingest", "1. Bring photos in",
            "Click Ingest inbox to pick up scan files that are already in the inbox folder. When the scanner is online you also get a Scan feeder button here."),
      step(".queue", "2. The queue",
        "Every scan waiting for you. The tabs above filter by state: To review, Approved, Exported, Rejected. Marks show a possible rescan (<em>dup?</em>) and whether the back has writing.",
        { side: "right", align: "start" }),
      step(".images", "3. Front and back",
        "The photo and the back of the print, cropped automatically from the full scanner page. Rotate each side if it came out sideways; the original scan file is never changed.",
        { side: "top" }),
      step(".editor-tools", "4. Fix the scan itself",
        "<strong>Swap front/back</strong> when the picture shows up as the back. <strong>Re-detect crop</strong> if the trim is wrong. <strong>Flag duplicate</strong> marks a rescan you don't want. Previous and Next move through the queue.",
        { side: "bottom" }),
      step("#ocr-panel", "5. Text on the back",
        "Printed text on the back is read automatically. Click a line to add it to the description. <strong>Fix</strong> corrects a misread line and <strong>Not text</strong> rejects lab codes and smudges. Approved fixes teach the app, and the same misreading is corrected by itself next time.",
        { side: "top" }),
      step("#date-text", "6. Date",
        "Type the date as it's written on the photo: <em>Xmas '84</em>, <em>Summer of 1979</em>, <em>1960s</em>. The line underneath shows how it was understood and where the photo lands on the Immich timeline.",
        { side: "top" }),
      step("#description", "7. Description",
        "Text from the back, or your own note. People, places and events are suggested from whatever is in here.",
        { side: "top" }),
      step(".grid2", "8. People, places, events, tags",
        "Filled in from the description. <strong>Dashed chips are suggestions</strong> until you approve or edit the field: click a “+” chip to add it, or the × to remove one, and it won't be suggested again for this photo.",
        { side: "top" }),
      step(".form-actions", "9. Approve or reject",
        "<strong>Approve</strong> queues the photo for export and records what you changed, so the app gets better at the next batch. <strong>Reject</strong> leaves it out; the original scan stays in the archive either way. The letters on the buttons are keyboard shortcuts.",
        { side: "top" }),
      step("#btn-export", "10. Export approved",
        "Writes every approved photo into the library folder with its date and tags, ready for Immich to import. It asks for confirmation first, because exported files leave the app.",
        { side: "bottom", align: "end" }),
      step("#btn-learning", "What the app learns",
        "Corrections you approved, the fixes it reuses, and threshold suggestions it proposes. Nothing is applied until you press Apply, and nothing leaves this machine.",
        { side: "bottom", align: "start" }),
      step("#btn-health", "Is everything working?",
        "Green means the scanner, folders, text reader and database are all fine. Open it when something looks off.",
        { side: "bottom", align: "start" }),
      step(undefined, "That's the loop",
        "Scan, check, approve, export. Reopen this guide any time with the <strong>Guide</strong> button, or by pressing G."),
    ];
    return steps.filter((s) => !s.element || visible(document.querySelector(s.element)));
  }

  // Colors come from theme.css only: read the token, and let driver.js use its own default if it's missing.
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  async function startTour() {
    const factory = window.driver?.js?.driver;
    if (!factory) { window.toast?.("The guide didn't load", true); return; }

    // Editor steps need a scan open: open the first one in the queue exactly as an operator would.
    const firstScan = document.querySelector("#scan-list li");
    if (!visible($("editor")) && firstScan) {
      firstScan.click();
      for (let i = 0; i < 40 && !visible($("editor")); i++) await new Promise((r) => setTimeout(r, 50));
    }
    document.getElementById("health-panel").hidden = true;
    document.getElementById("learning-panel").hidden = true;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const tour = factory({
      showProgress: true,
      animate: !reduceMotion,
      allowClose: true,
      ...(token("--scrim") ? { overlayColor: token("--scrim"), overlayOpacity: 1 } : {}),
      stagePadding: 6,
      stageRadius: 8,
      popoverClass: "photo-scanner-tour",
      nextBtnText: "Next",
      prevBtnText: "Back",
      doneBtnText: "Done",
      progressText: "{{current}} of {{total}}",
      steps: buildSteps(),
    });

    // driver.js's onDestroyed hook only fires when it still has an active element/step to report - which isn't
    // true on the tour's first and last (center-screen) steps, so leaving Escape or Done on one of those would
    // silently skip it. document.body loses the "driver-active" class on every exit path, gated or not, so that's
    // the one signal to watch instead.
    const finish = () => {
      observer.disconnect();
      try { localStorage.setItem(TOUR_KEY, "1"); } catch { /* storage unavailable */ }
      document.getElementById("btn-guide")?.focus();
    };
    const observer = new MutationObserver(() => {
      if (!document.body.classList.contains("driver-active")) finish();
    });
    observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    tour.drive();
  }

  const button = document.getElementById("btn-guide");
  if (button) button.onclick = () => startTour();
  window.startTour = startTour; // used by the UI tests
})();
