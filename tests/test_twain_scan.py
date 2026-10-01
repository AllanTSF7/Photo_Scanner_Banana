"""TWAIN scanning path (Windows, Epson Scan 2) against a fake pytwain module: no driver or scanner needed."""

from __future__ import annotations

import time
import types

import pytest
from PIL import Image

from banana.config import ScannerConfig
from banana.scanner import sane
from banana.scanner.twain_scan import acquire_pages, pick_source


def _fake_twain(pages: int = 2, loaded: bool = True, sources=("EPSON FF-680W",)):
    t = types.SimpleNamespace(
        CAP_FEEDERLOADED=1, CAP_FEEDERENABLED=2, CAP_DUPLEXENABLED=3, CAP_XFERCOUNT=4, ICAP_PIXELTYPE=5,
        ICAP_XRESOLUTION=6, ICAP_YRESOLUTION=7, ICAP_AUTOMATICBORDERDETECTION=8, ICAP_AUTOMATICDESKEW=9,
        ICAP_AUTOMATICROTATE=10, TWTY_BOOL=6, TWTY_INT16=1, TWTY_UINT16=4, TWTY_FIX32=7,
        TWPT_RGB=2, TWPT_GRAY=1, TWPT_BW=0,
    )
    t.set_calls, t.closed, t.opened = {}, [], []

    class Source:
        def get_capability(self, cap):
            assert cap == t.CAP_FEEDERLOADED
            return (6, (0, 0, [1 if loaded else 0]))

        def set_capability(self, cap, kind, value):
            t.set_calls[cap] = value

        def acquire_file(self, before, after, show_ui, modal):
            t.show_ui = show_ui
            for n in range(pages):
                path = before({})
                Image.new("RGB", (40, 30), (10 * n, 80, 120)).save(path, "BMP")
                after(pages - n - 1)

        def close(self):
            t.closed.append("source")

    class SourceManager:
        source_list = list(sources)

        def open_source(self, name):
            t.opened.append(name)
            return Source()

        def close(self):
            t.closed.append("manager")

    t.SourceManager = SourceManager
    return t


def test_acquire_writes_jpeg_pages_in_scan_order_and_never_shows_the_driver_ui(tmp_path):
    fake = _fake_twain(pages=4)
    pages = acquire_pages(ScannerConfig(host="x"), tmp_path, count="all", twain=fake)

    assert [p.name for p in pages] == ["page_0001.jpg", "page_0002.jpg", "page_0003.jpg", "page_0004.jpg"]
    assert all(p.exists() for p in pages) and not list(tmp_path.glob("*.bmp"))  # BMPs converted and removed
    assert fake.show_ui is False
    assert fake.opened == ["EPSON FF-680W"]
    assert fake.closed == ["source", "manager"]


def test_acquire_applies_duplex_resolution_crop_deskew_and_rotate(tmp_path):
    fake = _fake_twain()
    cfg = ScannerConfig(host="x", resolution=600, auto_crop=True, skew_correction=True, auto_rotate=True)
    acquire_pages(cfg, tmp_path, count="one", twain=fake)
    s = fake.set_calls
    assert s[fake.CAP_DUPLEXENABLED] is True
    assert s[fake.CAP_XFERCOUNT] == 2  # one photo, both sides
    assert s[fake.ICAP_PIXELTYPE] == fake.TWPT_RGB
    assert s[fake.ICAP_XRESOLUTION] == 600.0
    assert s[fake.ICAP_AUTOMATICBORDERDETECTION] is True
    assert s[fake.ICAP_AUTOMATICDESKEW] is True
    assert s[fake.ICAP_AUTOMATICROTATE] is True


def test_whole_stack_is_capped_at_the_feeder_capacity(tmp_path):
    fake = _fake_twain()
    acquire_pages(ScannerConfig(host="x", max_feeder_count=36), tmp_path, count="all", twain=fake)
    assert fake.set_calls[fake.CAP_XFERCOUNT] == 72


def test_empty_feeder_is_reported_plainly_and_the_driver_is_still_closed(tmp_path):
    fake = _fake_twain(loaded=False)
    with pytest.raises(RuntimeError, match="No photos in the feeder"):
        acquire_pages(ScannerConfig(host="x"), tmp_path, twain=fake)
    assert fake.closed == ["source", "manager"]


def test_pick_source_prefers_the_ff680w_and_explains_when_nothing_is_installed():
    assert pick_source(["WIA-Ricoh", "EPSON DS-530", "EPSON FF-680W"]) == "EPSON FF-680W"
    assert pick_source(["WIA-Ricoh", "EPSON DS-530"]) == "EPSON DS-530"
    assert pick_source(["A", "B"], wanted="B") == "B"
    with pytest.raises(RuntimeError, match="Install Epson Scan 2"):
        pick_source([])
    with pytest.raises(RuntimeError, match="not found"):
        pick_source(["A"], wanted="EPSON FF-680W")


def test_scan_controller_routes_through_twain_and_pairs_pages_into_the_inbox(tmp_path, monkeypatch):
    fake = _fake_twain(pages=4)
    from banana.scanner import twain_scan

    monkeypatch.setattr(twain_scan, "acquire_pages", lambda cfg, staging, count: acquire_pages(cfg, staging, count, twain=fake))
    cfg = ScannerConfig(host="192.168.16.178", backend="twain", first_side="back")
    ctl = sane.ScanController(cfg, tmp_path / "inbox")
    ctl.start(destination="inbox", count="all")
    for _ in range(100):
        if ctl.status()["state"] != "scanning":
            break
        time.sleep(0.05)
    status = ctl.status()
    assert status["state"] == "done", status["message"]
    assert status["pages"] == 4
    assert sorted(status["files"]) == sorted([
        f"{status['run_name']}_0001.jpg", f"{status['run_name']}_0001_b.jpg",
        f"{status['run_name']}_0002.jpg", f"{status['run_name']}_0002_b.jpg",
    ])


def test_missing_scanimage_says_what_is_missing_instead_of_a_bare_winerror(tmp_path):
    cfg = ScannerConfig(host="192.168.16.178", backend="sane", scanimage=str(tmp_path / "no-such-scanimage"))
    ctl = sane.ScanController(cfg, tmp_path / "inbox")
    ctl.start(destination="inbox", count="one")
    for _ in range(100):
        if ctl.status()["state"] != "scanning":
            break
        time.sleep(0.05)
    assert ctl.status()["state"] == "failed"
    assert "scanimage wasn't found" in ctl.status()["message"]


def test_auto_backend_is_twain_on_windows_and_sane_elsewhere(monkeypatch):
    import banana.config as config

    monkeypatch.setattr(config.sys, "platform", "win32")
    assert ScannerConfig().effective_backend == "twain"
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert ScannerConfig().effective_backend == "sane"
    assert ScannerConfig(backend="twain").effective_backend == "twain"


def _fake_twain_that_raises_after_last_page(pages: int, exc: Exception):
    """The 2026-09-30 failure: the driver hands over the last page (its BMP is written) and then raises
    before after() runs, so that page is never converted and acquire_file errors out."""
    fake = _fake_twain(pages=pages)
    source_cls = fake.SourceManager().open_source("x").__class__

    def acquire_file(self, before, after, show_ui, modal):
        for n in range(pages):
            path = before({})
            Image.new("RGB", (40, 30), (10 * n, 80, 120)).save(path, "BMP")
            if n < pages - 1:
                after(pages - n - 1)
        raise exc

    source_cls.acquire_file = acquire_file
    fake.opened.clear()
    return fake


def _wait(ctl):
    for _ in range(200):
        if ctl.status()["state"] != "scanning":
            return ctl.status()
        time.sleep(0.05)
    raise AssertionError("scan never finished")


def test_driver_error_after_the_last_page_keeps_every_page(tmp_path):
    from banana.scanner.twain_scan import ScanInterrupted

    fake = _fake_twain_that_raises_after_last_page(4, OSError("transfer ended"))
    with pytest.raises(ScanInterrupted) as info:
        acquire_pages(ScannerConfig(host="x"), tmp_path, count="all", twain=fake)
    assert [p.name for p in info.value.pages] == ["page_0001.jpg", "page_0002.jpg", "page_0003.jpg", "page_0004.jpg"]
    assert not list(tmp_path.glob("*.bmp"))  # the unconverted last page was converted, not left behind
    assert "transfer ended" in str(info.value)
    assert fake.closed == ["source", "manager"]


def test_driver_error_before_any_page_is_still_a_failure(tmp_path):
    fake = _fake_twain_that_raises_after_last_page(0, OSError("paper jam"))
    with pytest.raises(OSError, match="paper jam"):
        acquire_pages(ScannerConfig(host="x"), tmp_path, twain=fake)


def test_interrupted_run_still_reaches_the_inbox_with_a_visible_warning(tmp_path, monkeypatch, caplog):
    fake = _fake_twain_that_raises_after_last_page(4, OSError("transfer ended"))
    from banana.scanner import twain_scan

    monkeypatch.setattr(twain_scan, "acquire_pages", lambda cfg, staging, count: acquire_pages(cfg, staging, count, twain=fake))
    ctl = sane.ScanController(ScannerConfig(host="h", backend="twain"), tmp_path / "inbox")
    with caplog.at_level("INFO", logger="banana.scanner"):
        ctl.start(destination="inbox")
        status = _wait(ctl)

    assert status["state"] == "done"
    assert "transfer ended" in status["warning"] and "4 page(s)" in status["warning"]
    assert len(status["files"]) == 4
    assert ctl.stranded() == []  # nothing left in a hidden staging folder
    assert "transfer ended" in caplog.text  # the cause is in the log, not only on screen


def _stranded_run(inbox, name, jpgs, bmp=True, broken_bmp=False):
    folder = inbox / f".scanning-{name}"
    folder.mkdir(parents=True)
    for n in range(1, jpgs + 1):
        Image.new("RGB", (40, 30), (n, 80, 120)).save(folder / f"page_{n:04d}.jpg", "JPEG")
    if bmp:
        Image.new("RGB", (40, 30), (200, 80, 120)).save(folder / f"page_{jpgs + 1:04d}.bmp", "BMP")
    if broken_bmp:
        (folder / f"page_{jpgs + 2:04d}.bmp").write_bytes(b"BM" + b"\0" * 20)  # a transfer cut off mid-file
    return folder


def test_recover_finishes_stranded_runs_into_the_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    folder = _stranded_run(inbox, "scan20260930132056", jpgs=3)  # 3 JPEGs + the unconverted last BMP
    ctl = sane.ScanController(ScannerConfig(host="h", backend="twain", first_side="back"), inbox)

    assert ctl.stranded() == [{"run": "scan20260930132056", "files": 4}]
    result = ctl.recover()

    assert result == {"recovered": [{"run": "scan20260930132056", "files": 4}], "kept": []}
    assert not folder.exists()
    assert sorted(p.name for p in inbox.iterdir()) == [
        "scan20260930132056_0001.jpg", "scan20260930132056_0001_b.jpg",
        "scan20260930132056_0002.jpg", "scan20260930132056_0002_b.jpg",
    ]
    assert ctl.stranded() == []


def test_recover_never_deletes_a_page_it_cannot_read(tmp_path):
    inbox = tmp_path / "inbox"
    folder = _stranded_run(inbox, "scan1", jpgs=2, bmp=False, broken_bmp=True)
    ctl = sane.ScanController(ScannerConfig(host="h", backend="twain"), inbox)

    result = ctl.recover()
    assert result["recovered"] == [{"run": "scan1", "files": 2}]
    assert result["kept"] == [{"run": "scan1", "files": 1}]
    assert (folder / "page_0004.bmp").exists()  # left exactly where it was for a person to look at


def test_recover_refuses_while_a_scan_is_running(tmp_path):
    ctl = sane.ScanController(ScannerConfig(host="h", backend="twain"), tmp_path / "inbox")
    ctl._run = sane.ScanRun(state="scanning")
    with pytest.raises(RuntimeError, match="scan is running"):
        ctl.recover()
