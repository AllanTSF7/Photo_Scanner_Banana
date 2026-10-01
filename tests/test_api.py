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
        "[analysis]\nread_text = false\n"  # OCR has its own tests; keeps these fast
        "[ingest]\nsettle_seconds = 0\nwatch_inbox = false\n"  # demo files are brand new; no background ingests
        '[auth]\nallowed_hosts = ["testserver"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BANANA_CONFIG", str(config))
    make_demo_inbox.main(tmp_path / "inbox")
    import banana.web.api as api
    from banana import auth, db

    api = importlib.reload(api)
    with db.session(api.engine) as session:
        auth.create_user(session, "tester", "correct horse battery")
    with TestClient(api.app) as c:
        assert c.post("/api/auth/login", json={"username": "tester", "password": "correct horse battery"}).status_code == 200
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
    assert c.get("/api/scanner").json() == {"configured": False, "stranded": []}

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


def test_deleting_a_duplicate_anchor_reassigns_its_follower(client):
    c, _ = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    anchor, follower = scans["Attic3_0001"]["id"], scans["Attic3_0007"]["id"]
    assert c.get(f"/api/scans/{follower}").json()["duplicate_group_id"] == anchor

    c.patch(f"/api/scans/{anchor}", json={"status": "rejected"})
    assert c.delete(f"/api/scans/{anchor}").status_code == 200
    # Never a dangling "possible rescan of #<deleted id>" - the sole survivor just isn't a duplicate of anything.
    assert c.get(f"/api/scans/{follower}").json()["duplicate_group_id"] is None


def test_deleting_a_duplicate_anchor_promotes_a_new_one_for_the_rest(client):
    import banana.web.api as api
    from banana.models import Scan

    c, _ = client
    c.post("/api/ingest")
    scans = c.get("/api/scans").json()
    a, b, c_scan = (s["id"] for s in scans[:3])
    with api.db.session(api.engine) as session:  # simulate a 3-way group: b and c_scan both point at a
        for sid in (b, c_scan):
            row = session.get(Scan, sid)
            row.duplicate_group_id = a
            session.add(row)
        session.commit()
    c.patch(f"/api/scans/{a}", json={"status": "rejected"})
    assert c.delete(f"/api/scans/{a}").status_code == 200

    new_anchor, remaining_follower = sorted((b, c_scan))
    assert c.get(f"/api/scans/{new_anchor}").json()["duplicate_group_id"] is None
    assert c.get(f"/api/scans/{remaining_follower}").json()["duplicate_group_id"] == new_anchor


def test_repair_dangling_duplicate_groups(client):
    """Data that went dangling before this repair existed (a scan deleted without reassigning its followers)."""
    import banana.web.api as api
    from banana.ingest.service import repair_dangling_duplicate_groups
    from banana.models import Scan

    c, _ = client
    c.post("/api/ingest")
    scans = {s["source_key"]: s for s in c.get("/api/scans").json()}
    anchor, follower = scans["Attic3_0001"]["id"], scans["Attic3_0007"]["id"]

    with api.db.session(api.engine) as session:
        session.delete(session.get(Scan, anchor))  # bypass the guarded endpoint, as old data would have
        session.commit()

    assert c.get(f"/api/scans/{follower}").json()["duplicate_group_id"] == anchor  # dangling, as reported

    with api.db.session(api.engine) as session:
        changed = repair_dangling_duplicate_groups(session)
        session.commit()
    assert changed == 1
    assert c.get(f"/api/scans/{follower}").json()["duplicate_group_id"] is None

    with api.db.session(api.engine) as session:  # idempotent
        assert repair_dangling_duplicate_groups(session) == 0


def _anonymous():
    return TestClient(sys.modules["banana.web.api"].app)


def test_everything_but_the_public_paths_needs_a_signed_in_session(client):
    c, _ = client
    with _anonymous() as anon:
        for path in ("/api/scans", "/api/summary", "/api/immich/settings", "/docs", "/openapi.json"):
            assert anon.get(path).status_code == 401, path
        home = anon.get("/", follow_redirects=False)
        assert home.status_code == 303 and home.headers["location"] == "/login"
        assert anon.post("/api/export").status_code == 401
        assert anon.post("/api/ingest").status_code == 401
        assert anon.delete("/api/scans/1").status_code == 401
        assert anon.patch("/api/immich/settings", json={"url": "http://evil.example"}).status_code == 401
        for path in ("/health", "/login", "/static/app.css", "/static/login.js", "/api/auth/state"):
            assert anon.get(path).status_code == 200, path
    assert c.get("/api/scans").status_code == 200
    assert c.get("/api/auth/me").json() == {"username": "tester"}


def test_wrong_password_is_refused_and_repeated_failures_are_throttled(client):
    with _anonymous() as anon:
        assert anon.post("/api/auth/login", json={"username": "tester", "password": "nope-nope"}).status_code == 401
        assert anon.post("/api/auth/login", json={"username": "nobody", "password": "nope-nope"}).status_code == 401
        for _ in range(8):
            anon.post("/api/auth/login", json={"username": "tester", "password": "nope-nope"})
        right = anon.post("/api/auth/login", json={"username": "tester", "password": "correct horse battery"})
        assert right.status_code == 429  # even the right password waits out the lockout


def test_sign_out_ends_the_session(client):
    c, _ = client
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/scans").status_code == 401


def test_disabling_a_user_signs_them_out_and_blocks_sign_in(client):
    from banana import auth, db

    c, _ = client
    api = sys.modules["banana.web.api"]
    with db.session(api.engine) as session:
        auth.set_disabled(session, "tester", True)
    assert c.get("/api/scans").status_code == 401
    assert c.post("/api/auth/login", json={"username": "tester", "password": "correct horse battery"}).status_code == 401


def test_unknown_host_is_refused_to_block_dns_rebinding(client):
    c, _ = client
    assert c.get("/api/scans", headers={"Host": "evil.example:8420"}).status_code == 400
    assert c.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_cross_site_writes_are_refused_even_when_signed_in(client):
    c, _ = client
    assert c.post("/api/ingest", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/auth/logout", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/ingest", headers={"Origin": "http://testserver"}).status_code == 200  # same site


def test_session_cookie_is_httponly_strict_and_only_its_hash_is_stored(client):
    from sqlmodel import select

    from banana import db
    from banana.models import AuthSession

    with _anonymous() as anon:
        response = anon.post("/api/auth/login", json={"username": "tester", "password": "correct horse battery"})
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        token = response.cookies["ps_session"]
    api = sys.modules["banana.web.api"]
    with db.session(api.engine) as session:
        stored = [row.token_hash for row in session.exec(select(AuthSession))]
    assert token not in stored and all(len(h) == 64 for h in stored)


def test_first_account_setup_is_closed_by_default(client):
    with _anonymous() as anon:
        assert anon.get("/api/auth/state").json() == {"setup_open": False, "username": None}
        assert anon.post("/api/auth/setup", json={"username": "mallory", "password": "long enough pw"}).status_code == 403


def test_first_account_setup_works_once_when_enabled(tmp_path, monkeypatch):
    for name in ("inbox", "archive", "sorted", "data"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\n" + "".join(f'{n} = "{(tmp_path / n).as_posix()}"\n' for n in ("inbox", "archive", "sorted"))
        + f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        + '[auth]\nallowed_hosts = ["testserver"]\nsetup_page = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BANANA_CONFIG", str(config))
    import banana.web.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as first:
        assert first.get("/api/auth/state").json()["setup_open"] is True
        assert first.post("/api/auth/setup", json={"username": "Allan", "password": "short"}).status_code == 422
        assert first.post("/api/auth/setup", json={"username": "Allan", "password": "long enough pw"}).status_code == 200
        assert first.get("/api/auth/me").json() == {"username": "allan"}  # usernames are lower-cased
    with TestClient(api.app) as second:
        assert second.get("/api/auth/state").json()["setup_open"] is False
        assert second.post("/api/auth/setup", json={"username": "mallory", "password": "long enough pw"}).status_code == 403


def test_passwords_are_hashed_and_verified():
    from banana import auth

    stored = auth.hash_password("correct horse battery")
    assert "correct horse" not in stored and stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("correct horse batterY", stored)
    assert not auth.verify_password("anything", "not-a-hash")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_dev_reload_token_disabled_without_env(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("BANANA_DEV_RELOAD", raising=False)
    assert c.get("/api/dev/reload-token").json() == {"enabled": False, "token": None}
    monkeypatch.setenv("BANANA_DEV_RELOAD", "1")
    data = c.get("/api/dev/reload-token").json()
    assert data["enabled"] is True and data["token"]


def test_windows_download_is_404_until_a_build_is_published_then_serves_it(client):
    c, root = client
    assert c.get("/api/downloads/windows").status_code == 404

    downloads = root / "data" / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "PhotoScanner_win64.zip").write_bytes(b"PK\x03\x04fake-zip-bytes")

    response = c.get("/api/downloads/windows")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04fake-zip-bytes"
    assert response.headers["content-type"] == "application/zip"


def test_component_health(client):
    c, _ = client
    data = c.get("/api/health").json()
    by_name = {check["name"]: check for check in data["checks"]}
    assert by_name["api"]["status"] == "ok"
    assert by_name["database"]["status"] == "ok"
    assert by_name["inbox"]["status"] == "ok"
    assert by_name["scanner"]["status"] == "off"  # not configured in this fixture
    assert by_name["immich"]["status"] == "off"  # not configured, and never contacted (immich.enabled defaults false)
    assert data["overall"] in ("ok", "warn", "fail")


def test_immich_settings_never_echo_the_raw_api_key(client):
    c, _ = client
    assert c.get("/api/immich/settings").json() == {
        "enabled": False, "url": "", "library_id": "", "import_path_prefix": "/mnt/photo_vault/sorted",
        "api_key_set": False, "api_key_last4": None,
    }

    saved = c.patch("/api/immich/settings", json={
        "enabled": True, "url": "http://immich.example", "library_id": "lib-1", "api_key": "supersecretkey1234",
    }).json()
    assert saved["api_key_set"] is True and saved["api_key_last4"] == "1234"
    assert "api_key" not in saved and "supersecretkey1234" not in str(saved)

    # Re-fetching, and updating an unrelated field, both still never surface the raw key.
    for response in (c.get("/api/immich/settings"), c.patch("/api/immich/settings", json={"enabled": False})):
        body = response.json()
        assert body["api_key_set"] is True and body["api_key_last4"] == "1234"
        assert "supersecretkey1234" not in str(body)


def test_immich_status_makes_no_network_call_when_disabled(client):
    c, _ = client
    # Even with a URL/key saved, disabled means no attempt is made - error stays None, not a connection failure.
    c.patch("/api/immich/settings", json={"url": "http://127.0.0.1:1", "api_key": "k"})  # port 1: nothing listens
    status = c.get("/api/immich/status").json()
    assert status == {"enabled": False, "connected": False, "asset_count": None, "error": None}


def test_immich_check_refused_when_disabled(client):
    c, _ = client
    response = c.post("/api/immich/check")
    assert response.status_code == 409
    assert "enabled" in response.json()["detail"]


def test_operator_immich_duplicate_flag_round_trips(client):
    c, _ = client
    c.post("/api/ingest")
    sid = c.get("/api/scans").json()[0]["id"]
    assert c.get(f"/api/scans/{sid}").json()["operator_immich_duplicate"] is False
    updated = c.patch(f"/api/scans/{sid}", json={"operator_immich_duplicate": True}).json()
    assert updated["operator_immich_duplicate"] is True
    assert updated["immich_duplicate_asset_id"] is None


def test_immich_asset_image_is_served_only_for_matched_assets_and_read_only(client):
    from banana import db
    from banana.models import Scan
    from tests.immich._fake_server import FakeImmichServer

    asset = "6728050d-9acf-48cd-8ff9-6d86ecd10a1e"
    c, _ = client
    c.post("/api/ingest")
    sid = c.get("/api/scans").json()[0]["id"]
    server = FakeImmichServer()
    server.thumbnails = {asset: b"\xff\xd8fake-jpeg-bytes"}
    try:
        c.patch("/api/immich/settings", json={"enabled": True, "url": server.url, "api_key": "k"})
        assert c.get(f"/api/immich/asset/{asset}/image").status_code == 404  # no scan matched it: not served
        assert c.get("/api/immich/asset/not-a-uuid/image").status_code == 404

        api = sys.modules["banana.web.api"]
        with db.session(api.engine) as session:
            scan = session.get(Scan, sid)
            scan.immich_duplicate_asset_id = asset
            session.add(scan)
            session.commit()
        response = c.get(f"/api/immich/asset/{asset}/image")
        assert response.status_code == 200 and response.content == b"\xff\xd8fake-jpeg-bytes"
        assert response.headers["content-type"] == "image/jpeg"
        assert all(request[0] == "GET" for request in server.requests)  # read-only, always
    finally:
        server.close()


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


def test_stranded_scan_runs_are_reported_and_recovered_into_review(client):
    from PIL import Image

    c, root = client
    c.post("/api/ingest")  # clear the demo inbox first
    folder = root / "inbox" / ".scanning-scan20260930132056"
    folder.mkdir()
    for n in (1, 2, 3):
        Image.new("RGB", (400, 300), (40 * n, 90, 60)).save(folder / f"page_{n:04d}.jpg", "JPEG")
    Image.new("RGB", (400, 300), (200, 90, 60)).save(folder / "page_0004.bmp", "BMP")

    assert c.get("/api/scanner").json()["stranded"] == [{"run": "scan20260930132056", "files": 4}]

    r = c.post("/api/scanner/recover").json()
    assert r["recovered"] == [{"run": "scan20260930132056", "files": 4}]
    assert len(r["ingest"]["created"]) == 2  # two photos, front + back each
    assert not folder.exists()
    assert c.get("/api/scanner").json()["stranded"] == []


def test_delete_keeps_the_originals_when_the_record_cannot_be_removed(client, monkeypatch):
    """Files used to be unlinked before the commit; a busy database then left a row pointing at deleted originals."""
    import sqlite3

    from sqlmodel import Session

    c, _ = client
    c.post("/api/ingest")
    scan = next(s for s in c.get("/api/scans").json() if s["source_key"] == "Attic3_0002")
    c.patch(f"/api/scans/{scan['id']}", json={"status": "rejected"})

    def busy(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Session, "commit", busy)
    with pytest.raises(sqlite3.OperationalError):
        c.delete(f"/api/scans/{scan['id']}")
    monkeypatch.undo()

    assert Path(scan["front_path"]).exists() and Path(scan["back_path"]).exists()
    assert c.get(f"/api/scans/{scan['id']}").status_code == 200


def test_a_save_based_on_an_old_version_is_refused_with_the_current_scan(client):
    c, _ = client
    c.post("/api/ingest")
    scan = c.get("/api/scans").json()[0]
    first = c.patch(f"/api/scans/{scan['id']}", json={"description": "tab A", "version": scan["version"]})
    assert first.status_code == 200 and first.json()["version"] == scan["version"] + 1

    stale = c.patch(f"/api/scans/{scan['id']}", json={"description": "tab B", "version": scan["version"]})
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "conflict" and detail["scan"]["description"] == "tab A"

    retry = c.patch(f"/api/scans/{scan['id']}", json={"description": "tab B", "version": detail["scan"]["version"]})
    assert retry.status_code == 200 and retry.json()["description"] == "tab B"  # "keep mine", knowingly


def test_a_partial_save_leaves_other_fields_alone(client):
    """Two reviewers editing different fields of one scan no longer wipe each other's work."""
    c, _ = client
    c.post("/api/ingest")
    scan = c.get("/api/scans").json()[0]
    sid = scan["id"]
    a = c.patch(f"/api/scans/{sid}", json={"people": ["Grandma"], "version": scan["version"]}).json()
    b = c.patch(f"/api/scans/{sid}", json={"description": "Christmas"}).json()  # sent only what it changed
    assert b["people"] == ["Grandma"] and b["description"] == "Christmas"
    assert b["version"] == a["version"] + 1


def test_read_text_finishing_after_a_save_does_not_overwrite_it(client, monkeypatch):
    """Read text runs for seconds; a description typed and saved meanwhile used to be overwritten by it."""
    import banana.web.api as api
    from banana import db
    from banana.models import Scan

    c, _ = client
    c.post("/api/ingest")
    sid = next(s for s in c.get("/api/scans").json() if s["has_back"])["id"]
    calls = []

    def slow_ocr(scan, settings, known=None, learned=None):
        calls.append(scan.description)
        if len(calls) == 1:  # the operator saves while the first OCR pass is still running
            with db.session(api.engine) as other:
                row = other.get(Scan, sid)
                row.description = "typed by the operator"
                other.add(row)
                other.commit()
        if not (scan.description or "").strip():
            scan.description = "from OCR"
        scan.ocr_lines = [{"text": "Xmas '84"}]
        return []

    monkeypatch.setattr(api, "read_back_text", slow_ocr)
    result = c.post(f"/api/scans/{sid}/read-text").json()
    assert result["description"] == "typed by the operator"
    assert result["ocr_lines"] == [{"text": "Xmas '84"}]
    assert calls == [None, "typed by the operator"]  # re-ran on the fresh row instead of committing the stale one


def test_entity_extraction_never_writes_and_the_save_records_the_suggestion(client):
    c, _ = client
    c.post("/api/ingest")
    scan = c.get("/api/scans").json()[0]
    found = c.post("/api/entities/extract", json={"text": "Grandma at Uncle Bob's birthday", "scan_id": scan["id"]}).json()
    assert "Grandma" in found["people"]
    after = c.get(f"/api/scans/{scan['id']}").json()
    assert after["version"] == scan["version"] and "people" not in after["suggestions"]  # nothing written

    saved = c.patch(f"/api/scans/{scan['id']}", json={"description": "Grandma at Uncle Bob's birthday"}).json()
    assert "Grandma" in saved["suggestions"]["people"]["value"]  # recorded by the save, for the learning loop
    assert saved["people"] == []  # and never filled in as a value
