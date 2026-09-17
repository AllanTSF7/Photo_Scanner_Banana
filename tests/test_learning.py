"""Learning loop Stages 1-2: correction events, append-only storage, training filter, dictionary, proposals."""

import sqlite3

import pytest
from PIL import Image, ImageDraw
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import select

from banana import db
from banana.config import Settings
from banana.core import corrections, dictionary, proposals
from banana.models import Batch, CorrectionEvent, Scan, ScanStatus


@pytest.fixture
def env(tmp_path):
    settings = Settings.model_validate({"paths": {"data_dir": str(tmp_path / "data")}})
    engine = db.make_engine(settings.db_path)
    back = tmp_path / "back.jpg"
    image = Image.new("RGB", (1200, 800), (250, 246, 236))
    ImageDraw.Draw(image).text((100, 100), "JimmyDawley", fill=(0, 0, 0), font_size=80)
    image.save(back)
    with db.session(engine) as session:
        session.add(Batch(id=1, name="batch-1"))
        session.commit()
    return settings, engine, back


def make_scan(session, back, sid, **kwargs) -> Scan:
    scan = Scan(id=sid, batch_id=1, source_key=f"s{sid}", front_path=str(back), back_path=str(back),
                status=ScanStatus.NEEDS_REVIEW.value, **kwargs)
    scan.ocr_frame = {"crop": None, "rotation": 0, "max_side": 1200}
    scan.ocr_lines = [
        {"text": "JimmyDawley", "raw": "JimmyDawley", "score": 0.99, "box": [[90, 90], [600, 90], [600, 190], [90, 190]]},
        {"text": "E66817567/67", "raw": "E66817567/67", "score": 0.86, "box": [[90, 300], [500, 300], [500, 360], [90, 360]]},
        {"text": "ORIGINAL", "raw": "ORIGINAL", "score": 0.99, "box": [[90, 400], [400, 400], [400, 460], [90, 460]]},
    ]
    corrections.suggest(scan, "ocr_line", [l["text"] for l in scan.ocr_lines], "rapidocr-ppocr@1.4.4")
    corrections.suggest(scan, "people", ["Jimmy Dawley", "Chocolat"], "spacy@3.8.0+rules@1")
    corrections.suggest(scan, "date", "2006-01-12", "date-rules@2")
    corrections.suggest(scan, "blank_back", "content", "edge-density@1", edge_density=0.012)
    session.add(scan)
    session.flush()
    return scan


def approve(session, scan, settings):
    scan.status = ScanStatus.APPROVED.value
    events = corrections.record_approval(session, scan, settings)
    session.commit()
    return events


def test_approval_writes_one_event_per_item(env):
    settings, engine, back = env
    with db.session(engine) as session:
        scan = make_scan(session, back, 1)
        lines = [dict(l) for l in scan.ocr_lines]
        lines[0]["corrected"] = "Jimmy Dawley"
        lines[1]["removed"] = True
        scan.ocr_lines = lines
        scan.people = ["Jimmy Dawley", "Grandma"]  # "Chocolat" removed, "Grandma" added
        from banana.dates import PhotoDate, Precision
        scan.set_photo_date(PhotoDate(Precision.DAY, 2006, 1, 12), "manual")
        scan.back_rotation = 180
        events = approve(session, scan, settings)

    got = {(e.field, e.action, str(e.suggested), str(e.approved)) for e in events}
    assert ("ocr_line", "edited", "JimmyDawley", "Jimmy Dawley") in got
    assert ("ocr_line", "removed", "E66817567/67", "None") in got
    assert ("ocr_line", "kept", "ORIGINAL", "ORIGINAL") in got
    assert ("person", "kept", "Jimmy Dawley", "Jimmy Dawley") in got
    assert ("person", "removed", "Chocolat", "None") in got
    assert ("person", "added", "None", "Grandma") in got
    assert ("date", "kept", "2006-01-12", "2006-01-12") in got
    assert ("rotation", "edited", "0", "180") in got
    assert all(e.producer for e in events)  # producer is mandatory
    added = next(e for e in events if e.action == "added")
    assert added.producer == "operator"
    edited_line = next(e for e in events if e.field == "ocr_line" and e.action == "edited")
    assert edited_line.asset_ref["raw_ocr"] == "JimmyDawley"
    image = edited_line.asset_ref["image"]
    with Image.open(image) as crop:  # the line image travels with the corrected text
        assert crop.width > 400 and crop.height < 200
    assert all(e.batch == "batch-1" for e in events)


def test_auto_rotation_suggestion_kept_and_overridden(env):
    settings, engine, back = env
    producer = corrections.producers(settings)["rotation_auto"]
    with db.session(engine) as session:
        kept = make_scan(session, back, 2)
        corrections.suggest(kept, "rotation_front", 90, producer, confidence=1.8)
        corrections.suggest(kept, "rotation_back", 90, producer, confidence=1.8)
        kept.front_rotation = kept.back_rotation = 90  # operator leaves the auto-rotation as-is
        kept_events = approve(session, kept, settings)

        overridden = make_scan(session, back, 3)
        corrections.suggest(overridden, "rotation_front", 90, producer, confidence=1.8)
        corrections.suggest(overridden, "rotation_back", 90, producer, confidence=1.8)
        overridden.front_rotation = overridden.back_rotation = 90
        overridden.back_rotation = 180  # operator disagrees with the auto-rotation and fixes just the back
        overridden_events = approve(session, overridden, settings)

    kept_rotation = {e.asset_ref["side"]: e for e in kept_events if e.field == "rotation"}
    assert kept_rotation["front"].action == "kept" and kept_rotation["front"].producer == producer
    assert kept_rotation["back"].action == "kept" and str(kept_rotation["back"].suggested) == "90"

    overridden_rotation = {e.asset_ref["side"]: e for e in overridden_events if e.field == "rotation"}
    assert overridden_rotation["front"].action == "kept"  # front wasn't touched
    assert overridden_rotation["back"].action == "edited"
    assert str(overridden_rotation["back"].suggested) == "90" and str(overridden_rotation["back"].approved) == "180"


def test_events_are_append_only(env):
    settings, engine, back = env
    with db.session(engine) as session:
        approve(session, make_scan(session, back, 1), settings)
    con = sqlite3.connect(settings.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE correction_event SET action = 'kept'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM correction_event")


def test_reapproval_unchanged_writes_nothing_changed_writes_new_set(env):
    settings, engine, back = env
    with db.session(engine) as session:
        scan = make_scan(session, back, 1)
        first = approve(session, scan, settings)
        assert first
        assert approve(session, scan, settings) == []
        scan.people = ["Jimmy Dawley"]
        again = approve(session, scan, settings)
        assert again and again[0].approval_id != first[0].approval_id
        assert len(list(session.exec(select(CorrectionEvent)))) == len(first) + len(again)


def test_training_events_only_latest_approval_of_approved_scans(env):
    settings, engine, back = env
    with db.session(engine) as session:
        approved = make_scan(session, back, 1)
        approve(session, approved, settings)
        approved.people = ["Jimmy Dawley"]
        latest = approve(session, approved, settings)

        rejected = make_scan(session, back, 2)
        approve(session, rejected, settings)
        rejected.status = ScanStatus.REJECTED.value  # approved once, then rejected: never training data
        session.commit()

        make_scan(session, back, 3)  # pending: no events at all
        session.commit()

        usable = corrections.training_events(session)
    assert {e.scan_id for e in usable} == {1}
    assert {e.approval_id for e in usable} == {latest[0].approval_id}


def test_dictionary_learns_line_and_word_fixes_and_suppression(env):
    settings, engine, back = env
    with db.session(engine) as session:
        for sid in (1, 2):
            scan = make_scan(session, back, sid)
            lines = [dict(l) for l in scan.ocr_lines]
            lines[0]["corrected"] = "Jimmy Dawley"
            scan.ocr_lines = lines
            scan.people = ["Jimmy Dawley"]  # "Chocolat" removed on both scans
            approve(session, scan, settings)
        learned = dictionary.build(session)

    assert learned.correct_line("JimmyDawley") == ("Jimmy Dawley", True)
    assert learned.correct_line("Field trip with JimmyDawley") == ("Field trip with Jimmy Dawley", True)
    assert learned.correct_line("Something else") == ("Something else", False)
    # A rescan of the same label is read with different spacing/punctuation: still matches the learned line.
    assert learned.correct_line("Jimmy-Dawley") == ("Jimmy Dawley", True)
    assert learned.is_suppressed("person", "chocolat") and not learned.is_suppressed("person", "Jimmy Dawley")


def test_dictionary_applied_to_new_ocr(env, monkeypatch):
    from banana.analysis import ocr
    from banana.ingest import service

    settings, engine, back = env

    class Reader:
        name = "fake"

        def read(self, image, use_cls: bool = True):
            return [ocr.TextLine("JimmyDawley", 0.99, [[0, 0], [10, 0], [10, 10], [0, 10]])]

    monkeypatch.setattr(ocr, "get_reader", lambda: Reader())
    learned = dictionary.CorrectionDictionary(words={"JimmyDawley": "Jimmy Dawley"})
    scan = Scan(id=9, batch_id=1, source_key="k", front_path=str(back), back_path=str(back))
    service.read_back_text(scan, settings, learned=learned)
    line = scan.ocr_lines[0]
    assert line["text"] == "Jimmy Dawley" and line["raw"] == "JimmyDawley" and line["dictionary"].startswith("dictionary@")
    assert scan.suggestions["ocr_line"]["value"] == ["Jimmy Dawley"]
    assert scan.ocr_frame["max_side"] == settings.analysis.ocr_max_side


def test_blank_back_proposal_needs_evidence_then_applies_explicitly(env):
    settings, engine, back = env
    with db.session(engine) as session:
        assert proposals.blank_back_proposal(session, settings).proposed is None  # not enough evidence
        for sid in range(1, 13):
            scan = make_scan(session, back, sid)
            is_blank = sid <= 6
            density = 0.0015 + sid * 0.0001 if is_blank else 0.004 + sid * 0.0001  # current 0.001 calls all "content"
            corrections.suggest(scan, "blank_back", "content", "edge-density@1", edge_density=density)
            scan.keep_back = not is_blank
            approve(session, scan, settings)
        proposal = proposals.blank_back_proposal(session, settings)
        assert proposal.proposed is not None and 0.0021 < proposal.proposed < 0.0051
        assert settings.analysis.blank_edge_density == 0.001  # nothing applied silently

        proposals.apply_proposal(session, settings, "analysis.blank_edge_density")
        assert settings.analysis.blank_edge_density == proposal.proposed
        fresh = Settings()
        proposals.apply_overrides(session, fresh)
        assert fresh.analysis.blank_edge_density == proposal.proposed  # survives restarts
        proposals.revert_override(session, settings, "analysis.blank_edge_density")
        assert settings.analysis.blank_edge_density == 0.001


def test_producers_are_versioned(env):
    settings, _, _ = env
    produced = corrections.producers(settings)
    assert all("@" in value for value in produced.values())
