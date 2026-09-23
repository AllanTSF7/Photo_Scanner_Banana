"""The Immich client is structurally read-only: no put/patch/delete method exists on it at all, and its one
POST path is checked against a hard allow-list before any request is made."""

import pytest

from banana.immich.client import BULK_CHECK_CHUNK, ImmichClient, build_client
from banana.immich.settings import EffectiveImmichConfig
from tests.immich._fake_server import FakeImmichServer


@pytest.fixture
def server():
    srv = FakeImmichServer()
    yield srv
    srv.close()


@pytest.fixture
def client(server):
    cfg = EffectiveImmichConfig(enabled=True, url=server.url, api_key="k", library_id="lib-1", import_path_prefix="/x")
    with ImmichClient(cfg) as c:
        yield c


def test_no_mutating_methods_exist_at_all():
    for verb in ("put", "patch", "delete"):
        assert not hasattr(ImmichClient, verb), f"ImmichClient must never have a .{verb}() method"


def test_statistics_is_get_only(server, client):
    server.statistics = {"images": 42, "videos": 3}
    assert client.statistics() == {"images": 42, "videos": 3}
    assert server.requests == [("GET", "/api/assets/statistics")]


def test_thumbnail_is_get_only(server, client):
    server.thumbnails["a1"] = b"\xff\xd8fake-jpeg-bytes"
    data = client.thumbnail_bytes("a1")
    assert data == b"\xff\xd8fake-jpeg-bytes"
    assert server.requests == [("GET", "/api/assets/a1/thumbnail?size=thumbnail")]


def test_bulk_upload_check_posts_only_to_the_allowlisted_path(server, client):
    server.checksums = {"abc123": "matched-asset-id"}
    result = client.bulk_upload_check([("scan1:front", "abc123"), ("scan1:back", "def456")])
    assert result["scan1:front"]["assetId"] == "matched-asset-id"
    assert result["scan1:back"]["action"] == "accept"
    assert [r[:2] for r in server.requests] == [("POST", "/api/assets/bulk-upload-check")]


def test_bulk_upload_check_chunks_large_batches(server, client):
    items = [(f"scan{i}", f"sum{i}") for i in range(BULK_CHECK_CHUNK + 10)]
    client.bulk_upload_check(items)
    posts = [r for r in server.requests if r[1] == "/api/assets/bulk-upload-check"]
    assert len(posts) == 2  # one full chunk, one remainder - never a single giant request
    assert len(posts[0][2]["assets"]) == BULK_CHECK_CHUNK
    assert len(posts[1][2]["assets"]) == 10


def test_search_metadata_posts_only_to_the_allowlisted_path(server, client):
    server.search_pages[1] = {"assets": {"items": [{"id": "a1", "checksum": "x"}], "nextPage": None}}
    result = client.search_metadata_page("lib-1", 1)
    assert result["assets"]["items"][0]["id"] == "a1"
    assert [r[:2] for r in server.requests] == [("POST", "/api/search/metadata")]


def test_search_metadata_asks_for_images_only_and_omits_a_blank_library_filter(server, client):
    server.search_pages[1] = {"assets": {"items": [], "nextPage": None}}
    client.search_metadata_page("lib-1", 1)
    client.search_metadata_page("", 1)
    with_lib, without_lib = [r[2] for r in server.requests]
    assert with_lib["type"] == without_lib["type"] == "IMAGE"  # videos are never compared
    assert with_lib["libraryId"] == "lib-1"
    assert "libraryId" not in without_lib  # blank = every image the key can see


def test_post_refuses_anything_off_the_allowlist_and_makes_no_request(server, client):
    with pytest.raises(RuntimeError, match="not on the read-only allow-list"):
        client._post("assets/some-id")
    assert server.requests == []  # refused before the network call, not after


def test_build_client_is_none_unless_fully_enabled(server):
    assert build_client(EffectiveImmichConfig(False, server.url, "k", "lib", "/x")) is None
    assert build_client(EffectiveImmichConfig(True, "", "k", "lib", "/x")) is None
    assert build_client(EffectiveImmichConfig(True, server.url, "", "lib", "/x")) is None
    real = build_client(EffectiveImmichConfig(True, server.url, "k", "lib", "/x"))
    assert real is not None
    real.close()
