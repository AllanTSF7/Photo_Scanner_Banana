"""A bare-bones fake Immich HTTP server for tests - the same spirit as
tests/ui/test_ui_live.py::_FakeScannerPort (never touch a real external device/service in tests), but at the
HTTP level. Records every request it receives so tests can assert exactly what was and wasn't called.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - quiet test output
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self.server.requests.append(("GET", self.path))
        if self.path == "/api/assets/statistics":
            self._json(200, self.server.statistics)
        elif self.path.startswith("/api/assets/") and "/thumbnail" in self.path:
            asset_id = self.path.split("/")[3]
            data = self.server.thumbnails.get(asset_id, b"")
            self.send_response(200 if data else 404)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(("POST", self.path, body))
        if self.path == "/api/assets/bulk-upload-check":
            results = []
            for item in body.get("assets", []):
                match = self.server.checksums.get(item["checksum"])
                results.append({"id": item["id"], "action": "reject" if match else "accept", "assetId": match})
            self._json(200, {"results": results})
        elif self.path == "/api/search/metadata":
            page = body.get("page", 1)
            self._json(200, self.server.search_pages.get(page, {"assets": {"items": [], "nextPage": None}}))
        else:
            self._json(404, {"message": "not found"})

    def _reject(self) -> None:
        self.server.requests.append((self.command, self.path))
        self._json(405, {"message": "not allowed"})

    do_PUT = do_PATCH = do_DELETE = _reject


class FakeImmichServer(ThreadingHTTPServer):
    """`requests` is the full (method, path[, body]) log. `search_pages` maps a 1-indexed page number to the
    canned response for `search/metadata`; `checksums` maps a checksum to the asset id it should "match"."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[tuple] = []
        self.statistics: dict = {"images": 0, "videos": 0}
        self.checksums: dict[str, str] = {}
        self.thumbnails: dict[str, bytes] = {}
        self.search_pages: dict[int, dict] = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        self.shutdown()
