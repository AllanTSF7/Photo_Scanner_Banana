"""Scanning through Epson's TWAIN driver (Epson Scan 2) on Windows, where SANE's `scanimage` doesn't exist.

Writes pages into the same staging folder and `page_NNNN.jpg` naming as the SANE path, so pairing,
placement and ingest in banana.scanner.sane work unchanged. The driver's own UI is never shown.
pytwain needs a Windows message loop in the thread that opens the source; ScanController runs this
entirely inside its own worker thread, which satisfies that.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from banana.config import ScannerConfig

JPEG_QUALITY = 95
_PIXEL_TYPES = {"Color": "TWPT_RGB", "Gray": "TWPT_GRAY", "Lineart": "TWPT_BW"}


class ScanInterrupted(RuntimeError):
    """The driver raised after handing over at least one page. The pages are on disk and usable; the run
    should still place and ingest them, and say what stopped it. (Real case, 2026-09-30: 12 runs where
    the driver raised after the last transfer, before its BMP was converted - every page was fine, but
    the whole run was reported as failed and left in a hidden staging folder.)"""

    def __init__(self, pages: list[Path], cause: BaseException) -> None:
        self.pages = pages
        self.cause = cause
        super().__init__(f"the scanner driver stopped after {len(pages)} page(s): {describe_error(cause)}")


def describe_error(exc: BaseException) -> str:
    """Exception class plus message plus any TWAIN condition code pytwain attached, for the log and the UI."""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    for attr in ("condition_code", "cc", "status"):
        value = getattr(exc, attr, None)
        if value is not None:
            text += f" ({attr}={value})"
    return text


def convert_leftovers(staging: Path, mode: str = "Color") -> list[Path]:
    """Convert any page_NNNN.bmp the driver wrote but never got converted into page_NNNN.jpg. A BMP that
    can't be decoded (the transfer itself was cut off) is left exactly where it is, never deleted."""
    converted = []
    for bmp in sorted(staging.glob("page_*.bmp")):
        jpg = bmp.with_suffix(".jpg")
        try:
            with Image.open(bmp) as image:
                image.convert("L" if mode in ("Gray", "Lineart") else "RGB").save(jpg, "JPEG", quality=JPEG_QUALITY)
        except OSError:
            jpg.unlink(missing_ok=True)
            continue
        os.remove(bmp)
        converted.append(jpg)
    return converted


def _current(result):
    """Current value from a pytwain get_capability result (a one-value or an enumeration container)."""
    _, value = result
    if isinstance(value, tuple) and len(value) == 3:
        current_index, _, values = value
        return values[current_index]
    return value


def pick_source(sources: list[str], wanted: str = "") -> str:
    if wanted:
        if wanted in sources:
            return wanted
        raise RuntimeError(f"TWAIN scanner {wanted!r} not found (available: {', '.join(sources) or 'none'})")
    for match in ("FF-680W", "EPSON"):
        for name in sources:
            if match in name.upper():
                return name
    if sources:
        return sources[0]
    raise RuntimeError("No TWAIN scanner found. Install Epson Scan 2 and add the scanner in Epson Scan 2 Utility.")


def _set(twain, src, cap: str, kind: str, value, required: bool = False) -> None:
    try:
        src.set_capability(getattr(twain, cap), getattr(twain, kind), value)
    except Exception as exc:  # noqa: BLE001 - optional capabilities vary by driver; required ones re-raise
        if required:
            raise RuntimeError(f"The scanner driver rejected {cap}={value!r}: {exc}") from exc


def acquire_pages(cfg: ScannerConfig, staging: Path, count: str = "all", twain=None) -> list[Path]:
    """Scan the feeder into `staging` as page_0001.jpg, page_0002.jpg, ... in scan order."""
    if twain is None:
        import twain  # Windows only; imported here so nothing else depends on it
    staging.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    pending: list[Path] = []

    sm = twain.SourceManager()
    try:
        src = sm.open_source(pick_source(list(sm.source_list), cfg.twain_source))
        try:
            if not _current(src.get_capability(twain.CAP_FEEDERLOADED)):
                raise RuntimeError("No photos in the feeder. Load the stack and try again.")
            sheets = 1 if count == "one" else cfg.max_feeder_count
            _set(twain, src, "CAP_FEEDERENABLED", "TWTY_BOOL", True)
            _set(twain, src, "CAP_DUPLEXENABLED", "TWTY_BOOL", cfg.duplex, required=cfg.duplex)
            _set(twain, src, "CAP_XFERCOUNT", "TWTY_INT16", sheets * (2 if cfg.duplex else 1))
            _set(twain, src, "ICAP_PIXELTYPE", "TWTY_UINT16", getattr(twain, _PIXEL_TYPES[cfg.mode]), required=True)
            _set(twain, src, "ICAP_XRESOLUTION", "TWTY_FIX32", float(cfg.resolution), required=True)
            _set(twain, src, "ICAP_YRESOLUTION", "TWTY_FIX32", float(cfg.resolution))
            _set(twain, src, "ICAP_AUTOMATICBORDERDETECTION", "TWTY_BOOL", cfg.auto_crop)
            _set(twain, src, "ICAP_AUTOMATICDESKEW", "TWTY_BOOL", cfg.skew_correction)
            _set(twain, src, "ICAP_AUTOMATICROTATE", "TWTY_BOOL", cfg.auto_rotate)

            def before(_info: dict) -> str:
                path = staging / f"page_{len(pages) + len(pending) + 1:04d}.bmp"
                pending.append(path)
                return str(path)

            def after(_more: int) -> None:
                # The driver hands over lossless BMP; keep the pipeline's JPEG convention at high quality.
                bmp = pending.pop()
                jpg = bmp.with_suffix(".jpg")
                with Image.open(bmp) as image:
                    image.convert("RGB" if cfg.mode == "Color" else "L").save(jpg, "JPEG", quality=JPEG_QUALITY)
                os.remove(bmp)
                pages.append(jpg)

            try:
                src.acquire_file(before=before, after=after, show_ui=False, modal=False)
            except Exception as exc:  # noqa: BLE001 - keep whatever pages arrived, then report the cause
                pages.extend(convert_leftovers(staging, cfg.mode))
                if not pages:
                    raise
                raise ScanInterrupted(sorted(pages), exc) from exc
        finally:
            src.close()
    finally:
        sm.close()
    return pages
