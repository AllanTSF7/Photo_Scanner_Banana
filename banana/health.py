"""Component health checks shown in the UI's System panel (GET /api/health)."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from banana import core
from banana.config import Settings
from banana.scanner import sane

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"


def _check(name: str, label: str, status: str, detail: str) -> dict:
    return {"name": name, "label": label, "status": status, "detail": detail}


@functools.lru_cache(maxsize=4)
def _exiftool_version(executable: str) -> str | None:
    try:
        return subprocess.run([executable, "-ver"], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _folder(name: str, label: str, path: Path) -> dict:
    if not path.exists():
        return _check(name, label, FAIL, f"{path} does not exist")
    if not os.access(path, os.W_OK):
        return _check(name, label, FAIL, f"{path} is not writable")
    return _check(name, label, OK, str(path))


def run_checks(settings: Settings, engine: Engine) -> list[dict]:
    checks = [_check("api", "API server", OK, "responding")]

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(_check("database", "Database", OK, str(settings.db_path)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("database", "Database", FAIL, str(exc)))

    exiftool = shutil.which(settings.exiftool.path)
    version = _exiftool_version(exiftool) if exiftool else None
    checks.append(
        _check("exiftool", "ExifTool", OK, f"{version} ({exiftool})")
        if version else _check("exiftool", "ExifTool", FAIL, f"'{settings.exiftool.path}' not found: export won't work")
    )

    checks.append(
        _check("native", "C++ core", OK, "banana_core loaded")
        if core.NATIVE_AVAILABLE else _check("native", "C++ core", WARN, "not built: using slower Python fallback")
    )

    checks += [
        _folder("inbox", "Inbox folder", settings.paths.inbox),
        _folder("archive", "Archive folder", settings.paths.archive),
        _folder("library", "Library folder", settings.paths.sorted),
        _folder("data", "Data folder", settings.paths.data_dir),
    ]

    if not settings.analysis.read_text:
        checks.append(_check("ocr", "Text reader", OFF, "disabled (analysis.read_text = false)"))
    else:
        from banana.analysis import ocr

        reader = ocr.get_reader()
        checks.append(
            _check("ocr", "Text reader", OK, f"{reader.name} (printed text; handwriting planned)")
            if reader else _check("ocr", "Text reader", WARN, f"unavailable: {ocr.reader_error()}")
        )

    if not settings.analysis.derive_entities:
        checks.append(_check("entities", "Entity extractor", OFF, "disabled (analysis.derive_entities = false)"))
    else:
        from banana.analysis import entities

        checks.append(
            _check("entities", "Entity extractor", OK, entities.engine_name())
            if entities.NLP.get() is not None
            else _check("entities", "Entity extractor", WARN, f"rules only: {entities.NLP.error}")
        )

    cfg = settings.scanner
    if not cfg.host and not cfg.sane_device:
        checks.append(_check("sane", "SANE", OFF, "no scanner configured"))
        checks.append(_check("scanner", "Scanner", OFF, "not configured (scanner.host)"))
    else:
        scanimage = shutil.which(cfg.scanimage)
        checks.append(
            _check("sane", "SANE", OK, scanimage)
            if scanimage else _check("sane", "SANE", FAIL, f"'{cfg.scanimage}' not found: install sane-utils")
        )
        online = sane.is_online(cfg)
        checks.append(
            _check("scanner", "Scanner", OK if online else FAIL,
                   f"{cfg.device} {'reachable' if online else f'unreachable on port {cfg.port}'}")
        )

    immich = settings.immich
    checks.append(
        _check("immich", "Immich", OFF, "not connected (automation planned)")
        if not (immich.url and immich.api_key) else _check("immich", "Immich", WARN, "configured, integration planned")
    )
    return checks


def overall(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else OK
