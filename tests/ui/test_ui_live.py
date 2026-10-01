"""Live UI component tests: a real server, real headless Chromium, every button and field exercised.

Each test gets its own server process with temporary folders, demo scans and a fake scanner,
so nothing touches the operator's data or the real scanner.
Requires: pip install playwright && python -m playwright install --with-deps chromium
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import make_demo_inbox  # noqa: E402

FAKE_SCANIMAGE = """#!{python}
import sys
from PIL import Image
pattern = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--batch="))
for n in range(1, 5):
    Image.new("RGB", (900, 600), (40 * n, 120, 200 - 30 * n)).save(pattern % n, quality=85)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeScannerPort:
    """Accepts TCP connections so the status check reports the scanner as online."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def read_text(request):
    """Override with @pytest.mark.parametrize("read_text", [True], indirect=True) to run real OCR on ingest."""
    return getattr(request, "param", False)


@pytest.fixture
def server(tmp_path, read_text):
    for name in ("inbox", "archive", "sorted", "data"):
        (tmp_path / name).mkdir()
    make_demo_inbox.main(tmp_path / "inbox")
    scanimage = tmp_path / "scanimage"
    scanimage.write_text(FAKE_SCANIMAGE.format(python=sys.executable))
    scanimage.chmod(scanimage.stat().st_mode | stat.S_IEXEC)
    fake_port = _FakeScannerPort()

    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n"
        + "".join(f'{n} = "{(tmp_path / n).as_posix()}"\n' for n in ("inbox", "archive", "sorted", "data")).replace(
            "data =", "data_dir ="
        )
        # backend = "sane" always: on Windows "auto" means Epson's real TWAIN driver, and a test must never drive
        # real hardware (it did before this was pinned - "No photos in the feeder" came from the real scanner).
        + f'[scanner]\nhost = "127.0.0.1"\nport = {fake_port.port}\nbackend = "sane"\nscanimage = "{scanimage.as_posix()}"\n'
        + f"[analysis]\nread_text = {'true' if read_text else 'false'}\n"
        + "[ingest]\nsettle_seconds = 0\nwatch_inbox = false\n",
        encoding="utf-8",
    )
    from banana import auth, db

    with db.session(db.make_engine(tmp_path / "data" / "banana.db")) as session:
        auth.create_user(session, UI_USER, UI_PASSWORD)
    port = _free_port()
    env = {**os.environ, "BANANA_CONFIG": str(config), "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "banana.web.api:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode())
            time.sleep(0.1)
    yield {"base": base, "root": tmp_path}
    proc.terminate()
    proc.wait(10)
    fake_port.close()


UI_USER, UI_PASSWORD = "operator", "ui-tests-password"

# The fake scanimage is a POSIX shell script (same reason tests/test_scanner.py skips on Windows).
needs_posix_scanner = pytest.mark.skipif(os.name == "nt", reason="fake scanimage is a POSIX script")
needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def signed_in(browser, base, **kwargs):
    """A browser context already signed in (the session cookie is shared by its pages)."""
    context = browser.new_context(**kwargs)
    response = context.request.post(base + "/api/auth/login", data={"username": UI_USER, "password": UI_PASSWORD})
    assert response.ok, response.text()
    return context


@pytest.fixture(scope="module")
def browser():
    try:
        with playwright_api.sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    except Exception as exc:  # browsers not installed
        pytest.skip(f"chromium not available: {exc}")


@pytest.fixture
def page(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 1440, "height": 900})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # HTTP error replies the UI handles itself (404 preview, 409/422 validation) are logged by the browser, not bugs.
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.goto(server["base"] + "/")
    yield pg
    context.close()
    assert not errors, f"browser errors: {errors}"


def toast(pg):
    return pg.locator("#toast")


def ingest(pg, count=7):
    pg.click("#btn-ingest")
    expect(toast(pg)).to_contain_text(f"Ingested {count} scan(s)")
    expect(pg.locator("#scan-list li")).to_have_count(count)


def no_horizontal_scroll(pg) -> bool:
    return pg.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth + 1")


# ---------------------------------------------------------------- layout


def test_header_controls_and_empty_state(page):
    expect(page).to_have_title("Photo Scanner · Review")
    expect(page.locator(".brand")).to_have_text("Photo Scanner review")
    for label in ("To review", "Approved", "Exported", "Rejected", "All"):
        tab = page.locator(".tab", has_text=label)
        expect(tab).to_be_visible()
        expect(tab).to_be_enabled()
    expect(page.locator("#empty-list")).to_be_visible()
    expect(page.locator("#btn-ingest")).to_be_enabled()
    expect(page.locator("#btn-export")).to_be_enabled()
    expect(page.locator("a", has_text="API docs")).to_have_attribute("href", "/docs")
    expect(page.locator("#link-download-windows")).to_have_attribute("href", "/api/downloads/windows")
    expect(page.locator("#scanner-status")).to_have_text("Scanner online")
    expect(page.locator("#btn-scan")).to_be_enabled()
    assert page.locator("select").count() == 0 or all(
        s.locator("option").count() > 0 for s in page.locator("select").all()
    )


@pytest.mark.parametrize("width,height", [(1440, 900), (1024, 768), (400, 800)])
def test_responsive_no_horizontal_scroll(browser, server, width, height):
    context = signed_in(browser, server["base"], viewport={"width": width, "height": height})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    ingest(pg)
    pg.locator("#scan-list li").first.click()
    expect(pg.locator("#editor")).to_be_visible()
    assert no_horizontal_scroll(pg), f"horizontal scroll at {width}px"
    for selector in ("#btn-ingest", "#btn-export", "#btn-approve", "#btn-reject", "#date-text", "#description"):
        pg.locator(selector).scroll_into_view_if_needed()
        expect(pg.locator(selector)).to_be_visible()
    if width <= 900:
        columns = pg.evaluate("getComputedStyle(document.querySelector('.images')).gridTemplateColumns.split(' ').length")
        assert columns == 1
    context.close()


# ---------------------------------------------------------------- ingest + editor fields


def test_ingest_populates_queue_and_editor(page):
    ingest(page)
    expect(toast(page)).to_contain_text("possible rescan")
    expect(page.locator(".tab", has_text="To review")).to_contain_text("7")
    expect(page.locator("#scan-list .chip", has_text="dup?")).not_to_have_count(0)
    expect(page.locator("#scan-list .chip", has_text="back text")).not_to_have_count(0)

    page.locator("#scan-list li").first.click()
    expect(page.locator("#scan-title")).to_have_text("Scan #000001")
    expect(page.locator("#scan-status")).to_have_text("To review")
    expect(page.locator("#img-front")).to_have_js_property("complete", True)
    assert page.evaluate("document.querySelector('#img-front').naturalWidth") > 0
    assert page.evaluate("document.querySelector('#img-back').naturalWidth") > 0
    expect(page.locator("#back-type")).to_have_text("has writing")
    expect(page.locator("#keep-back")).to_be_checked()


def test_date_field_preview_valid_invalid_and_clear(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    date = page.locator("#date-text")
    expect(date).to_be_editable()

    date.fill("Xmas '84")
    expect(page.locator("#date-preview")).to_contain_text("1984-12-25")
    expect(page.locator("#date-preview")).to_contain_text("day")
    expect(page.locator("#save-state")).to_have_text("unsaved changes")

    date.fill("Summer of 1979")
    expect(page.locator("#date-preview")).to_contain_text("Summer 1979")

    date.fill("sometime maybe")
    expect(page.locator("#date-preview .bad")).to_be_visible()
    page.click("#form button[type=submit]")
    expect(toast(page)).to_contain_text("could not understand date")
    expect(toast(page)).to_have_class("toast error")

    date.fill("")
    expect(page.locator("#date-preview")).to_contain_text("No date")


def test_description_and_chip_inputs(page):
    ingest(page)
    page.locator("#scan-list li").first.click()

    page.fill("#description", "a quiet afternoon outside")  # no names: this test drives the chip inputs by hand
    expect(page.locator("#description")).to_have_value("a quiet afternoon outside")

    for field in ("people", "places", "events", "tags"):
        box = page.locator(f'.chips-input[data-field="{field}"]')
        box.click()  # clicking the box focuses its input
        chip_input = box.locator("input")
        expect(chip_input).to_be_focused()
        chip_input.type("First")
        chip_input.press("Enter")
        chip_input.type("Second,")
        chip_input.type("first")  # duplicate (case-insensitive) is ignored
        chip_input.press("Enter")
        expect(box.locator(".chip")).to_have_count(2)
        box.locator(".chip", has_text="First").locator("button").click()  # remove via x
        expect(box.locator(".chip")).to_have_count(1)
        chip_input = box.locator("input")
        chip_input.click()
        chip_input.press("Backspace")  # remove last chip
        expect(box.locator(".chip")).to_have_count(0)
        chip_input.type("Keep")
        chip_input.press("Enter")

    page.fill("#date-text", "Dec 25, 1984")
    page.click("#form button[type=submit]")
    expect(toast(page)).to_have_text("Saved")
    expect(page.locator("#save-state")).to_have_text("")

    page.reload()
    page.locator("#scan-list li").first.click()
    expect(page.locator("#description")).to_have_value("a quiet afternoon outside")
    expect(page.locator("#date-text")).to_have_value("1984-12-25")
    for field in ("people", "places", "events", "tags"):
        expect(page.locator(f'.chips-input[data-field="{field}"] .chip')).to_have_text(["Keep×"])


def test_chip_added_on_blur_and_ctrl_s_saves(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    box = page.locator('.chips-input[data-field="people"]')
    box.locator("input").type("Blurred")
    page.click("#description")
    expect(box.locator(".chip")).to_have_count(1)
    page.keyboard.press("Control+s")
    expect(toast(page)).to_have_text("Saved")


def test_keep_back_toggle_click_and_key(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    toggle = page.locator("#keep-back")
    figure = page.locator("#fig-back")
    expect(toggle).to_be_enabled()
    toggle.click()
    expect(toggle).not_to_be_checked()
    expect(figure).to_have_class(re.compile(r"\bdropped\b"))
    page.click("#scan-title")  # move focus out of the checkbox
    page.keyboard.press("b")
    expect(toggle).to_be_checked()

    no_back = page.locator("#scan-list li", has_text="#000004")
    no_back.click()
    expect(page.locator("#fig-back .no-back")).to_have_text("No back scanned")
    expect(page.locator("#keep-back")).to_be_disabled()

    blank = page.locator("#scan-list li", has_text="#000003")
    blank.click()
    expect(page.locator("#back-type")).to_have_text("looks blank")
    expect(page.locator("#keep-back")).not_to_be_checked()


# ---------------------------------------------------------------- actions, tabs, keyboard


def test_approve_reject_buttons_tabs_and_counts(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    page.click("#btn-approve")
    expect(toast(page)).to_contain_text("#1 approved")
    expect(page.locator("#scan-list li")).to_have_count(6)
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")  # next one opens

    page.click("#btn-reject")
    expect(toast(page)).to_contain_text("#2 rejected")
    expect(page.locator(".tab", has_text="Approved")).to_contain_text("1")
    expect(page.locator(".tab", has_text="Rejected")).to_contain_text("1")

    page.locator(".tab", has_text="Approved").click()
    expect(page.locator(".tab.active")).to_contain_text("Approved")
    expect(page.locator("#scan-list li")).to_have_count(1)
    expect(page.locator("#scan-status")).to_have_text("Approved")

    page.locator(".tab", has_text="Rejected").click()
    expect(page.locator("#scan-list li")).to_have_count(1)
    page.click("#btn-approve")  # a rejected scan can be approved again
    expect(page.locator(".tab", has_text="Approved")).to_contain_text("2")

    page.locator(".tab", has_text="All").click()
    expect(page.locator("#scan-list li")).to_have_count(7)
    expect(page.locator("#scan-list .chip", has_text="Approved")).to_have_count(2)


def test_keyboard_shortcuts_and_typing_guard(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    page.click("#scan-title")
    page.keyboard.press("j")
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")
    page.keyboard.press("j")
    expect(page.locator("#scan-title")).to_have_text("Scan #000003")
    page.keyboard.press("k")
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")

    page.click("#description")
    page.keyboard.type("ax jk b")  # shortcuts must not fire while typing
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")
    expect(page.locator("#scan-status")).to_have_text("To review")
    expect(page.locator("#description")).to_have_value("ax jk b")

    page.click("#scan-title")
    page.keyboard.press("a")
    expect(toast(page)).to_contain_text("#2 approved")
    page.keyboard.press("x")
    expect(toast(page)).to_contain_text("rejected")


def test_rapid_approve_then_reject_acts_on_the_next_scan(page):
    """Regression: X pressed while Approve was still moving to the next scan was silently dropped (selection cleared).
    It must queue and reject the scan shown next, never the one just approved."""
    ingest(page)
    page.locator("#scan-list li", has_text="#000002").click()
    page.click("#scan-title")
    page.keyboard.press("a")
    page.keyboard.press("x")  # no wait in between
    expect(page.locator(".tab", has_text="Rejected")).to_contain_text("1", timeout=10000)
    expect(page.locator(".tab", has_text="Approved")).to_contain_text("1")
    statuses = page.evaluate("""async () => {
        const scans = await (await fetch('/api/scans')).json();
        return Object.fromEntries(scans.map(s => [s.id, s.status]));
    }""")
    assert statuses["2"] == "approved"
    assert statuses["3"] == "rejected"


def test_unsaved_edits_are_saved_when_switching(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    page.fill("#description", "auto-saved note")
    page.locator("#scan-list li", has_text="#000002").click()
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")
    page.locator("#scan-list li", has_text="#000001").click()
    expect(page.locator("#description")).to_have_value("auto-saved note")


# ---------------------------------------------------------------- pipeline buttons


@needs_exiftool
def test_export_button(page, server):
    ingest(page)
    page.locator("#scan-list li").first.click()
    page.fill("#date-text", "Xmas '84")
    page.click("#btn-approve")

    # Irreversible: a confirm dialog; Cancel exports nothing.
    page.click("#btn-export")
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#confirm-title")).to_have_text("Export 1 approved scan?")
    expect(page.locator("#confirm-cancel")).to_be_focused()
    page.click("#confirm-cancel")
    expect(dialog).to_be_hidden()
    expect(page.locator(".tab", has_text="Exported")).to_contain_text("0")

    page.click("#btn-export")
    expect(page.locator("#confirm-ok")).to_have_text("Export 1 scan")
    page.click("#confirm-ok")
    expect(toast(page)).to_contain_text("Exported 1 scan(s)")
    expect(page.locator(".tab", has_text="Exported")).to_contain_text("1")
    page.locator(".tab", has_text="Exported").click()
    expect(page.locator("#export-info")).to_contain_text("1984/1984-12-25/SCAN_000001_A.jpg")
    assert (server["root"] / "sorted" / "1984" / "1984-12-25" / "SCAN_000001_A.jpg.xmp").exists()

    page.click("#btn-export")
    expect(toast(page)).to_contain_text("Nothing approved to export")


def test_ingest_button_when_inbox_empty(page):
    ingest(page)
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Inbox has no new scans")


@needs_posix_scanner
def test_scan_feeder_button(page):
    expect(page.locator("#scanner-status")).to_have_text("Scanner online")
    page.click("#btn-scan")
    expect(toast(page)).to_contain_text("Scanned 2 photo(s), 4 page(s)", timeout=20000)
    expect(toast(page)).to_contain_text("Ingested")
    expect(page.locator("#scanner-status")).to_have_text("Scanner online")
    expect(page.locator("#btn-scan")).to_be_enabled()
    # the demo inbox files are ingested together with the scanned pages
    expect(page.locator("#scan-list li")).to_have_count(9)


@needs_posix_scanner
def test_scan_destination_dropdown_leave_in_inbox(page, server):
    select = page.locator("#scan-destination")
    expect(select).to_be_visible()
    expect(select).to_be_enabled()
    expect(select.locator("option")).to_have_text(["Add to review queue", "Leave in inbox"])
    assert select.locator("option").evaluate_all("o => o.map(x => x.value)") == ["review", "inbox"]
    expect(select).to_have_value("review")  # config default

    select.select_option("inbox")
    page.click("#btn-scan")
    expect(toast(page)).to_contain_text("4 file(s) left in the inbox", timeout=20000)
    expect(page.locator("#scan-list li")).to_have_count(0)  # nothing ingested
    scanned = [p.name for p in (server["root"] / "inbox").iterdir() if p.name.startswith("scan")]
    assert len(scanned) == 4  # 2 fronts + 2 backs

    page.reload()
    expect(page.locator("#scan-destination")).to_have_value("inbox")  # choice remembered
    ingest(page, count=9)


@needs_posix_scanner
def test_system_health_indicator_and_panel(page):
    button = page.locator("#btn-health")
    expect(button).to_be_enabled()
    expect(page.locator("#health-panel")).to_be_hidden()
    button.click()
    panel = page.locator("#health-panel")
    expect(panel).to_be_visible()
    expect(button).to_have_attribute("aria-expanded", "true")
    names = ["api", "database", "exiftool", "native", "inbox", "archive", "library", "data", "ocr", "entities", "sane", "scanner", "ingest", "immich"]
    expect(panel.locator("li")).to_have_count(len(names))
    for name in ("api", "database", "inbox", "archive", "library", "data", "sane", "scanner"):
        expect(panel.locator(f'li[data-name="{name}"]')).to_have_attribute("data-status", "ok")
    expect(panel.locator('li[data-name="immich"]')).to_have_attribute("data-status", "off")
    expect(page.locator("#health-text")).not_to_have_text("System")
    expect(page.locator("#health-checked")).to_contain_text("checked")

    page.click("#btn-health-recheck")
    expect(page.locator("#btn-health-recheck")).to_be_enabled()
    page.click("#btn-health-close")
    expect(panel).to_be_hidden()
    button.click()
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


@needs_posix_scanner
def test_health_reports_scanner_offline(browser, server, tmp_path):
    import tomllib

    # Point a second server at a closed port to see the failure state.
    cfg = tomllib.loads((server["root"] / "config.toml").read_text())
    closed = _free_port()
    text = (server["root"] / "config.toml").read_text().replace(f"port = {cfg['scanner']['port']}", f"port = {closed}")
    other = tmp_path / "offline.toml"
    other.write_text(text)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "banana.web.api:app", "--host", "127.0.0.1", "--port", str(port)],
        env={**os.environ, "BANANA_CONFIG": str(other), "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        context = signed_in(browser, f"http://127.0.0.1:{port}")
        pg = context.new_page()
        pg.goto(f"http://127.0.0.1:{port}/")
        expect(pg.locator("#scanner-status")).to_have_text("Scanner offline")
        expect(pg.locator("#btn-scan")).to_be_disabled()
        expect(pg.locator("#btn-health .dot")).to_have_class("dot fail")
        expect(pg.locator("#health-text")).to_have_text("1 problem")
        pg.click("#btn-health")
        expect(pg.locator('#health-list li[data-name="scanner"]')).to_have_attribute("data-status", "fail")
        context.close()
    finally:
        proc.terminate()
        proc.wait(10)


def _natural_size(pg, selector):
    pg.wait_for_function(f"document.querySelector('{selector}').complete && document.querySelector('{selector}').naturalWidth > 0")
    return pg.evaluate(f"[document.querySelector('{selector}').naturalWidth, document.querySelector('{selector}').naturalHeight]")


def test_rotate_buttons_and_r_key(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator("#front-crop")).to_have_text(re.compile("^(cropped|full page)$"))
    w, h = _natural_size(page, "#img-front")
    assert w > h

    page.locator('[data-rotate="front"][data-step="90"]').click()
    page.wait_for_function(f"document.querySelector('#img-front').naturalWidth === {h}")
    assert _natural_size(page, "#img-front") == [h, w]

    page.locator('[data-rotate="front"][data-step="-90"]').click()
    page.wait_for_function(f"document.querySelector('#img-front').naturalWidth === {w}")

    bw, bh = _natural_size(page, "#img-back")
    page.locator('[data-rotate="back"][data-step="-90"]').click()
    page.wait_for_function(f"document.querySelector('#img-back').naturalWidth === {bh}")

    page.click("#scan-title")
    page.keyboard.press("r")
    page.wait_for_function(f"document.querySelector('#img-front').naturalWidth === {h}")
    page.keyboard.press("Shift+R")
    page.wait_for_function(f"document.querySelector('#img-front').naturalWidth === {w}")

    page.locator("#scan-list li", has_text="#000004").click()  # no back image
    expect(page.locator('[data-rotate="back"]').first).to_be_disabled()
    expect(page.locator("#btn-swap")).to_be_disabled()


def test_swap_and_redetect_buttons(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    page.fill("#description", "kept through swap")
    front_before = page.evaluate("document.querySelector('#img-front').src")
    page.click("#btn-swap")
    expect(toast(page)).to_have_text("Front and back swapped")
    expect(page.locator("#description")).to_have_value("kept through swap")
    page.wait_for_function(f"document.querySelector('#img-front').src !== {front_before!r}")
    expect(page.locator("#btn-redetect")).to_be_enabled()
    page.click("#btn-redetect")
    expect(toast(page)).to_have_text("Crop detected again")
    expect(page.locator("#btn-redetect")).to_be_enabled()


@needs_posix_scanner
def test_feed_dropdown_one_photo(page, server):
    select = page.locator("#scan-count")
    expect(select.locator("option")).to_have_text(["Whole stack", "One photo"])
    expect(page.locator("#btn-scan")).to_have_text("Scan feeder")
    select.select_option("one")
    expect(page.locator("#btn-scan")).to_have_text("Scan one photo")
    page.click("#btn-scan")
    expect(toast(page)).to_contain_text("Scanned", timeout=20000)
    args = next((server["root"] / "inbox").glob(".scanning-*/args.txt"), None)
    page.reload()
    expect(page.locator("#scan-count")).to_have_value("one")
    expect(page.locator("#btn-scan")).to_have_text("Scan one photo")


def test_text_panel_without_ocr_and_read_text_button(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator("#ocr-panel")).to_be_visible()
    expect(page.locator("#ocr-empty")).to_have_text("No text read yet.")
    expect(page.locator("#btn-read-text")).to_be_enabled()
    page.locator("#scan-list li", has_text="#000004").click()
    expect(page.locator("#ocr-empty")).to_have_text("No back scanned.")
    expect(page.locator("#btn-read-text")).to_be_disabled()


@pytest.mark.parametrize("read_text", [True], indirect=True)
def test_real_ocr_fills_date_description_and_line_buttons(page):
    pytest.importorskip("rapidocr_onnxruntime")
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Ingested 7 scan(s)", timeout=90000)
    page.locator("#scan-list li", has_text="#000001").click()  # back reads: Xmas '84 / Grandma & John
    expect(page.locator("#ocr-lines .ocr-line")).not_to_have_count(0)
    expect(page.locator("#ocr-engine")).to_have_text("read by rapidocr-ppocr")
    expect(page.locator("#date-text")).to_have_value("1984-12-25")
    expect(page.locator("#description")).to_have_value(re.compile("Grandma"))
    expect(page.locator("#ocr-lines .ocr-line.used")).not_to_have_count(0)

    page.fill("#description", "")
    page.locator("#ocr-lines .ocr-line").first.click()
    expect(page.locator("#description")).not_to_have_value("")
    expect(page.locator("#save-state")).to_have_text("unsaved changes")

    page.click("#btn-read-text")
    expect(toast(page)).to_have_text("Text read from the back", timeout=30000)
    expect(page.locator("#btn-read-text")).to_be_enabled()


def _chips(page, field):
    return page.locator(f'.chips-input[data-field="{field}"] .chip:not(.suggestion)')


def test_people_places_events_derived_from_description(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    description = page.locator("#description")
    description.click()
    description.type("Mary's 5th birthday at Lake Erie with Uncle Bob. Wedding of Susan Miller in Chicago")
    expect(_chips(page, "people")).not_to_have_count(0, timeout=10000)
    expect(page.locator("#derive-hint")).to_be_visible()
    people = _chips(page, "people").all_text_contents()
    assert any("Uncle Bob" in p for p in people) and any("Susan Miller" in p for p in people)
    expect(_chips(page, "events")).to_contain_text(["5th Birthday×", "Wedding×"])
    expect(_chips(page, "places").filter(has_text="Lake Erie")).to_have_count(1)
    expect(page.locator("#save-state")).to_have_text("unsaved changes")

    # Removing a derived chip dismisses it: editing the description again won't bring it back.
    _chips(page, "events").filter(has_text="Wedding").locator("button").click()
    description.type(" and cake")
    page.wait_for_timeout(900)
    expect(_chips(page, "events").filter(has_text="Wedding")).to_have_count(0)
    expect(page.locator('.chips-input[data-field="events"] .suggestion', has_text="Wedding")).to_have_count(0)

    page.click("#form button[type=submit]")
    expect(toast(page)).to_have_text("Saved")
    page.reload()
    page.locator("#scan-list li").first.click()
    expect(_chips(page, "events").filter(has_text="5th Birthday")).to_have_count(1)
    expect(_chips(page, "events").filter(has_text="Wedding")).to_have_count(0)


def test_filled_fields_get_suggestion_chips_not_overwrites(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    box = page.locator('.chips-input[data-field="people"]')
    box.locator("input").type("Grandpa Joe")
    box.locator("input").press("Enter")
    page.click("#form button[type=submit]")
    expect(toast(page)).to_have_text("Saved")
    page.reload()
    page.locator("#scan-list li").first.click()
    page.fill("#description", "With Uncle Bob at Lake Erie")
    page.locator("#description").press("End")  # fire input handling
    suggestion = box.locator(".suggestion", has_text="Uncle Bob")
    expect(suggestion).to_be_visible(timeout=10000)
    expect(_chips(page, "people")).to_have_count(1)  # the saved chip wasn't replaced
    suggestion.click()
    expect(_chips(page, "people")).to_have_count(2)
    expect(page.locator("#save-state")).to_have_text("unsaved changes")


@pytest.mark.parametrize("read_text", [True], indirect=True)
def test_learning_loop_fix_line_approve_and_reuse(page):
    """Fix a misread line, reject a noise line, approve: the fix is recorded and reused on the rescan (#7)."""
    pytest.importorskip("rapidocr_onnxruntime")
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Ingested 7 scan(s)", timeout=90000)
    page.locator("#scan-list li", has_text="#000001").click()
    rows = page.locator("#ocr-lines .ocr-row")
    expect(rows).not_to_have_count(0)
    first_text = rows.first.locator(".ocr-line span").first.inner_text()

    # Fix: inline input, Enter saves, badge + original shown.
    rows.first.locator(".line-fix").click()
    editor = rows.first.locator(".line-edit")
    expect(editor).to_be_focused()
    expect(editor).to_have_value(first_text)
    editor.fill("Christmas 1984")
    editor.press("Enter")
    expect(toast(page)).to_contain_text("Line fixed")
    expect(rows.first.locator(".badge.fixed")).to_have_text("fixed")
    expect(rows.first.locator(".was")).to_have_text(first_text)
    expect(rows.first.locator(".ocr-line")).to_contain_text("Christmas 1984")

    # Escape cancels an edit without saving.
    rows.first.locator(".line-fix").click()
    rows.first.locator(".line-edit").fill("nope")
    rows.first.locator(".line-edit").press("Escape")
    expect(rows.first.locator(".ocr-line")).to_contain_text("Christmas 1984")

    # Not text / Restore toggle.
    if rows.count() > 1:
        second = rows.nth(1)
        second.locator(".line-remove").click()
        expect(second).to_have_class(re.compile(r"\bremoved\b"))
        expect(second.locator(".line-remove")).to_have_text("Restore")
        expect(second.locator(".line-fix")).to_be_disabled()
        second.locator(".line-remove").click()
        expect(second).not_to_have_class(re.compile(r"\bremoved\b"))

    # Approve records corrections.
    page.click("#btn-approve")
    expect(toast(page)).to_contain_text("corrections recorded for learning")
    expect(page.locator("#learning-count")).not_to_have_text("0")

    # Learning panel shows the event counts and the new dictionary entry; proposals need more evidence.
    page.click("#btn-learning")
    panel = page.locator("#learning-panel")
    expect(panel).to_be_visible()
    expect(page.locator("#btn-learning")).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#learning-usable")).not_to_have_text("0")
    expect(page.locator('#learning-fields li[data-field="ocr_line"]')).to_be_visible()
    expect(page.locator("#dictionary-list li", has_text="Christmas 1984")).to_be_visible()
    expect(page.locator("#proposal-list li[data-key]")).to_have_count(2)
    expect(page.locator(".proposal-apply").first).to_be_disabled()
    expect(page.locator("#producer-list li")).not_to_have_count(0)
    expect(page.locator("#stage-list li")).to_have_count(5)
    page.click("#btn-learning-refresh")
    expect(page.locator("#btn-learning-refresh")).to_be_enabled()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()

    # The rescan (#7) has the same back: Read text applies the learned fix automatically.
    page.locator(".tab", has_text="All").click()
    page.locator("#scan-list li", has_text="#000007").click()
    page.click("#btn-read-text")
    expect(toast(page)).to_have_text("Text read from the back", timeout=30000)
    fixed_row = page.locator("#ocr-lines .ocr-row", has_text="Christmas 1984")
    expect(fixed_row.locator(".badge.auto")).to_have_text("auto-fixed")
    expect(fixed_row.locator(".was")).to_have_text(re.compile(r"^Xmas\s?'84$"))  # this scan's own raw OCR ("Xmas '84")


def test_learning_panel_and_health_panel_are_exclusive_and_responsive(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 860})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    pg.click("#btn-learning")
    expect(pg.locator("#learning-panel")).to_be_visible()
    expect(pg.locator("#proposal-list li[data-key]")).to_have_count(2)
    assert no_horizontal_scroll(pg), "learning panel scrolls sideways at 400px"
    pg.click("#btn-health")
    expect(pg.locator("#health-panel")).to_be_visible()
    expect(pg.locator("#learning-panel")).to_be_hidden()
    pg.click("#btn-learning")
    expect(pg.locator("#health-panel")).to_be_hidden()
    pg.click("#btn-learning-close")
    expect(pg.locator("#learning-panel")).to_be_hidden()
    context.close()


def test_image_skeleton_clears_after_load(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator("#frame-front")).not_to_have_class(re.compile(r"\bloading\b"))
    expect(page.locator("#img-front")).to_have_class(re.compile(r"\bdeveloped\b"))
    expect(page.locator("#scan-list-skeleton li")).to_have_count(0)
    # busy state on async buttons
    page.click("#btn-redetect")
    expect(page.locator("#btn-redetect")).not_to_have_class(re.compile(r"\bbusy\b"), timeout=15000)


@pytest.mark.parametrize("read_text", [True], indirect=True)
def test_ui_provisional_marking(page):
    """Every value the app suggested carries data-state="suggested" until the operator commits it."""
    pytest.importorskip("rapidocr_onnxruntime")
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Ingested 7 scan(s)", timeout=90000)
    page.locator("#scan-list li", has_text="#000001").click()  # back: Xmas '84 / Grandma & John

    suggested = '[data-state="suggested"]'
    expect(page.locator(f"#date-text{suggested}")).to_have_value("1984-12-25")
    expect(page.locator(f"#description{suggested}")).to_have_value(re.compile("Grandma"))
    expect(page.locator(f"#back-type{suggested}")).to_have_text("has writing")
    expect(page.locator(f"#front-crop{suggested}")).to_be_visible()
    lines = page.locator("#ocr-lines .ocr-line")
    expect(lines).not_to_have_count(0)
    assert lines.count() == page.locator(f"#ocr-lines .ocr-line{suggested}").count()
    derived = page.locator(f'.chips-input[data-field="people"] .chip{suggested}')
    expect(derived).not_to_have_count(0, timeout=10000)
    # Nothing suggested is rendered as committed: every chip that matches a suggestion is marked.
    assert page.locator('.chips-input[data-field="people"] .chip:not([data-state])').count() == 0

    # Editing commits: the field loses its provisional marking.
    page.locator("#date-text").fill("Dec 25, 1984")
    expect(page.locator("#date-text")).not_to_have_attribute("data-state", "suggested")
    page.locator('.chips-input[data-field="people"] input').type("Aunt Sue")
    page.locator('.chips-input[data-field="people"] input').press("Enter")
    assert page.locator(f'.chips-input[data-field="people"] .chip{suggested}').count() == 0

    # Fixing a line commits that line only.
    rows = page.locator("#ocr-lines .ocr-row")
    rows.first.locator(".line-fix").click()
    rows.first.locator(".line-edit").fill("Christmas 1984")
    rows.first.locator(".line-edit").press("Enter")
    expect(toast(page)).to_contain_text("Line fixed")
    expect(rows.first.locator(".ocr-line")).not_to_have_attribute("data-state", "suggested")

    # Approval commits everything.
    page.click("#btn-approve")
    expect(toast(page)).to_contain_text("approved")
    page.locator(".tab", has_text="Approved").click()
    page.locator("#scan-list li", has_text="#000001").click()
    expect(page.locator("#editor [data-state='suggested']:not(.suggestion)")).to_have_count(0)


def test_visible_controls_and_shortcut_hints(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    # Every review-loop shortcut is declared on a visible, labeled control with its hint rendered on it.
    for selector, hint in [("#btn-approve", "A"), ("#btn-next", "J"), ("#btn-prev", "K"), ("#btn-reject", "X"),
                           ("#btn-flag-dup", "D"), ('[data-rotate="front"][data-step="90"]', "R"),
                           ('[data-rotate="front"][data-step="-90"]', "Shift+R"), ("#btn-save", "Ctrl+S")]:
        control = page.locator(selector)
        expect(control).to_be_visible()
        expect(control.locator(".shortcut-hint")).to_have_text(hint)
        assert control.get_attribute("aria-keyshortcuts")

    expect(page.locator("#btn-prev")).to_be_disabled()  # first scan
    page.click("#btn-next")
    expect(page.locator("#scan-title")).to_have_text("Scan #000002")
    expect(page.locator("#btn-prev")).to_be_enabled()
    page.click("#btn-prev")
    expect(page.locator("#scan-title")).to_have_text("Scan #000001")

    page.click("#btn-flag-dup")
    expect(toast(page)).to_have_text("Flagged as duplicate")
    expect(page.locator("#btn-flag-dup")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#scan-operator-dup")).to_be_visible()
    page.click("#scan-title")
    page.keyboard.press("d")
    expect(toast(page)).to_have_text("Duplicate flag cleared")
    expect(page.locator("#btn-flag-dup")).to_have_attribute("aria-pressed", "false")


def test_async_refresh_keeps_field_being_edited(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    description = page.locator("#description")
    description.click()
    description.type("typed while ")
    # An async action re-renders the scan (rotate); the focused description must keep what's typed.
    page.evaluate("rotate('front', 90)")
    page.wait_for_function("document.querySelector('#img-front').naturalWidth < document.querySelector('#img-front').naturalHeight")
    expect(description).to_have_value("typed while ")
    description.type("rotating")
    expect(description).to_have_value("typed while rotating")


def test_every_control_renders_in_plex_at_readable_size(page):
    """Regression: chip-field inputs had no type attribute, missed input[type=text], and fell back to Arial 13.3px."""
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator(".chips-input input").first).to_be_visible()
    offenders = page.evaluate("""() => [...document.querySelectorAll('input, textarea, select, button, label, h1, h2, h3, p, li, span, kbd')]
        .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
        .map(el => {
          const cs = getComputedStyle(el);
          const plex = /Plex (Sans|Mono)/.test(cs.fontFamily.split(',')[0]);
          const size = parseFloat(cs.fontSize);
          const control = /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
          const tooSmall = control ? size < 16 : size < 14;
          return (!plex || tooSmall) ? `${el.tagName}${el.id ? '#' + el.id : ''}.${el.className} font=${cs.fontFamily} size=${size}` : null;
        })
        .filter(Boolean)""")
    assert not offenders, "\n".join(offenders[:10])
    # And the fonts really loaded (vendored files, no fallback).
    loaded = page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
    assert {"Plex Sans", "Plex Mono"} <= set(loaded)


def test_api_docs_page(page, server):
    link = page.locator("a", has_text="API docs")
    expect(link).to_have_attribute("target", "_blank")
    page.goto(server["base"] + "/docs")
    expect(page).to_have_title(re.compile("Photo Scanner API"))


# ---------------------------------------------------------------- guided tour (driver.js)


def test_guide_tour_opens_steps_through_and_completes(page):
    ingest(page)
    expect(page.locator("#scanner-status")).to_have_text("Scanner online")  # scan-feeder step needs this
    page.click("#btn-guide")

    popover = page.locator(".driver-popover")
    expect(popover).to_be_visible()
    expect(popover.locator(".driver-popover-title")).to_have_text("The review workflow")
    expect(page.locator("#health-panel")).to_be_hidden()
    expect(page.locator("#learning-panel")).to_be_hidden()

    # The first editor-only step (front/back images) needs a scan open; the tour opens one itself.
    expect(page.locator("#editor")).to_be_visible()

    next_button = popover.locator(".driver-popover-next-btn")
    seen_titles = [popover.locator(".driver-popover-title").inner_text()]
    for _ in range(13):  # 14 steps total: 1 already shown, 13 more clicks reach the last ("That's the loop")
        next_button.click()
        seen_titles.append(popover.locator(".driver-popover-title").inner_text())
    expect(popover.locator(".driver-popover-title")).to_have_text("That's the loop")
    assert len(set(seen_titles)) == len(seen_titles), f"a step was skipped or repeated: {seen_titles}"

    next_button.click()  # "Done" on the last step
    expect(popover).to_be_hidden()
    assert page.evaluate("localStorage.getItem('tourCompleted')") == "1"
    expect(page.locator("#btn-guide")).to_be_focused()


def test_guide_tour_reopens_with_g_shortcut_and_closes_on_escape(page):
    ingest(page)
    page.click("body")  # focus outside any input, so the shortcut isn't swallowed by the typing guard
    page.keyboard.press("g")
    popover = page.locator(".driver-popover")
    expect(popover).to_be_visible()
    page.keyboard.press("Escape")
    expect(popover).to_be_hidden()
    assert page.evaluate("localStorage.getItem('tourCompleted')") == "1"


def test_guide_tour_at_400px(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    ingest(pg)
    pg.click("#btn-guide")
    popover = pg.locator(".driver-popover")
    expect(popover).to_be_visible()
    assert no_horizontal_scroll(pg), "guide popover causes horizontal scroll at 400px"
    box = popover.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 400 + 1, f"popover off-screen at 400px: {box}"
    pg.keyboard.press("Escape")
    expect(popover).to_be_hidden()
    context.close()


# ---------------------------------------------------------------- autocorrect


def test_autocorrect_fixes_typo_on_blur_with_undo(page):
    ingest(page)
    page.locator("#scan-list li").first.click()

    date = page.locator("#date-text")
    date.fill("Januaru '84")
    date.blur()
    note = page.locator('.autocorrect-note[data-for="date"]')
    expect(note).to_contain_text("Januaru")
    expect(note).to_contain_text("January")
    expect(date).to_have_value("January '84")
    expect(page.locator("#date-preview")).to_contain_text("1984")

    note.locator("button", has_text="Undo").click()
    expect(date).to_have_value("Januaru '84")
    expect(note).to_be_hidden()

    description = page.locator("#description")
    description.fill("Thanksgivng dinner")
    description.blur()
    desc_note = page.locator('.autocorrect-note[data-for="description"]')
    expect(desc_note).to_contain_text("Thanksgiving")
    expect(description).to_have_value("Thanksgiving dinner")

    # Editing the field again dismisses the (now stale) note without reverting the text.
    description.click()
    description.type(" tonight")
    expect(desc_note).to_be_hidden()
    expect(description).to_have_value("Thanksgiving dinner tonight")


def test_autocorrect_leaves_correct_text_and_short_names_alone(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    description = page.locator("#description")
    description.fill("Christmas with Mary and Jon")
    description.blur()
    expect(page.locator('.autocorrect-note[data-for="description"]')).to_be_hidden()
    expect(description).to_have_value("Christmas with Mary and Jon")



# ---------------------------------------------------------------- auto-rotate (banana/analysis/ocr/orientation.py)


def _write_sideways_scan(root):
    """A photo whose back was fed in sideways: readable text, but rotated 90 from upright."""
    from PIL import Image, ImageDraw

    from banana import imaging

    upright = root / "_upright.jpg"
    image = Image.new("RGB", (1400, 700), (250, 246, 236))
    draw = ImageDraw.Draw(image)
    for i, text in enumerate(["Grandma and John", "Lake Erie", "July 4, 1976"]):
        draw.text((120, 120 + i * 150), text, fill=(20, 20, 20), font_size=90)
    image.save(upright)

    inbox = root / "inbox"
    Image.new("RGB", (900, 600), (80, 120, 160)).save(inbox / "Rotated_0001.jpg")  # front: any photo content
    imaging.open_edited(upright, imaging.Edit(None, 90)).save(inbox / "Rotated_0001_b.jpg")  # back: sideways
    upright.unlink()


@pytest.mark.parametrize("read_text", [True], indirect=True)
def test_auto_rotate_chip_shows_and_manual_rotate_still_overrides(page, server):
    pytest.importorskip("rapidocr_onnxruntime")
    _write_sideways_scan(server["root"])
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Ingested 8 scan(s)", timeout=90000)  # 7 demo scans + this one

    scan = page.evaluate("""async () => {
        const scans = await (await fetch('/api/scans')).json();
        return scans.find((s) => s.source_key === 'Rotated_0001');
    }""")
    page.locator(f'#scan-list li[data-id="{scan["id"]}"]').click()
    expect(page.locator("#editor")).to_be_visible()
    assert scan["front_rotation"] == scan["back_rotation"] == 270  # recovers the correction for a 90-degree feed
    assert scan["suggestions"]["rotation_front"]["value"] == 270
    assert scan["suggestions"]["rotation_back"]["value"] == 270

    expect(page.locator("#front-rotation")).to_be_visible()
    expect(page.locator("#front-rotation")).to_have_attribute("data-state", "suggested")
    expect(page.locator("#back-rotation")).to_be_visible()
    expect(page.locator("#back-rotation")).to_have_attribute("data-state", "suggested")

    # The operator can still override either side manually - the escape hatch always works.
    w, h = _natural_size(page, "#img-back")
    page.locator('[data-rotate="back"][data-step="90"]').click()
    page.wait_for_function(f"document.querySelector('#img-back').naturalWidth === {h}")
    expect(page.locator("#back-rotation")).not_to_have_attribute("data-state", "suggested")
    expect(page.locator("#front-rotation")).to_have_attribute("data-state", "suggested")  # front untouched


@pytest.mark.parametrize("read_text", [True], indirect=True)
def test_auto_rotate_chip_at_400px(browser, server):
    pytest.importorskip("rapidocr_onnxruntime")
    _write_sideways_scan(server["root"])
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    pg.click("#btn-ingest")
    expect(toast(pg)).to_contain_text("Ingested 8 scan(s)", timeout=90000)
    scan = pg.evaluate("""async () => {
        const scans = await (await fetch('/api/scans')).json();
        return scans.find((s) => s.source_key === 'Rotated_0001');
    }""")
    pg.locator(f'#scan-list li[data-id="{scan["id"]}"]').click()
    expect(pg.locator("#editor")).to_be_visible()
    expect(pg.locator("#front-rotation")).to_be_visible()
    assert no_horizontal_scroll(pg), "auto-rotate chip causes horizontal scroll at 400px"
    context.close()


# ---------------------------------------------------------------- delete a rejected scan


def test_delete_only_shows_for_rejected_and_removes_the_scan(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator("#btn-delete")).to_be_hidden()  # never offered during normal review

    page.click("#btn-reject")
    expect(toast(page)).to_contain_text("rejected")
    page.locator(".tab", has_text="Rejected").click()
    page.locator("#scan-list li").first.click()
    expect(page.locator("#btn-delete")).to_be_visible()

    page.click("#btn-delete")
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.click("#confirm-cancel")
    expect(page.locator("#confirm-dialog")).to_be_hidden()
    expect(page.locator("#scan-list li")).to_have_count(1)  # cancelling deletes nothing

    page.click("#btn-delete")
    page.click("#confirm-ok")
    expect(toast(page)).to_contain_text("deleted permanently")
    expect(page.locator("#empty-list")).to_be_visible()  # the only rejected scan is gone

    page.locator(".tab", has_text="All").click()
    expect(page.locator("#scan-list li")).to_have_count(6)  # 7 ingested, 1 deleted


def test_delete_button_at_400px(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    ingest(pg)
    pg.locator("#scan-list li").first.click()
    pg.click("#btn-reject")
    pg.locator(".tab", has_text="Rejected").click()
    pg.locator("#scan-list li").first.click()
    expect(pg.locator("#btn-delete")).to_be_visible()
    pg.locator("#btn-delete").scroll_into_view_if_needed()
    assert no_horizontal_scroll(pg), "delete button causes horizontal scroll at 400px"
    context.close()


# ---------------------------------------------------------------- Immich (read-only duplicate check)


def test_immich_panel_settings_save_and_mask_round_trip(page):
    page.click("#btn-immich")
    expect(page.locator("#immich-panel")).to_be_visible()
    expect(page.locator("#health-panel")).to_be_hidden()
    expect(page.locator("#learning-panel")).to_be_hidden()

    page.fill("#immich-url", "http://127.0.0.1:1")  # nothing listens here: fails fast, never hangs
    page.fill("#immich-api-key", "supersecretkey1234")
    page.fill("#immich-library-id", "lib-1")
    page.check("#immich-enabled")
    page.click("#btn-immich-save")
    expect(page.locator("#immich-save-state")).to_have_text("Saved")
    expect(page.locator("#immich-api-key")).to_have_value("")  # never redisplays what was typed
    expect(page.locator("#immich-api-key")).to_have_attribute("placeholder", "•••• 1234")
    assert "supersecretkey1234" not in page.content()

    page.click("#btn-immich-close")
    expect(page.locator("#immich-panel")).to_be_hidden()
    page.click("#btn-immich")  # re-open: settings persisted server-side
    expect(page.locator("#immich-url")).to_have_value("http://127.0.0.1:1")
    expect(page.locator("#immich-enabled")).to_be_checked()
    assert "supersecretkey1234" not in page.content()

    page.click("#btn-immich-test")
    expect(toast(page)).to_contain_text("Could not connect", timeout=10000)

    # Leave it disabled again so other tests in this suite see the default (no network calls) state.
    page.uncheck("#immich-enabled")
    page.click("#btn-immich-save")


def test_immich_panel_at_400px(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    pg.click("#btn-immich")
    expect(pg.locator("#immich-panel")).to_be_visible()
    assert no_horizontal_scroll(pg), "Immich panel causes horizontal scroll at 400px"
    for selector in ("#immich-url", "#immich-api-key", "#immich-library-id", "#immich-enabled",
                      "#btn-immich-test", "#btn-immich-save", "#btn-immich-check"):
        pg.locator(selector).scroll_into_view_if_needed()
        expect(pg.locator(selector)).to_be_visible()
    context.close()


def test_immich_chip_shows_only_for_a_flagged_scan_and_manual_flag_toggles_it(page):
    ingest(page)
    page.locator("#scan-list li").first.click()
    expect(page.locator("#scan-immich-dup")).to_be_hidden()
    expect(page.locator("#btn-flag-immich-dup")).to_have_attribute("aria-pressed", "false")

    page.click("#btn-flag-immich-dup")
    expect(toast(page)).to_contain_text("Flagged as already in Immich")
    expect(page.locator("#scan-operator-immich-dup")).to_be_visible()
    expect(page.locator("#btn-flag-immich-dup")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-flag-immich-dup")).to_have_text("Unflag as in Immich")

    page.click("#btn-flag-immich-dup")
    expect(toast(page)).to_contain_text("Immich flag cleared")
    expect(page.locator("#scan-operator-immich-dup")).to_be_hidden()


def _seed_immich_match(pg, server):
    """Make every scan the UI loads look like the Immich check matched it (asset-123, 2 bits apart)."""
    pg.request.patch(server["base"] + "/api/immich/settings", data={"url": "http://immich.test:2283/"})

    def patch(route):
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        body = response.json()

        def mark(scan):
            scan["immich_duplicate_asset_id"] = "asset-123"
            scan["suggestions"] = {**(scan.get("suggestions") or {}), "immich_duplicate": {
                "value": {"asset_id": "asset-123", "distance": 2}, "producer": "immich-dhash@1"}}

        for scan in (body if isinstance(body, list) else [body]):
            mark(scan)
        route.fulfill(response=response, json=body)

    pg.route(re.compile(r"/api/scans(/\d+)?(\?.*)?$"), patch)


def test_immich_chip_shows_distance_and_link_to_the_match(page, server):
    _seed_immich_match(page, server)
    try:
        ingest(page)
        page.locator("#scan-list li").first.click()
        expect(page.locator("#scan-immich-dup")).to_have_text("in Immich? (differs by 2 of 64)")
        link = page.locator("#link-immich-dup")
        expect(link).to_be_visible()
        expect(link).to_have_attribute("href", "http://immich.test:2283/photos/asset-123")
        expect(link).to_have_attribute("target", "_blank")
        # The match is also shown in the app, beside this scan's front, fetched through the server.
        expect(page.locator("#immich-compare")).to_be_visible()
        expect(page.locator("#img-immich")).to_have_attribute("src", "/api/immich/asset/asset-123/image")
        expect(page.locator("#immich-compare-note")).to_have_text("differs by 2 of 64")
        expect(page.locator("#img-match-scan")).to_have_attribute("src", re.compile(r"/api/scans/\d+/image/front"))
    finally:
        page.request.patch(server["base"] + "/api/immich/settings", data={"url": ""})


def test_immich_match_link_at_400px(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    _seed_immich_match(pg, server)
    pg.goto(server["base"] + "/")
    ingest(pg)
    pg.locator("#scan-list li").first.click()
    expect(pg.locator("#scan-immich-dup")).to_be_visible()
    pg.locator("#immich-compare").scroll_into_view_if_needed()
    expect(pg.locator("#frame-immich")).to_be_visible()
    expect(pg.locator("#frame-match-scan")).to_be_visible()
    pg.locator("#link-immich-dup").scroll_into_view_if_needed()
    expect(pg.locator("#link-immich-dup")).to_be_visible()
    assert no_horizontal_scroll(pg), "Immich match chip/link causes horizontal scroll at 400px"
    pg.request.patch(server["base"] + "/api/immich/settings", data={"url": ""})
    context.close()


def _fake_running_check(pg):
    body = {"state": "running", "phase": "assets", "done": 1234, "total": 83639,
            "message": "Reading the Immich library: 1234 photos looked at, 1234 new", "stats": None}
    pg.route("**/api/immich/check/status", lambda route: route.fulfill(json=body))


def test_immich_progress_bar_shows_photos_read_out_of_the_total(page):
    _fake_running_check(page)
    page.click("#btn-immich")
    expect(page.locator("#immich-progress-wrap")).to_be_visible()
    expect(page.locator("#immich-progress-text")).to_have_text("1,234 of 83,639 photos (1%)")
    assert page.eval_on_selector("#immich-progress", "e => [e.value, e.max]") == [1234, 83639]
    expect(page.locator("#btn-immich-check")).to_be_disabled()  # one check at a time


def test_immich_progress_bar_at_400px(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    _fake_running_check(pg)
    pg.goto(server["base"] + "/")
    pg.click("#btn-immich")
    expect(pg.locator("#immich-progress")).to_be_visible()
    expect(pg.locator("#immich-progress-text")).to_be_visible()
    assert no_horizontal_scroll(pg), "Immich progress bar causes horizontal scroll at 400px"
    context.close()


def test_immich_check_button_disabled_until_enabled(page):
    page.click("#btn-immich")
    expect(page.locator("#btn-immich-check")).to_be_disabled()


def test_sign_in_page_wrong_password_then_sign_in_and_sign_out_at_400px(browser, server):
    context = browser.new_context(viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    expect(pg).to_have_url(server["base"] + "/login")  # not signed in: sent to the sign-in page
    assert no_horizontal_scroll(pg)

    pg.fill("#login-username", UI_USER)
    pg.fill("#login-password", "not the password")
    pg.click("#btn-login")
    expect(pg.locator("#login-error")).to_be_visible()
    expect(pg.locator("#login-error")).to_contain_text("Wrong username or password")

    pg.fill("#login-password", UI_PASSWORD)
    pg.click("#btn-login")
    expect(pg).to_have_url(server["base"] + "/")
    expect(pg.locator("#user-chip")).to_have_text(UI_USER)

    pg.locator("#btn-logout").scroll_into_view_if_needed()
    expect(pg.locator("#btn-logout")).to_be_visible()
    pg.click("#btn-logout")
    expect(pg).to_have_url(server["base"] + "/login")
    pg.goto(server["base"] + "/")
    expect(pg).to_have_url(server["base"] + "/login")  # the session really ended
    context.close()


# ---------------------------------------------------------------- scan problems stay visible


def _strand_run(root, name="scan20260930132056"):
    """A scan run that stopped before its pages reached the inbox: 3 JPEGs + the driver's unconverted last BMP."""
    from PIL import Image

    folder = root / "inbox" / f".scanning-{name}"
    folder.mkdir()
    for n in (1, 2, 3):
        Image.new("RGB", (400, 300), (40 * n, 90, 60)).save(folder / f"page_{n:04d}.jpg", "JPEG")
    Image.new("RGB", (400, 300), (200, 90, 60)).save(folder / "page_0004.bmp", "BMP")
    return folder


def test_scan_alert_recover_button_at_400px(browser, server):
    folder = _strand_run(server["root"])
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")

    alert = pg.locator("#scan-alert")
    expect(alert).to_be_visible()
    expect(alert).to_contain_text("stopped before 4 files reached the review queue")
    expect(alert).to_have_attribute("role", "alert")
    assert no_horizontal_scroll(pg)

    recover = pg.locator("#btn-recover")
    expect(recover).to_be_visible()
    expect(recover).to_be_enabled()
    recover.click()
    expect(toast(pg)).to_contain_text("Recovered 4 file(s)")
    expect(alert).to_be_hidden()
    assert not folder.exists()
    expect(pg.locator("#scan-list li")).to_have_count(9)  # 2 recovered photos + the 7 demo scans
    context.close()


def test_scan_alert_dismiss_button_at_400px_and_comes_back_when_something_changes(browser, server):
    _strand_run(server["root"])
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")

    dismiss = pg.locator("#btn-scan-alert-dismiss")
    expect(dismiss).to_be_visible()
    expect(dismiss).to_be_enabled()
    dismiss.click()
    expect(pg.locator("#scan-alert")).to_be_hidden()
    pg.reload()
    expect(pg.locator("#scan-alert")).to_be_hidden()  # dismissed stays dismissed

    _strand_run(server["root"], name="scan20260930141529")
    pg.reload()
    expect(pg.locator("#scan-alert")).to_be_visible()  # a new problem shows again
    expect(pg.locator("#scan-alert")).to_contain_text("2 scan runs stopped")
    context.close()


def test_retry_unreadable_button_at_400px(browser, server):
    """Photos ingest gave up on sit in inbox/_unreadable; the System panel lists them with a Retry button."""
    import make_demo_inbox as demo

    folder = server["root"] / "inbox" / "_unreadable"
    folder.mkdir()
    demo.front(5).save(folder / "Box_0009.jpg", quality=90)  # readable now (e.g. it was still copying before)
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    pg = context.new_page()
    pg.goto(server["base"] + "/")
    pg.click("#btn-health")
    box = pg.locator("#ingest-unreadable")
    expect(box).to_be_visible()
    expect(box).to_contain_text("Box_0009.jpg")
    expect(pg.locator('#health-list li[data-name="ingest"]')).to_have_attribute("data-status", "warn")
    assert no_horizontal_scroll(pg)

    retry = pg.locator("#btn-retry-unreadable")
    expect(retry).to_be_enabled()
    retry.click()
    expect(toast(pg)).to_contain_text("Ingested")
    expect(box).to_be_hidden()
    context.close()


def test_ingest_button_reports_files_set_aside(page, server):
    ingest(page)
    demo_front = server["root"] / "archive"
    first = next(demo_front.rglob("Attic3_0001.jpg"))
    import shutil as _sh

    _sh.copy(first, server["root"] / "inbox" / "Attic3_0001.jpg")  # the very same photo dropped again
    page.click("#btn-ingest")
    expect(toast(page)).to_contain_text("Already ingested, set aside: Attic3_0001")


# ---------------------------------------------------------------- two editors, one scan


def _two_tabs_on_first_scan(browser, server):
    context = signed_in(browser, server["base"], viewport={"width": 400, "height": 800})
    a, b = context.new_page(), context.new_page()
    a.goto(server["base"] + "/")
    ingest(a)
    for pg in (a, b):
        if pg is b:
            pg.goto(server["base"] + "/")
        pg.locator("#scan-list li").first.click()
        expect(pg.locator("#editor")).to_be_visible()
    return context, a, b


def _save(pg):
    pg.locator("#form button[type=submit]").scroll_into_view_if_needed()
    pg.click("#form button[type=submit]")


def test_conflict_keep_my_changes_at_400px(browser, server):
    context, a, b = _two_tabs_on_first_scan(browser, server)
    a.fill("#description", "Saved in tab A")
    _save(a)
    expect(toast(a)).to_have_text("Saved")

    b.fill("#description", "Typed in tab B")
    _save(b)
    dialog = b.locator("#conflict-dialog")
    expect(dialog).to_be_visible()
    expect(b.locator("#conflict-body")).to_contain_text("Changed there: Description")
    assert no_horizontal_scroll(b)
    keep = b.locator("#conflict-mine")
    expect(keep).to_be_enabled()
    keep.click()
    expect(dialog).to_be_hidden()
    expect(toast(b)).to_have_text("Saved")
    a.reload()
    a.locator("#scan-list li").first.click()
    expect(a.locator("#description")).to_have_value("Typed in tab B")
    context.close()


def test_conflict_use_the_saved_version_at_400px(browser, server):
    context, a, b = _two_tabs_on_first_scan(browser, server)
    a.fill("#description", "Saved in tab A")
    _save(a)
    expect(toast(a)).to_have_text("Saved")

    b.fill("#description", "Typed in tab B")
    _save(b)
    theirs = b.locator("#conflict-theirs")
    expect(theirs).to_be_visible()
    expect(theirs).to_be_enabled()
    theirs.click()
    expect(b.locator("#description")).to_have_value("Saved in tab A")
    expect(toast(b)).to_contain_text("Your changes were not saved")
    context.close()


def test_two_editors_changing_different_fields_keep_both(browser, server):
    context, a, b = _two_tabs_on_first_scan(browser, server)
    a.fill("#description", "Christmas at Grandma's")
    _save(a)
    expect(toast(a)).to_have_text("Saved")
    b.fill("#date-text", "Xmas '84")
    _save(b)
    # Only the date is sent, so A's description can't be lost: saved without asking.
    expect(toast(b)).to_have_text("Saved")
    expect(b.locator("#conflict-dialog")).to_be_hidden()
    b.reload()
    b.locator("#scan-list li").first.click()
    expect(b.locator("#description")).to_have_value("Christmas at Grandma's")
    expect(b.locator("#date-text")).to_have_value("1984-12-25")
    context.close()
