"""SANE scanning with a fake `scanimage` that writes pages like the real --batch mode."""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from banana.config import ScannerConfig
from banana.scanner import sane

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake scanimage is a POSIX script")

FAKE = """#!{python}
import sys
from pathlib import Path
from PIL import Image
pages = {pages}
if pages == 0:
    print("scanimage: sane_start: Document feeder out of documents", file=sys.stderr)
    sys.exit(7)
pattern = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--batch="))
Path(pattern).parent.joinpath("args.txt").write_text("\\n".join(sys.argv[1:]))
for n in range(1, pages + 1):
    Image.new("RGB", (400, 300), (n * 20 % 255, 90, 160)).save(pattern % n, quality=80)
"""


def fake_scanimage(tmp_path: Path, pages: int) -> str:
    script = tmp_path / f"scanimage_{pages}"
    script.write_text(FAKE.format(python=sys.executable, pages=pages))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def wait(controller: sane.ScanController) -> dict:
    for _ in range(200):
        status = controller.status()
        if status["state"] != "scanning":
            return status
        time.sleep(0.05)
    raise TimeoutError


def test_args():
    cfg = ScannerConfig(host="192.168.16.178", resolution=300, auto_crop=False)
    args = sane.scanimage_args(cfg, Path("/x"))
    assert args[:3] == ["scanimage", "-d", "epsonds:net:192.168.16.178"]
    assert "--adf-crp=no" in args and "--adf-skew=yes" in args
    assert args[args.index("--resolution") + 1] == "300"
    assert args[-1] == "--batch=/x/page_%04d.jpg"


def test_one_photo_mode_limits_batch_count():
    duplex = sane.scanimage_args(ScannerConfig(host="h"), Path("/x"), count="one")
    front_only = sane.scanimage_args(ScannerConfig(host="h", source="ADF Front"), Path("/x"), count="one")
    assert duplex[-1] == "--batch-count=2" and front_only[-1] == "--batch-count=1"
    assert not any(a.startswith("--batch-count") for a in sane.scanimage_args(ScannerConfig(host="h"), Path("/x")))


@pytest.mark.parametrize("first_side,front_page,back_page", [("back", 2, 1), ("front", 1, 2)])
def test_first_side_decides_which_page_is_the_front(tmp_path, first_side, front_page, back_page):
    staging = tmp_path / "staging"
    staging.mkdir()
    pages = []
    for n in (1, 2):
        page = staging / f"page_{n:04d}.jpg"
        page.write_text(f"page {n}")
        pages.append(page)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    sane.place_pages(pages, inbox, "run", duplex=True, first_side=first_side)
    assert (inbox / "run_0001.jpg").read_text() == f"page {front_page}"
    assert (inbox / "run_0001_b.jpg").read_text() == f"page {back_page}"


def test_page_count_survives_ingest_phase(tmp_path):
    import threading

    release = threading.Event()
    cfg = ScannerConfig(host="scanner", scanimage=fake_scanimage(tmp_path, 4))
    controller = sane.ScanController(cfg, tmp_path / "inbox")
    controller.start(on_complete=lambda: release.wait(5) and {"created": []})
    for _ in range(200):
        status = controller.status()
        if status.get("phase") == "ingesting":
            break
        time.sleep(0.02)
    assert status["phase"] == "ingesting" and status["pages"] == 4  # was reset to 0 before the fix
    release.set()
    assert wait(controller)["pages"] == 4


def test_duplex_scan_pairs_pages(tmp_path):
    inbox = tmp_path / "inbox"
    cfg = ScannerConfig(host="scanner", first_side="front", scanimage=fake_scanimage(tmp_path, 5))
    ingested = []
    controller = sane.ScanController(cfg, inbox)
    controller.start(on_complete=lambda: ingested.append(True) or {"created": [1, 2, 3]})
    status = wait(controller)

    assert status["state"] == "done", status
    assert status["pages"] == 5
    assert "odd page count" in status["message"]
    run = status["run_name"]
    assert sorted(status["files"]) == sorted([
        f"{run}_0001.jpg", f"{run}_0001_b.jpg", f"{run}_0002.jpg", f"{run}_0002_b.jpg", f"{run}_0003.jpg",
    ])
    assert ingested and status["ingest"] == {"created": [1, 2, 3]}
    assert not [p for p in inbox.iterdir() if p.name.startswith(".scanning")]  # staging removed


def test_front_only_scan(tmp_path):
    cfg = ScannerConfig(host="scanner", source="ADF Front", scanimage=fake_scanimage(tmp_path, 2))
    controller = sane.ScanController(cfg, tmp_path / "inbox")
    controller.start()
    status = wait(controller)
    assert sorted(status["files"]) == [f"{status['run_name']}_0001.jpg", f"{status['run_name']}_0002.jpg"]


def test_empty_feeder_fails_with_explanation(tmp_path):
    cfg = ScannerConfig(host="scanner", scanimage=fake_scanimage(tmp_path, 0))
    controller = sane.ScanController(cfg, tmp_path / "inbox")
    controller.start()
    status = wait(controller)
    assert status["state"] == "failed"
    assert "No photos in the feeder" in status["message"]


def test_one_scan_at_a_time(tmp_path):
    cfg = ScannerConfig(host="scanner", scanimage=fake_scanimage(tmp_path, 2))
    controller = sane.ScanController(cfg, tmp_path / "inbox")
    controller.start()
    if controller.status()["state"] == "scanning":
        with pytest.raises(RuntimeError):
            controller.start()
    wait(controller)


def test_scanned_files_match_ingest_pairing(tmp_path):
    from banana.config import DEFAULT_PAIRING_PATTERN
    from banana.ingest.pairing import pair_files

    cfg = ScannerConfig(host="scanner", scanimage=fake_scanimage(tmp_path, 4))
    inbox = tmp_path / "inbox"
    controller = sane.ScanController(cfg, inbox)
    controller.start()
    wait(controller)
    result = pair_files([p for p in inbox.iterdir() if p.is_file()], DEFAULT_PAIRING_PATTERN)
    assert len(result.groups) == 2 and all(g.back for g in result.groups) and not result.unmatched
