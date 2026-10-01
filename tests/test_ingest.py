"""Ingest robustness: one photo per transaction, never stranding files, files still being written, unreadable
files, reused scanner names, one ingest at a time, and the inbox watcher."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import select

from banana import db
from banana.config import Settings
from banana.ingest import service
from banana.ingest.runner import IngestRunner
from banana.models import Scan

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_demo_inbox import back, front  # noqa: E402


@pytest.fixture
def env(tmp_path):
    for name in ("inbox", "archive", "sorted", "data"):
        (tmp_path / name).mkdir()
    settings = Settings.model_validate({
        "paths": {n: str(tmp_path / n) for n in ("inbox", "archive", "sorted")} | {"data_dir": str(tmp_path / "data")},
        "analysis": {"read_text": False},
        "ingest": {"settle_seconds": 0, "watch_inbox": False},
    })
    engine = db.make_engine(settings.db_path)
    service._failures.clear()
    yield settings, engine
    engine.dispose()


def drop(inbox: Path, base: str, seed: int, with_back: bool = True) -> None:
    front(seed).save(inbox / f"{base}.jpg", quality=90)
    if with_back:
        back([f"photo {seed}"]).save(inbox / f"{base}_b.jpg", quality=90)


def scans(engine) -> list[Scan]:
    with db.session(engine) as s:
        return list(s.exec(select(Scan).order_by(Scan.id)))


def ingest(settings, engine, **kw):
    with db.session(engine) as s:
        return service.ingest_inbox(s, settings, **kw)


# ---------------------------------------------------------------- one photo per transaction


def test_other_writes_succeed_while_an_ingest_is_analysing(env, monkeypatch):
    """The 2026-09-30 failure: saves made during a scan's ingest waited out the 5 s busy timeout and failed."""
    settings, engine = env
    for n in range(1, 4):
        drop(settings.paths.inbox, f"Box_{n:04d}", n)
    real = service.analyze_scan
    writes = []

    def analyze_then_write_elsewhere(session, scan, s, **kw):
        result = real(session, scan, s, **kw)
        con = sqlite3.connect(settings.db_path, timeout=0.2)  # a second writer that gives up after 200 ms
        try:
            con.execute("UPDATE scan SET description = 'edited meanwhile' WHERE id = (SELECT min(id) FROM scan)")
            con.commit()
            writes.append("ok")
        finally:
            con.close()
        return result

    monkeypatch.setattr(service, "analyze_scan", analyze_then_write_elsewhere)
    report = ingest(settings, engine)
    assert len(report.created) == 3
    assert writes == ["ok", "ok", "ok"]  # never blocked by the ingest


def test_a_failed_commit_puts_that_photos_files_back_and_keeps_the_ones_before(env, monkeypatch):
    settings, engine = env
    drop(settings.paths.inbox, "Box_0001", 1)
    drop(settings.paths.inbox, "Box_0002", 2)
    from sqlmodel import Session

    real_commit = Session.commit
    calls = {"n": 0}

    def flaky_commit(self):
        if any(isinstance(o, Scan) and o.source_key == "Box_0002" for o in self.new):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)
    with pytest.raises(sqlite3.OperationalError):
        ingest(settings, engine)
    monkeypatch.setattr(Session, "commit", real_commit)

    assert [s.source_key for s in scans(engine)] == ["Box_0001"]
    assert sorted(p.name for p in settings.paths.inbox.iterdir()) == ["Box_0002.jpg", "Box_0002_b.jpg"]
    archived = sorted(p.name for p in settings.paths.archive.rglob("*.jpg"))
    assert archived == ["Box_0001.jpg", "Box_0001_b.jpg"]  # nothing in the archive without a row
    with db.session(engine) as s:
        assert service.archive_orphans(s, settings) == []
    assert len(ingest(settings, engine).created) == 1  # and the next ingest picks it up


# ---------------------------------------------------------------- files still being written / unreadable


def test_files_still_being_written_wait_for_the_next_pass(env):
    settings, engine = env
    settings.ingest.settle_seconds = 10
    drop(settings.paths.inbox, "Box_0001", 1)
    report = ingest(settings, engine)
    assert report.created == [] and report.not_ready == ["Box_0001.jpg", "Box_0001_b.jpg"]

    old = time.time() - 60
    for p in settings.paths.inbox.iterdir():
        os.utime(p, (old, old))
    assert len(ingest(settings, engine).created) == 1


def test_files_the_app_placed_itself_skip_the_wait(env):
    settings, engine = env
    settings.ingest.settle_seconds = 10
    drop(settings.paths.inbox, "scan20260930_0001", 1)
    report = ingest(settings, engine, trusted=["scan20260930_0001.jpg", "scan20260930_0001_b.jpg"])
    assert len(report.created) == 1


def test_an_unreadable_photo_is_contained_retried_then_moved_aside(env):
    settings, engine = env
    drop(settings.paths.inbox, "Box_0001", 1)
    (settings.paths.inbox / "Box_0002.jpg").write_bytes(b"\xff\xd8 not really a jpeg")

    first = ingest(settings, engine)
    assert len(first.created) == 1  # the good photo is not held up by the bad one
    assert [f["name"] for f in first.failed] == ["Box_0002"]
    assert (settings.paths.inbox / "Box_0002.jpg").exists()

    assert [f["name"] for f in ingest(settings, engine).failed] == ["Box_0002"]
    third = ingest(settings, engine)
    assert third.unreadable == ["Box_0002"]
    assert (settings.paths.inbox / service.UNREADABLE_DIR / "Box_0002.jpg").exists()
    assert not (settings.paths.inbox / "Box_0002.jpg").exists()


# ---------------------------------------------------------------- reused scanner names


def test_same_name_same_photo_is_set_aside_but_a_reused_name_is_a_new_photo(env):
    settings, engine = env
    drop(settings.paths.inbox, "FastFoto_0001", 1)
    assert len(ingest(settings, engine).created) == 1

    drop(settings.paths.inbox, "FastFoto_0001", 1)  # the very same file dropped again
    again = ingest(settings, engine)
    assert again.created == [] and again.skipped == ["FastFoto_0001"]
    assert (settings.paths.inbox / service.ALREADY_INGESTED_DIR / "FastFoto_0001.jpg").exists()

    drop(settings.paths.inbox, "FastFoto_0001", 99)  # scanner restarted its numbering: a different photo
    new = ingest(settings, engine)
    assert len(new.created) == 1
    keys = [s.source_key for s in scans(engine)]
    assert keys[0] == "FastFoto_0001" and keys[1].startswith("FastFoto_0001~")
    assert all(s.source_sha256 for s in scans(engine))


def test_a_scan_from_before_hashes_gets_its_hash_filled_in(env):
    settings, engine = env
    drop(settings.paths.inbox, "Box_0001", 1)
    ingest(settings, engine)
    with db.session(engine) as s:
        row = s.exec(select(Scan)).one()
        row.source_sha256 = None  # as stored by an older version
        s.add(row)
        s.commit()
    drop(settings.paths.inbox, "Box_0001", 1)
    assert ingest(settings, engine).skipped == ["Box_0001"]
    assert scans(engine)[0].source_sha256


def test_name_matching_ignores_case_like_pairing_does(env):
    settings, engine = env
    drop(settings.paths.inbox, "Box_0001", 1)
    ingest(settings, engine)
    front(1).save(settings.paths.inbox / "BOX_0001.jpg", quality=90)
    back(["photo 1"]).save(settings.paths.inbox / "BOX_0001_b.jpg", quality=90)
    assert ingest(settings, engine).skipped == ["BOX_0001"]


# ---------------------------------------------------------------- runner: one at a time, watcher, orphans


def test_concurrent_ingests_queue_instead_of_colliding(env):
    settings, engine = env
    for n in range(1, 6):
        drop(settings.paths.inbox, f"Box_{n:04d}", n)
    runner = IngestRunner(engine, settings)
    results, errors = [], []

    def go():
        try:
            results.append(runner.run(reason="test"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(len(r["created"]) for r in results) == 5
    assert len(scans(engine)) == 5


def test_the_watcher_picks_up_dropped_files_on_its_own(env):
    settings, engine = env
    settings.ingest.watch_inbox, settings.ingest.watch_seconds = True, 0.1
    runner = IngestRunner(engine, settings)
    runner.start_watching()
    try:
        assert runner.status()["watching"]
        drop(settings.paths.inbox, "Box_0001", 1)
        for _ in range(100):
            if scans(engine):
                break
            time.sleep(0.05)
        assert len(scans(engine)) == 1
        for _ in range(40):  # the status is updated just after the commit
            if runner.status()["last_reason"] == "watcher":
                break
            time.sleep(0.05)
        assert runner.status()["last_reason"] == "watcher"
    finally:
        runner.stop_watching()


def test_retry_unreadable_puts_files_back(env):
    settings, engine = env
    folder = settings.paths.inbox / service.UNREADABLE_DIR
    folder.mkdir()
    front(3).save(folder / "Box_0003.jpg", quality=90)
    runner = IngestRunner(engine, settings)
    assert runner.status()["unreadable"] == ["Box_0003.jpg"]
    assert runner.retry_unreadable() == 1
    assert (settings.paths.inbox / "Box_0003.jpg").exists() and runner.status()["unreadable"] == []


def test_archived_files_with_no_scan_are_reported_not_touched(env):
    settings, engine = env
    drop(settings.paths.inbox, "Box_0001", 1)
    ingest(settings, engine)
    stray = settings.paths.archive / "inbox-20260930-133251" / "scan20260930_0007.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    runner = IngestRunner(engine, settings)
    assert runner.check_orphans() == [str(Path("inbox-20260930-133251") / "scan20260930_0007.jpg")]
    assert runner.status()["orphans"] == 1 and stray.exists()
