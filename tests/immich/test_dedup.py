"""Read-only Immich duplicate check: exact-checksum verification and the perceptual dHash pass, against a
fake local Immich server (never a real network call in tests)."""

from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from sqlmodel import select

from banana import core, db
from banana.config import Settings
from banana.immich.client import ImmichClient
from banana.immich.dedup import ImmichCheckController, check_exact, find_matches, refresh_asset_hashes
from banana.immich.settings import EffectiveImmichConfig
from banana.models import Batch, Export, ImmichAssetHash, Scan, ScanStatus
from tests.immich._fake_server import FakeImmichServer


@pytest.fixture
def server():
    srv = FakeImmichServer()
    yield srv
    srv.close()


@pytest.fixture
def env(tmp_path):
    settings = Settings.model_validate({"paths": {"data_dir": str(tmp_path / "data")}})
    engine = db.make_engine(settings.db_path)
    with db.session(engine) as session:
        session.add(Batch(id=1, name="batch-1"))
        session.commit()
    return settings, engine


def _photo(seed: int) -> bytes:
    """A distinct-enough image per seed that dHash tells them apart."""
    image = Image.new("RGB", (300, 200), (seed * 30 % 255, 120, 200 - seed * 20 % 200))
    for x in range(0, 300, 20):
        image.putpixel((x, seed * 7 % 200), (255, 255, 255))
    buf = BytesIO()
    image.save(buf, "JPEG")
    return buf.getvalue()


def test_check_exact_matches_by_sha1_and_records_the_asset_id(env, server):
    settings, engine = env
    with db.session(engine) as session:
        session.add(Scan(id=1, batch_id=1, source_key="s1", front_path="f", status=ScanStatus.EXPORTED.value))
        session.commit()
        session.add(Export(scan_id=1, front_rel="1984/x_A.jpg", front_sha256="x", front_sha1="matched-sha1",
                            back_rel="1984/x_B.jpg", back_sha256="y", back_sha1="unmatched-sha1"))
        session.commit()

        server.checksums = {"matched-sha1": "immich-asset-1"}
        cfg = EffectiveImmichConfig(True, server.url, "k", "lib", "/x")
        with ImmichClient(cfg) as client:
            stats = check_exact(client, session)

        assert stats.checked == 2 and stats.matched == 1
        export = session.exec(select(Export).where(Export.scan_id == 1)).one()
        assert export.immich_front_id == "immich-asset-1"
        assert export.immich_back_id is None  # checked, no match - not left as "never checked"
        assert export.immich_checked_at is not None


def test_check_exact_is_a_no_op_with_nothing_exported(env, server):
    settings, engine = env
    with db.session(engine) as session:
        cfg = EffectiveImmichConfig(True, server.url, "k", "lib", "/x")
        with ImmichClient(cfg) as client:
            stats = check_exact(client, session)
    assert stats.checked == 0 and server.requests == []  # no exports at all: never even calls out


def test_refresh_asset_hashes_computes_and_caches(env, server):
    settings, engine = env
    photo = _photo(1)
    server.thumbnails["a1"] = photo
    server.search_pages[1] = {"assets": {"items": [{"id": "a1", "checksum": "c1"}], "nextPage": None}}

    with db.session(engine) as session:
        cfg = EffectiveImmichConfig(True, server.url, "k", "lib", "/x")
        with ImmichClient(cfg) as client:
            stats = refresh_asset_hashes(client, session, "lib")
        assert stats.scanned == 1 and stats.refreshed == 1
        row = session.get(ImmichAssetHash, "a1")
        assert row.checksum == "c1"
        expected = f"{core.dhash(np.asarray(Image.open(BytesIO(photo)).convert('L'))):016x}"
        assert row.dhash_hex == expected

    thumbnail_requests = [r for r in server.requests if "thumbnail" in r[1]]
    assert len(thumbnail_requests) == 1

    # A second run with the same checksum skips re-fetching the thumbnail entirely.
    with db.session(engine) as session:
        with ImmichClient(cfg) as client:
            stats = refresh_asset_hashes(client, session, "lib")
        assert stats.scanned == 1 and stats.refreshed == 0
    assert len([r for r in server.requests if "thumbnail" in r[1]]) == 1  # still just the one fetch


def test_find_matches_flags_a_perceptually_similar_scan(env, server):
    settings, engine = env
    photo = _photo(2)
    with db.session(engine) as session:
        value = core.dhash(np.asarray(Image.open(BytesIO(photo)).convert("L")))
        session.add(ImmichAssetHash(asset_id="a2", checksum="c2", dhash_hex=f"{value:016x}"))
        session.add(Scan(id=1, batch_id=1, source_key="s1", front_path="f", dhash_hex=f"{value:016x}",
                          status=ScanStatus.NEEDS_REVIEW.value))
        session.add(Scan(id=2, batch_id=1, source_key="s2", front_path="f", dhash_hex=f"{value:016x}",
                          status=ScanStatus.REJECTED.value))  # rejected: never checked
        session.commit()

        stats = find_matches(session, settings)
        assert stats.matched == 1

        matched = session.get(Scan, 1)
        assert matched.immich_duplicate_asset_id == "a2"
        assert matched.suggestions["immich_duplicate"]["value"]["asset_id"] == "a2"
        assert matched.suggestions["immich_duplicate"]["value"]["distance"] == 0

        rejected = session.get(Scan, 2)
        assert rejected.immich_duplicate_asset_id is None  # excluded from the check


def test_find_matches_is_a_no_op_with_no_cached_hashes(env):
    settings, engine = env
    with db.session(engine) as session:
        session.add(Scan(id=1, batch_id=1, source_key="s1", front_path="f", dhash_hex="0" * 16))
        session.commit()
        assert find_matches(session, settings).matched == 0


def test_controller_runs_end_to_end_against_the_fake_server(env, server):
    import time

    settings, engine = env
    server.statistics = {"images": 1}
    with db.session(engine) as session:
        session.add(Scan(id=1, batch_id=1, source_key="s1", front_path="f", status=ScanStatus.NEEDS_REVIEW.value))
        session.commit()

    settings.immich.enabled = True
    settings.immich.url = server.url
    settings.immich.api_key = "k"
    settings.immich.library_id = "lib"
    controller = ImmichCheckController(engine, settings)

    controller.start()
    for _ in range(100):
        status = controller.status()
        if status["state"] != "running":
            break
        time.sleep(0.02)
    else:
        raise TimeoutError("check never finished")

    assert status["state"] == "done", status
    assert status["stats"]["assets_scanned"] == 0  # empty search_pages by default: nothing to scan, still clean


def test_controller_refuses_a_second_run_while_one_is_in_progress(env, server):
    # Deterministic, not timing-dependent: mark it running directly rather than racing a real background
    # thread that could plausibly finish (against an empty fake server) before a second start() is attempted.
    settings, engine = env
    settings.immich.enabled = True
    settings.immich.url = server.url
    settings.immich.api_key = "k"
    settings.immich.library_id = "lib"
    controller = ImmichCheckController(engine, settings)
    controller._run.state = "running"
    with pytest.raises(RuntimeError, match="already running"):
        controller.start()


def test_controller_refuses_to_start_when_disabled(env, server):
    settings, engine = env
    controller = ImmichCheckController(engine, settings)  # immich.enabled defaults to False
    with pytest.raises(RuntimeError):
        controller.start()
    assert server.requests == []
