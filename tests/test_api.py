"""API flow: ingest inbox -> review/edit -> approve -> export."""

import importlib
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import make_demo_inbox  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("inbox", "archive", "sorted", "data"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n" + "".join(f'{n} = "{(tmp_path / n).as_posix()}"\n' for n in ("inbox", "archive", "sorted")) +
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        "[analysis]\nread_text = false\n",  # OCR has its own tests; keeps these fast
        encoding="utf-8",
    )
    monkeypatch.setenv("BANANA_CONFIG", str(config))
    make_demo_inbox.main(tmp_path / "inbox")
    import banana.web.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as c:
        yield c, tmp_path


def test_ingest_review_flow(client):
    c, root = client
    report = c.post("/api/ingest").json()
    assert len(report["created"]) == 7
    assert 7 in report["possible_duplicates"]  # resized rescan of #1
    assert not any((root / "inbox").iterdir())  # moved to archive

    scans = {s["source_key"]: s for s in c.get("/api/scans?status=needs_review").json()}
    assert scans["Attic3_0001"]["back_type"] == "content" and scans["Attic3_0001"]["keep_back"]
    assert scans["Attic3_0003"]["back_type"] == "blank" and not scans["Attic3_0003"]["keep_back"]
    assert scans["Attic3_0004"]["has_back"] is False

    assert c.post("/api/ingest").json()["created"] == []  # idempotent

    sid = scans["Attic3_0001"]["id"]
    assert c.get(f"/api/scans/{sid}/image/front?size=200").headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/scans/{scans['Attic3_0004']['id']}/image/back").status_code == 404

    preview = c.get("/api/dates/parse", params={"text": "Xmas '84"}).json()
    assert preview[0]["label"] == "1984-12-25"

    bad = c.patch(f"/api/scans/{sid}", json={"date_text": "sometime"})
    assert bad.status_code == 422

    updated = c.patch(
        f"/api/scans/{sid}",
        json={"date_text": "Xmas '84", "description": "Christmas at Grandma's", "people": ["Grandma", "John"],
              "status": "approved"},
    ).json()
    assert updated["date"]["exif"] == "1984:12:25 12:00:00"
    assert updated["status"] == "approved"
    assert c.get("/api/summary").json()["counts"]["approved"] == 1


def test_scanner_status(client, monkeypatch):
    import socket

    import banana.web.api as api

    c, _ = client
    assert c.get("/api/scanner").json() == {"configured": False}

    with socket.socket() as listener:  # stand-in scanner on a free local port
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(api.settings.scanner, "host", "127.0.0.1")
        monkeypatch.setattr(api.settings.scanner, "port", port)
        status = c.get("/api/scanner").json()
        assert status["configured"] and status["online"] and status["port"] == port
        assert status["max_feeder_count"] == api.settings.scanner.max_feeder_count
        assert status["device"] == "epsonds:net:127.0.0.1" and status["scan"]["state"] == "idle"
    assert c.get("/api/scanner").json()["online"] is False


def test_rotate_swap_and_reanalyze(client):
    from io import BytesIO

    from PIL import Image

    c, _ = client
    c.post("/api/ingest")
    scan = next(s for s in c.get("/api/scans").json() if s["source_key"] == "Attic3_0001")
    sid = scan["id"]

    def preview_size(side):
        return Image.open(BytesIO(c.get(f"/api/scans/{sid}/image/{side}?size=300").content)).size

    w, h = preview_size("front")
    assert w > h
    assert c.patch(f"/api/scans/{sid}", json={"front_rotation": 90}).json()["front_rotation"] == 90
    assert preview_size("front") == (h, w)
    assert c.patch(f"/api/scans/{sid}", json={"front_rotation": 45}).status_code == 422

    swapped = c.post(f"/api/scans/{sid}/swap-sides").json()
    assert swapped["front_path"] == scan["back_path"] and swapped["back_path"] == scan["front_path"]
    assert swapped["back_rotation"] == 90 and swapped["front_rotation"] == 0  # rotations travel with the image

    again = c.post(f"/api/scans/{sid}/reanalyze").json()
    assert again["back_rotation"] == 90  # reanalyze keeps manual rotations
    no_back = next(s for s in c.get("/api/scans").json() if s["source_key"] == "Attic3_0004")
    assert c.post(f"/api/scans/{no_back['id']}/swap-sides").status_code == 409


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_export_applies_crop_and_rotation(client):
    from PIL import Image

    c, root = client
    c.post("/api/ingest")
    scan = next(s for s in c.get("/api/scans").json() if s["source_key"] == "Attic3_0004")
    import banana.web.api as api
    from banana.models import Scan

    with api.db.session(api.engine) as session:  # simulate a detected crop on this synthetic scan
        row = session.get(Scan, scan["id"])
        row.front_crop = [100, 50, 900, 650]
        session.add(row)
        session.commit()
    c.patch(f"/api/scans/{scan['id']}", json={"front_rotation": 90, "date_text": "1990", "status": "approved"})
    exported = c.post("/api/export").json()["exported"][0]
    assert exported["front"].endswith("_A.jpg")
    with Image.open(root / "sorted" / exported["front"]) as out:
        assert out.size == (600, 800)  # 800x600 crop, rotated a quarter turn


def test_read_text_endpoint(client, monkeypatch):
    from banana.analysis import ocr

    c, _ = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    sid = scans["Attic3_0001"]["id"]

    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    assert c.post(f"/api/scans/{sid}/read-text").status_code == 503

    class Reader:
        name = "fake"

        def read(self, image, use_cls: bool = True):
            return [ocr.TextLine("Xmas '84", 0.97, [[0, 0]] * 4), ocr.TextLine("Grandma & John", 0.95, [[0, 0]] * 4)]

    monkeypatch.setattr(ocr, "get_reader", lambda: Reader())
    data = c.post(f"/api/scans/{sid}/read-text").json()
    assert [line["text"] for line in data["ocr_lines"]] == ["Xmas '84", "Grandma & John"]
    assert data["date"]["label"] == "1984-12-25" and data["date_source"] == "back_ocr"
    assert data["description"] == "Xmas '84\nGrandma & John"
    assert c.post(f"/api/scans/{scans['Attic3_0004']['id']}/read-text").status_code == 409  # no back


def test_entities_endpoint_uses_known_names(client):
    c, _ = client
    c.post("/api/ingest")
    scans = c.get("/api/scans").json()
    c.patch(f"/api/scans/{scans[0]['id']}", json={"people": ["Tommy"], "places": ["Old Mill"]})
    data = c.post("/api/entities/extract", json={"text": "Tommy at the Old Mill, 5th birthday", "scan_id": scans[1]["id"]}).json()
    assert "Tommy" in data["people"] and "Old Mill" in data["places"] and "5th Birthday" in data["events"]
    assert data["engine"]
    own = c.post("/api/entities/extract", json={"text": "Tommy", "scan_id": scans[0]["id"]}).json()
    assert "Tommy" not in own["people"] or own["engine"].startswith("spacy")  # own names aren't a vocabulary source


def test_autocorrect_endpoint_fixes_typos_and_protects_known_names(client):
    c, _ = client
    c.post("/api/ingest")
    scans = c.get("/api/scans").json()

    fixed = c.post("/api/autocorrect", json={"text": "Januaru '84"}).json()
    assert fixed["text"] == "January '84"
    assert fixed["fixes"] == [{"from": "Januaru", "to": "January", "source": "spelling"}]
    assert fixed["producer"] == "autocorrect@1"

    c.patch(f"/api/scans/{scans[0]['id']}", json={"people": ["Januaru"]})  # an odd name, but a confirmed one

    protected = c.post("/api/autocorrect", json={"text": "Januaru said hi", "scan_id": scans[1]["id"]}).json()
    assert protected["text"] == "Januaru said hi" and protected["fixes"] == []  # confirmed name elsewhere: left alone

    own_scan = c.post("/api/autocorrect", json={"text": "Januaru said hi", "scan_id": scans[0]["id"]}).json()
    assert own_scan["text"] == "January said hi"  # a scan's own current value isn't a vocabulary source for itself

    clean = c.post("/api/autocorrect", json={"text": "Christmas 1984"}).json()
    assert clean["text"] == "Christmas 1984" and clean["fixes"] == []


def test_delete_scan_only_allowed_for_rejected_with_no_history(client):
    c, root = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}

    needs_review = scans["Attic3_0002"]["id"]
    assert c.delete(f"/api/scans/{needs_review}").status_code == 409  # not rejected yet

    c.patch(f"/api/scans/{needs_review}", json={"status": "rejected"})
    front_path = Path(scans["Attic3_0002"]["front_path"])
    back_path = Path(scans["Attic3_0002"]["back_path"])
    assert front_path.exists() and back_path.exists()

    deleted = c.delete(f"/api/scans/{needs_review}").json()
    assert sorted(deleted["files_removed"]) == sorted([front_path.name, back_path.name])
    assert not front_path.exists() and not back_path.exists()
    assert c.get(f"/api/scans/{needs_review}").status_code == 404


def test_delete_scan_refuses_when_it_has_training_history(client):
    c, _ = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    sid = scans["Attic3_0002"]["id"]

    c.patch(f"/api/scans/{sid}", json={"date_text": "1990", "status": "approved"})  # writes correction events
    c.patch(f"/api/scans/{sid}", json={"status": "rejected"})  # later rejected - events still exist
    response = c.delete(f"/api/scans/{sid}")
    assert response.status_code == 409
    assert "training" in response.json()["detail"]

    front_path = Path(scans["Attic3_0002"]["front_path"])
    assert front_path.exists()  # refused: nothing touched


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_delete_scan_refuses_when_already_exported(client):
    c, _ = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    sid = scans["Attic3_0001"]["id"]

    c.patch(f"/api/scans/{sid}", json={"date_text": "1990", "status": "approved"})
    c.post("/api/export")
    c.patch(f"/api/scans/{sid}", json={"status": "rejected"})
    response = c.delete(f"/api/scans/{sid}")
    assert response.status_code == 409
    assert "exported" in response.json()["detail"]


def test_demo_password_protects_everything(client, monkeypatch):
    import base64
    import importlib

    import banana.web.api as api
    from fastapi.testclient import TestClient

    c, _ = client
    assert c.get("/health").status_code == 200  # no password configured: open, as before

    monkeypatch.setenv("BANANA_DEMO_PASSWORD", "correct horse")
    protected = importlib.reload(api)
    try:
        with TestClient(protected.app) as demo:
            for path in ("/", "/health", "/api/scans", "/static/app.css", "/docs"):
                response = demo.get(path)
                assert response.status_code == 401, path
                assert response.headers["www-authenticate"].startswith("Basic")

            def auth(password):
                return {"Authorization": "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()}

            assert demo.get("/api/scans", headers=auth("wrong")).status_code == 401
            assert demo.get("/api/scans", headers={"Authorization": "Basic !!notbase64"}).status_code == 401
            assert demo.get("/api/scans", headers=auth("correct horse")).status_code == 200
            assert demo.get("/", headers=auth("correct horse")).status_code == 200
    finally:
        monkeypatch.delenv("BANANA_DEMO_PASSWORD")
        importlib.reload(api)


def test_dev_reload_token_disabled_without_env(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("BANANA_DEV_RELOAD", raising=False)
    assert c.get("/api/dev/reload-token").json() == {"enabled": False, "token": None}
    monkeypatch.setenv("BANANA_DEV_RELOAD", "1")
    data = c.get("/api/dev/reload-token").json()
    assert data["enabled"] is True and data["token"]


def test_component_health(client):
    c, _ = client
    data = c.get("/api/health").json()
    by_name = {check["name"]: check for check in data["checks"]}
    assert by_name["api"]["status"] == "ok"
    assert by_name["database"]["status"] == "ok"
    assert by_name["inbox"]["status"] == "ok"
    assert by_name["scanner"]["status"] == "off"  # not configured in this fixture
    assert data["overall"] in ("ok", "warn", "fail")


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_export_approved(client):
    c, root = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    c.patch(f"/api/scans/{scans['Attic3_0001']['id']}", json={"date_text": "Xmas '84", "status": "approved"})
    c.patch(f"/api/scans/{scans['Attic3_0003']['id']}", json={"date_text": "1979", "status": "approved"})

    result = c.post("/api/export").json()
    assert result["errors"] == []
    by_id = {e["id"]: e for e in result["exported"]}
    first = by_id[scans["Attic3_0001"]["id"]]
    assert first["front"].startswith("1984/1984-12-25/") and first["back"]
    assert by_id[scans["Attic3_0003"]["id"]]["back"] is None  # blank back not exported
    assert (root / "sorted" / f"{first['front']}.xmp").exists()
    assert c.get(f"/api/scans/{scans['Attic3_0001']['id']}").json()["export"]["front_rel"] == first["front"]
