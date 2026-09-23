"""Minimal read-only Immich HTTP client.

Structurally read-only, not just by convention: there is no put/patch/delete method anywhere in this class, and
the one POST path (`_post`) is checked against a hard allow-list of exactly the two documented non-mutating
Immich endpoints before any request is made - calling it with anything else raises before the network call
happens. Never add a method here that could upload, edit, stack, delete, or trigger a library scan.

Route/param names are Immich's own, current as of the version researched (2026-09) - Immich has changed these
across major versions before, so if a real server behaves differently, that's the first thing to check (fetch
its own OpenAPI document rather than trusting this file blindly).
"""

from __future__ import annotations

import httpx

from banana.immich.settings import EffectiveImmichConfig

# The only two POST endpoints this client is allowed to call - both documented as read/check operations, not
# uploads. Immich models them as POST because the request body is too large for a query string, not because
# they mutate anything.
_READONLY_POST_PATHS = {"assets/bulk-upload-check", "search/metadata"}

BULK_CHECK_CHUNK = 500  # conservative; Immich doesn't document a hard cap on items per bulk-upload-check call


class ImmichClient:
    def __init__(self, cfg: EffectiveImmichConfig, timeout: float = 10.0) -> None:
        self._http = httpx.Client(
            base_url=cfg.url.rstrip("/") + "/api",
            headers={"x-api-key": cfg.api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ImmichClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, **kwargs) -> httpx.Response:
        response = self._http.get(path, **kwargs)
        response.raise_for_status()
        return response

    def _post(self, path: str, **kwargs) -> httpx.Response:
        if path not in _READONLY_POST_PATHS:
            raise RuntimeError(f"refusing to POST {path!r}: not on the read-only allow-list")
        response = self._http.post(path, **kwargs)
        response.raise_for_status()
        return response

    def statistics(self) -> dict:
        """GET assets/statistics - asset counts."""
        return self._get("assets/statistics").json()

    def bulk_upload_check(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        """items: [(caller_id, sha1_checksum), ...]. Returns {caller_id: {action, assetId}} - chunked, since
        Immich doesn't document a hard limit but a huge single request is impolite regardless."""
        results: dict[str, dict] = {}
        for start in range(0, len(items), BULK_CHECK_CHUNK):
            chunk = items[start:start + BULK_CHECK_CHUNK]
            body = {"assets": [{"id": item_id, "checksum": checksum} for item_id, checksum in chunk]}
            for entry in self._post("assets/bulk-upload-check", json=body).json().get("results", []):
                results[entry["id"]] = entry
        return results

    def search_metadata_page(self, library_id: str, page: int, page_size: int = 1000) -> dict:
        """One page of a library's assets (id/checksum/originalPath/...). Pagination field names are an
        unconfirmed assumption (`page`/`size` in the request, `items`/`nextPage` in the response) - verify
        against the real server; see module docstring."""
        body: dict = {"page": page, "size": page_size, "type": "IMAGE"}  # videos are never compared
        if library_id:  # blank = every image the API key can see, not one External Library
            body["libraryId"] = library_id
        return self._post("search/metadata", json=body).json()

    def thumbnail_bytes(self, asset_id: str, size: str = "thumbnail") -> bytes:
        """GET assets/{id}/thumbnail. `size` query param name/values are an unconfirmed assumption."""
        return self._get(f"assets/{asset_id}/thumbnail", params={"size": size}).content


def build_client(cfg: EffectiveImmichConfig) -> ImmichClient | None:
    """The one choke point every caller goes through - `None` whenever the feature isn't fully opted in, so
    "no network calls when disabled" holds everywhere at once rather than being re-checked at each call site."""
    if not cfg.enabled or not cfg.url or not cfg.api_key:
        return None
    return ImmichClient(cfg)
