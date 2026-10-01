from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, select

from banana import __version__, auth, core, db
from banana import health as health_checks
from banana.analysis import autocorrect
from banana.analysis.date_parse import find_dates
from banana.config import load_settings
from banana.dates import PhotoDate
from banana.export.service import ManualDate
from banana.ingest import thumbs
from banana.export.service import scan_edit
from banana.imaging import Edit
from banana.analysis import ocr
from banana.analysis import entities
from banana.core import corrections, dictionary, proposals
from banana.ingest.service import (
    analyze_scan, extract_for_scan, known_entities, read_back_text, record_entity_suggestions,
    release_from_duplicate_group,
)
from banana.immich import settings as immich_settings
from banana.ingest.runner import IngestRunner
from banana.immich.client import build_client
from banana.immich.dedup import ImmichCheckController
from banana.models import Batch, CorrectionEvent, Export, Scan, ScanStatus, SettingOverride, utcnow
from banana.scanner import sane

STATIC = Path(__file__).parent / "static"

settings = load_settings()
engine = db.make_engine(settings.db_path)
with db.session(engine) as _startup_session:
    proposals.apply_overrides(_startup_session, settings)  # thresholds the operator applied from proposals
app = FastAPI(
    title="Photo Scanner API",
    version=__version__,
    description="Scan triage backend: ingest the scanner inbox, review dates/tags, export EXIF+XMP for Immich.",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
scans = sane.ScanController(settings.scanner, settings.paths.inbox)
ingests = IngestRunner(engine, settings)  # every ingest goes through this: one at a time, never overlapping
immich_check = ImmichCheckController(engine, settings)

# Every request goes through one gate (banana/auth.py has the account and session logic):
#  - Host must be one this app answers to: blocks DNS rebinding (a web page whose name resolves to us).
#  - A state-changing request that carries an Origin must come from this same host: blocks CSRF from other
#    sites. Non-browser clients send no Origin and are unaffected.
#  - Everything except the public paths below needs a signed-in session.
_PUBLIC_PATHS = {"/health", "/login", "/api/auth/login", "/api/auth/setup", "/api/auth/state", "/api/dev/reload-token"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_API_PREFIXES = ("/api/", "/docs", "/redoc", "/openapi.json")
_ALLOWED_HOSTS = {h.strip("[]").lower() for h in settings.auth.allowed_hosts}


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8420
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@app.middleware("http")
async def security_gate(request: Request, call_next):
    host_header = request.headers.get("host", "")
    if _host_name(host_header) not in _ALLOWED_HOSTS:
        return PlainTextResponse("Unknown host. Add it to [auth] allowed_hosts in config.toml.", status_code=400)
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc.lower() != host_header.lower():
            return PlainTextResponse("Cross-site request refused.", status_code=403)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    with db.session(engine) as session:
        user = auth.resolve_session(session, request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        if path.startswith(_API_PREFIXES):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.username = user.username
    return await call_next(request)


def get_session():
    with db.session(engine) as session:
        yield session


def _get_scan(session: Session, scan_id: int) -> Scan:
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(404, f"scan {scan_id} not found")
    return scan


def _date_json(d: PhotoDate) -> dict:
    return {
        "precision": d.precision.value, "year": d.year, "month": d.month, "day": d.day,
        "season": d.season, "circa": d.circa, "label": d.label(), "exif": d.exif_datetime(),
        "approximate": d.is_approximate,
    }


def _scan_json(session: Session, scan: Scan) -> dict:
    batch = session.get(Batch, scan.batch_id)
    export = session.exec(select(Export).where(Export.scan_id == scan.id)).first()
    data = scan.model_dump()
    data.update(
        batch=batch.name if batch else None,
        box_label=batch.box_label if batch else None,
        date=_date_json(scan.photo_date()),
        has_back=scan.back_path is not None,
        export=export.model_dump() if export else None,
    )
    return data


_STARTED = str(time.time())

@app.get("/api/dev/reload-token", include_in_schema=False)
def dev_reload_token() -> dict:
    """Live reload for the dev server: the token changes when the server restarts or a static file changes.
    Always present (so the page's probe never logs a 404); `enabled` is false unless BANANA_DEV_RELOAD is set."""
    if not os.environ.get("BANANA_DEV_RELOAD"):
        return {"enabled": False, "token": None}
    mtimes = sorted((p.name, p.stat().st_mtime_ns) for p in STATIC.iterdir())
    return {"enabled": True, "token": f"{_STARTED}:{hash(tuple(mtimes))}"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(STATIC / "login.html")


class Credentials(BaseModel):
    username: str
    password: str


# Failed sign-ins per client address: after _MAX_FAILURES within _FAILURE_WINDOW seconds, refuse for the rest
# of the window. scrypt already makes each guess slow; this caps how many guesses a client gets.
_MAX_FAILURES, _FAILURE_WINDOW = 10, 600
_failures: dict[str, deque] = {}
_failures_lock = threading.Lock()


def _throttled(client: str) -> bool:
    with _failures_lock:
        recent = _failures.setdefault(client, deque())
        while recent and recent[0] < time.monotonic() - _FAILURE_WINDOW:
            recent.popleft()
        return len(recent) >= _MAX_FAILURES


def _record_failure(client: str) -> None:
    with _failures_lock:
        _failures.setdefault(client, deque()).append(time.monotonic())


def _signed_in(response: Response, session: Session, user) -> dict:
    token = auth.create_session(session, user, settings.auth.session_days)
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=settings.auth.session_days * 86400,
        httponly=True, samesite="strict", path="/",
    )
    return {"username": user.username}


@app.get("/api/auth/state", tags=["auth"])
def auth_state(request: Request, session: Session = Depends(get_session)) -> dict:
    """Public: whether the first-account setup is open, and who (if anyone) this browser is signed in as."""
    user = auth.resolve_session(session, request.cookies.get(auth.COOKIE_NAME))
    return {
        "setup_open": settings.auth.setup_page and not auth.any_users(session),
        "username": user.username if user else None,
    }


@app.post("/api/auth/login", tags=["auth"])
def auth_login(body: Credentials, request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    client = request.client.host if request.client else "unknown"
    if _throttled(client):
        raise HTTPException(429, "Too many failed sign-ins. Wait 10 minutes and try again.")
    user = auth.authenticate(session, body.username, body.password)
    if user is None:
        _record_failure(client)
        raise HTTPException(401, "Wrong username or password.")
    return _signed_in(response, session, user)


@app.post("/api/auth/setup", tags=["auth"])
def auth_setup(body: Credentials, response: Response, session: Session = Depends(get_session)) -> dict:
    """Create the first account. Only while setup is enabled in config and no account exists yet."""
    if not settings.auth.setup_page or auth.any_users(session):
        raise HTTPException(403, "Setup is closed. Ask an administrator to create your account.")
    try:
        user = auth.create_user(session, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _signed_in(response, session, user)


@app.post("/api/auth/logout", tags=["auth"])
def auth_logout(request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    auth.end_session(session, request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"])
def auth_me(request: Request) -> dict:
    return {"username": request.state.username}


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"version": __version__, "native_core": core.NATIVE_AVAILABLE}


@app.get("/api/summary", tags=["system"])
def summary(session: Session = Depends(get_session)) -> dict:
    counts = Counter(s.status for s in session.exec(select(Scan)))
    return {
        "counts": {status.value: counts.get(status.value, 0) for status in ScanStatus},
        "native_core": core.NATIVE_AVAILABLE,
        "paths": {k: str(v) for k, v in settings.paths},
    }


@app.get("/api/scanner", tags=["scanner"])
def scanner_status() -> dict:
    """Whether the configured network scanner answers on its scan port (TCP connect, 1.5 s timeout)."""
    cfg = settings.scanner
    if not cfg.host:
        return {"configured": False, "stranded": scans.stranded()}
    return {
        "configured": True, "host": cfg.host, "port": cfg.port, "online": sane.is_online(cfg),
        "device": cfg.device, "source": cfg.source, "mode": cfg.mode, "resolution": cfg.resolution,
        "after_scan": cfg.after_scan, "duplex": cfg.duplex, "max_feeder_count": cfg.max_feeder_count,
        "scan": scans.status(), "stranded": scans.stranded(),
    }


def _ingest_after_scan() -> dict:
    # The run's own files were just placed by this app: no need to wait for them to settle.
    return ingests.run(reason="scan", trusted=scans.status()["files"])


@app.post("/api/scanner/recover", tags=["scanner"])
def recover_scans() -> dict:
    """Finish scan runs that stopped before their pages reached the inbox, then ingest them for review."""
    try:
        result = scans.recover()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result["recovered"]:
        runs = tuple(f"{r['run']}_" for r in result["recovered"])
        placed = [p.name for p in settings.paths.inbox.iterdir() if p.is_file() and p.name.startswith(runs)]
        result["ingest"] = ingests.run(reason="recover", trusted=placed)
    return result


def _on_startup() -> None:
    """Background start-up work, in order: finish scan runs cut off by a crash or a closed app (their pages
    sit in a hidden staging folder), report archived files with no scan, then start watching the inbox."""
    try:
        if settings.scanner.recover_on_start and scans.stranded():
            recover_scans()
    except Exception:  # noqa: BLE001 - never stop the app from starting; the banner still offers Recover
        logging.getLogger("banana.scanner").exception("recovering stranded scans at startup failed")
    try:
        ingests.check_orphans()
    except Exception:  # noqa: BLE001
        logging.getLogger("banana.ingest").exception("checking the archive for files with no scan failed")
    ingests.start_watching()


threading.Thread(target=_on_startup, daemon=True, name="startup").start()


class ScanRequest(BaseModel):
    destination: Literal["review", "inbox"] | None = None  # default: scanner.after_scan
    count: Literal["all", "one"] = "all"  # one: a single photo (both sides when duplex)


@app.post("/api/scanner/scan", tags=["scanner"])
def start_scan(body: ScanRequest | None = None) -> dict:
    """Scan everything in the feeder (SANE, background).

    destination "review": files go to the inbox and are ingested into the review queue when the scan finishes.
    destination "inbox": files are only placed in the inbox (ingest later with POST /api/ingest).
    """
    try:
        return scans.start(
            on_complete=_ingest_after_scan,
            destination=body.destination if body else None,
            count=body.count if body else "all",
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/downloads/windows", tags=["system"])
def download_windows_build() -> FileResponse:
    """The packaged desktop build for a new scanning station (.github/workflows/desktop-build.yml
    publishes it here after every build that passes). Inherits the same password gate as everything
    else in this app - there is no separate, unprotected download page."""
    path = settings.paths.data_dir / "downloads" / "PhotoScanner_win64.zip"
    if not path.exists():
        raise HTTPException(404, "no build has been published yet")
    return FileResponse(path, filename="PhotoScanner_win64.zip", media_type="application/zip")


@app.get("/api/health", tags=["system"])
def component_health() -> dict:
    """Health of each component: API, database, ExifTool, C++ core, folders, SANE, scanner, Immich."""
    checks = health_checks.run_checks(settings, engine)
    checks.insert(len(checks) - 1, health_checks.ingest_check(ingests.status()))  # before Immich
    return {"overall": health_checks.overall(checks), "checks": checks}


class ImmichSettingsUpdate(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    library_id: str | None = None
    import_path_prefix: str | None = None
    api_key: str | None = None  # omitted = unchanged; "" explicitly clears it


@app.get("/api/immich/settings", tags=["immich"])
def get_immich_settings(session: Session = Depends(get_session)) -> dict:
    """Never returns the raw API key - just whether one is set and its last 4 characters."""
    return immich_settings.get_masked(session, settings)


@app.patch("/api/immich/settings", tags=["immich"])
def update_immich_settings(body: ImmichSettingsUpdate, session: Session = Depends(get_session)) -> dict:
    return immich_settings.save(
        session, settings, enabled=body.enabled, url=body.url, library_id=body.library_id,
        import_path_prefix=body.import_path_prefix, api_key=body.api_key,
    )


@app.get("/api/immich/status", tags=["immich"])
def immich_status(session: Session = Depends(get_session)) -> dict:
    """A live, read-only probe - separate from and cheaper than the full duplicate check. Makes no network
    call at all when Immich checking isn't enabled."""
    cfg = immich_settings.get_effective(session, settings)
    if not cfg.enabled:
        return {"enabled": False, "connected": False, "asset_count": None, "error": None}
    client = build_client(cfg)
    try:
        stats = client.statistics()
        count = stats.get("images") if isinstance(stats, dict) else None
        return {"enabled": True, "connected": True, "asset_count": count, "error": None}
    except httpx.HTTPStatusError as exc:
        error = f"HTTP {exc.response.status_code} - check the API key/library id"
        return {"enabled": True, "connected": False, "asset_count": None, "error": error}
    except httpx.HTTPError as exc:
        return {"enabled": True, "connected": False, "asset_count": None, "error": f"Could not connect: {exc}"}
    finally:
        client.close()


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@app.get("/api/immich/asset/{asset_id}/image", tags=["immich"])
def immich_asset_image(asset_id: str, session: Session = Depends(get_session)) -> Response:
    """A preview of an Immich photo a scan matched, fetched read-only by the server so the browser never sees
    the API key and the operator can compare side by side without leaving the app. Only assets that a scan or
    export actually matched are served - this isn't a general window onto the Immich library."""
    matched = _UUID.match(asset_id) and (
        session.exec(select(Scan.id).where(Scan.immich_duplicate_asset_id == asset_id)).first() is not None
        or session.exec(select(Export.id).where(
            (Export.immich_front_id == asset_id) | (Export.immich_back_id == asset_id))).first() is not None
    )
    if not matched:
        raise HTTPException(404, "no scan matched that Immich photo")
    client = build_client(immich_settings.get_effective(session, settings))
    if client is None:
        raise HTTPException(409, "Immich checking isn't enabled")
    try:
        data = client.thumbnail_bytes(asset_id, "preview")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(404 if exc.response.status_code == 404 else 502, "Immich couldn't provide that photo") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach Immich: {exc}") from exc
    finally:
        client.close()
    kind = "image/webp" if data[8:12] == b"WEBP" else "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/immich/check", tags=["immich"])
def start_immich_check() -> dict:
    """Kick off the read-only duplicate check (exact-checksum + perceptual) in the background. Operator-
    triggered only - never automatic on ingest."""
    try:
        return immich_check.start()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/immich/check/status", tags=["immich"])
def immich_check_status() -> dict:
    return immich_check.status()


@app.get("/api/scanner/scan", tags=["scanner"])
def scan_progress() -> dict:
    """State of the current or last scan: idle | scanning | done | failed, pages so far, message, ingest result."""
    return scans.status()


@app.get("/api/scans", tags=["scans"])
def list_scans(
    status: str | None = None, limit: int = Query(500, le=5000), session: Session = Depends(get_session)
) -> list[dict]:
    query = select(Scan).order_by(Scan.id).limit(limit)
    if status:
        query = query.where(Scan.status == status)
    return [_scan_json(session, s) for s in session.exec(query)]


@app.get("/api/scans/{scan_id}", tags=["scans"])
def get_scan(scan_id: int, session: Session = Depends(get_session)) -> dict:
    return _scan_json(session, _get_scan(session, scan_id))


class ScanUpdate(BaseModel):
    date_text: str | None = None  # free text as written on the back; "" clears the date
    date: ManualDate | None = None  # explicit alternative to date_text
    description: str | None = None
    people: list[str] | None = None
    places: list[str] | None = None
    events: list[str] | None = None
    tags: list[str] | None = None
    keep_back: bool | None = None
    status: Literal["needs_review", "approved", "rejected"] | None = None
    front_rotation: Literal[0, 90, 180, 270] | None = None
    back_rotation: Literal[0, 90, 180, 270] | None = None
    operator_duplicate: bool | None = None
    operator_immich_duplicate: bool | None = None


@app.patch("/api/scans/{scan_id}", tags=["scans"])
def update_scan(scan_id: int, body: ScanUpdate, session: Session = Depends(get_session)) -> dict:
    scan = _get_scan(session, scan_id)
    if body.date is not None:
        try:
            scan.set_photo_date(PhotoDate(**body.date.model_dump()), "manual")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif body.date_text is not None:
        text = body.date_text.strip()
        if not text:
            scan.set_photo_date(PhotoDate.unknown(), "manual")
        else:
            found = find_dates(text, two_digit_year_pivot=settings.dates.two_digit_year_pivot)
            if not found:
                raise HTTPException(422, f"could not understand date {text!r}")
            scan.set_photo_date(found[0].date, "manual")
    for name in (
        "description", "people", "places", "events", "tags", "keep_back", "status",
        "front_rotation", "back_rotation", "operator_duplicate", "operator_immich_duplicate",
    ):
        value = getattr(body, name)
        if value is not None:
            setattr(scan, name, value)
    if scan.back_path is None:
        scan.keep_back = False
    scan.updated_at = utcnow()
    session.add(scan)
    recorded = None
    if body.status == ScanStatus.APPROVED.value:
        session.flush()
        recorded = len(corrections.record_approval(session, scan, settings))  # Stage 1: only approvals write events
    session.commit()
    data = _scan_json(session, scan)
    if recorded is not None:
        data["corrections_recorded"] = recorded
    return data


@app.delete("/api/scans/{scan_id}", tags=["scans"])
def delete_scan(scan_id: int, session: Session = Depends(get_session)) -> dict:
    """Permanently remove a rejected scan's original files and its record. Never reachable during normal
    review - only for a scan already rejected, with no export and no correction-event (training) history, so
    this can never discard data the learning loop depends on."""
    scan = _get_scan(session, scan_id)
    if scan.status != ScanStatus.REJECTED.value:
        raise HTTPException(409, "only a rejected scan can be deleted")
    if session.exec(select(Export).where(Export.scan_id == scan_id)).first():
        raise HTTPException(409, "this scan was already exported; remove the exported copy separately")
    if session.exec(select(CorrectionEvent).where(CorrectionEvent.scan_id == scan_id)).first():
        raise HTTPException(409, "this scan has correction history used for training and can't be deleted")

    release_from_duplicate_group(session, scan)  # never leave another scan's "possible rescan of #N" dangling
    paths = [Path(v) for v in {scan.front_path, scan.back_path, scan.front_enhanced_path} if v]
    # The record goes first: if the commit fails (e.g. the database is busy) the originals are still there and
    # the scan is unchanged. Deleting files first left a row pointing at originals that were already gone.
    session.delete(scan)
    session.commit()
    removed, kept = [], []
    for path in paths:
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError as exc:  # e.g. open in another program: the record is gone, say which file stayed
            logging.getLogger("banana.web").warning("deleted scan %s but could not remove %s: %s", scan_id, path, exc)
            kept.append(str(path))
    return {"deleted": scan_id, "files_removed": removed, "files_kept": kept}


class OcrLineUpdate(BaseModel):
    text: str | None = None  # operator's corrected text; "" or the shown text clears the correction
    removed: bool | None = None  # True = not text (noise, lab code the operator rejects)


@app.put("/api/scans/{scan_id}/ocr-lines/{index}", tags=["learning"])
def update_ocr_line(scan_id: int, index: int, body: OcrLineUpdate, session: Session = Depends(get_session)) -> dict:
    """Correct or reject one line read from the back. Becomes a correction event when the scan is approved."""
    scan = _get_scan(session, scan_id)
    lines = [dict(line) for line in (scan.ocr_lines or [])]
    if not 0 <= index < len(lines):
        raise HTTPException(404, f"line {index} not found")
    line = lines[index]
    if body.text is not None:
        text = body.text.strip()
        if text and text != line.get("text"):
            line["corrected"] = text
        else:
            line.pop("corrected", None)
    if body.removed is not None:
        if body.removed:
            line["removed"] = True
        else:
            line.pop("removed", None)
    scan.ocr_lines = lines
    scan.updated_at = utcnow()
    session.add(scan)
    session.commit()
    return _scan_json(session, scan)


@app.get("/api/scans/{scan_id}/corrections", tags=["learning"])
def scan_corrections(scan_id: int, session: Session = Depends(get_session)) -> list[dict]:
    """Every correction event recorded for this scan, newest approval first."""
    _get_scan(session, scan_id)
    events = session.exec(
        select(CorrectionEvent).where(CorrectionEvent.scan_id == scan_id).order_by(CorrectionEvent.id.desc())
    )
    return [e.model_dump() for e in events]


@app.get("/api/learning", tags=["learning"])
def learning_status(session: Session = Depends(get_session)) -> dict:
    """Learning loop state: recorded corrections, the correction dictionary, threshold proposals, active producers."""
    all_events = list(session.exec(select(CorrectionEvent)))
    usable = corrections.training_events(session)
    by_field: dict[str, dict[str, int]] = {}
    for event in usable:
        by_field.setdefault(event.field, {"kept": 0, "edited": 0, "removed": 0, "added": 0})[event.action] += 1
    learned = dictionary.build(session)
    overrides = [o.model_dump() for o in session.exec(select(SettingOverride))]
    line_images = sum(1 for e in usable if e.field == "ocr_line" and (e.asset_ref or {}).get("image"))
    return {
        "events_total": len(all_events),
        "events_usable": len(usable),
        "approvals": len({e.approval_id for e in all_events}),
        "scans_with_corrections": len({e.scan_id for e in usable}),
        "by_field": by_field,
        "ocr_line_images": line_images,
        "dictionary": {**learned.to_dict(), "size": learned.size},
        "proposals": [p.to_dict() for p in proposals.all_proposals(session, settings)],
        "overrides": overrides,
        "producers": corrections.producers(settings),
        "stages": [
            {"stage": 1, "name": "Correction capture", "status": "implemented"},
            {"stage": 2, "name": "Dictionary, suppression, threshold proposals", "status": "implemented"},
            {"stage": 2, "name": "Label-format templates", "status": "planned"},
            {"stage": 3, "name": "Periodic retraining (TrOCR, spaCy, orientation)", "status": "planned"},
            {"stage": 4, "name": "Promotion gate (held-out by batch and writer)", "status": "planned"},
        ],
    }


@app.post("/api/learning/proposals/{key}/apply", tags=["learning"])
def apply_learning_proposal(key: str, session: Session = Depends(get_session)) -> dict:
    """Apply a threshold proposal. Explicit operator action only; the previous value is kept for revert."""
    try:
        override = proposals.apply_proposal(session, settings, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return override.model_dump()


@app.post("/api/learning/proposals/{key}/revert", tags=["learning"])
def revert_learning_proposal(key: str, session: Session = Depends(get_session)) -> dict:
    try:
        proposals.revert_override(session, settings, key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"reverted": key}


@app.get("/api/scans/{scan_id}/image/{side}", tags=["scans"], response_class=FileResponse)
def scan_image(
    scan_id: int, side: Literal["front", "back"], size: int = Query(1600, ge=64, le=4096), raw: bool = False,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Preview with the scan's crop and rotation applied. `raw=true` shows the unedited scanner page."""
    scan = _get_scan(session, scan_id)
    path = scan.front_path if side == "front" else scan.back_path
    if not path or not Path(path).exists():
        raise HTTPException(404, f"no {side} image")
    edit = Edit() if raw else scan_edit(scan, side)
    preview = thumbs.preview(Path(path), settings.paths.data_dir / "thumbs", size, edit)
    return FileResponse(preview, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.post("/api/scans/{scan_id}/swap-sides", tags=["scans"])
def swap_sides(scan_id: int, session: Session = Depends(get_session)) -> dict:
    """Swap front and back (images, crops, rotations), then re-run the blank-back and duplicate checks."""
    scan = _get_scan(session, scan_id)
    if not scan.back_path:
        raise HTTPException(409, "scan has no back image to swap with")
    scan.front_path, scan.back_path = scan.back_path, scan.front_path
    scan.front_crop, scan.back_crop = scan.back_crop, scan.front_crop
    scan.front_rotation, scan.back_rotation = scan.back_rotation, scan.front_rotation
    analyze_scan(session, scan, settings, detect_crops=False)
    scan.updated_at = utcnow()
    session.commit()
    return _scan_json(session, scan)


@app.post("/api/scans/{scan_id}/read-text", tags=["scans"])
def read_text(scan_id: int, session: Session = Depends(get_session)) -> dict:
    """OCR the back again (current crop and rotation). Fills description/date only if they're empty."""
    scan = _get_scan(session, scan_id)
    if not scan.back_path:
        raise HTTPException(409, "scan has no back image")
    learned = dictionary.build(session)
    if read_back_text(scan, settings, known=known_entities(session, exclude_id=scan.id), learned=learned) is None:
        raise HTTPException(503, f"no text reader available: {ocr.reader_error() or 'install the ocr extra'}")
    scan.updated_at = utcnow()
    session.add(scan)
    session.commit()
    return _scan_json(session, scan)


@app.post("/api/scans/{scan_id}/reanalyze", tags=["scans"])
def reanalyze(scan_id: int, session: Session = Depends(get_session)) -> dict:
    """Detect the photo crop again and re-run the blank-back and duplicate checks. Rotations are kept."""
    scan = _get_scan(session, scan_id)
    analyze_scan(session, scan, settings)
    scan.updated_at = utcnow()
    session.commit()
    return _scan_json(session, scan)


class EntityRequest(BaseModel):
    text: str
    scan_id: int | None = None  # excluded from the known-names vocabulary


@app.post("/api/entities/extract", tags=["text"])
def extract_entities(body: EntityRequest, session: Session = Depends(get_session)) -> dict:
    """People, places and events mentioned in a description (offline NER + rules + names used on other scans).
    With a scan_id the result is also recorded as that scan's suggestion, for correction events on approval."""
    learned = dictionary.build(session)
    found, producer = extract_for_scan(body.text, settings, known_entities(session, exclude_id=body.scan_id), learned)
    if body.scan_id is not None and settings.analysis.derive_entities:
        scan = session.get(Scan, body.scan_id)
        if scan is not None:
            record_entity_suggestions(scan, found, producer)
            session.add(scan)
            session.commit()
    return {**found.to_dict(), "engine": entities.engine_name(), "producer": producer}


class AutocorrectRequest(BaseModel):
    text: str
    scan_id: int | None = None  # its own names are protected, not corrected


@app.post("/api/autocorrect", tags=["text"])
def autocorrect_text(body: AutocorrectRequest, session: Session = Depends(get_session)) -> dict:
    """Fix recurring misspellings in typed or read text ("Januaru" -> "January") so they aren't retyped.
    Offline and reversible: the caller shows every fix and can put the original back."""
    known = known_entities(session, exclude_id=body.scan_id)
    protected = known.people + known.places + known.events
    fixed, fixes = autocorrect.correct(body.text, learned=dictionary.build(session), protected=protected)
    return {"text": fixed, "fixes": [f.to_dict() for f in fixes], "producer": autocorrect.VERSION}


@app.get("/api/dates/parse", tags=["dates"])
def parse_date(text: str) -> list[dict]:
    """Preview how free text on a photo back is interpreted."""
    found = find_dates(text, two_digit_year_pivot=settings.dates.two_digit_year_pivot)
    return [{"raw": c.raw, "confidence": c.confidence, **_date_json(c.date)} for c in found]


@app.post("/api/ingest", tags=["pipeline"])
def ingest() -> dict:
    """Pair files in the inbox, move them to the archive, flag blank backs and possible rescans.
    Waits for an ingest already running (a scan's, or the inbox watcher's) instead of overlapping it."""
    return ingests.run(reason="button")


@app.get("/api/ingest/status", tags=["pipeline"])
def ingest_status() -> dict:
    """Inbox watcher, the last ingest's result, photos moved aside as unreadable, archived files with no scan."""
    return ingests.status()


@app.post("/api/ingest/retry-unreadable", tags=["pipeline"])
def retry_unreadable() -> dict:
    """Move every file from inbox/_unreadable back into the inbox and ingest again."""
    moved = ingests.retry_unreadable()
    return {"moved": moved, "ingest": ingests.run(reason="retry", trusted=()) if moved else None}


@app.post("/api/export", tags=["pipeline"])
def export_approved(session: Session = Depends(get_session)) -> dict:
    """Write EXIF + XMP sidecars for every approved scan into the Immich library folder."""
    from banana.export.exiftool_writer import ExifToolWriter
    from banana.export.exporter import Exporter
    from banana.export.service import export_scan

    scans = list(session.exec(select(Scan).where(Scan.status == ScanStatus.APPROVED.value)))
    exported, errors = [], []
    if scans:
        with ExifToolWriter(settings.exiftool.path) as writer:
            exporter = Exporter(settings.paths.sorted, writer)
            for scan in scans:
                try:
                    record = export_scan(session, scan, exporter)
                    exported.append({"id": scan.id, "front": record.front_rel, "back": record.back_rel})
                except Exception as exc:  # noqa: BLE001 - reported per scan
                    session.rollback()
                    errors.append({"id": scan.id, "error": str(exc)})
    return {"exported": exported, "errors": errors}
