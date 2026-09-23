"""Configuration loaded from TOML (path from BANANA_CONFIG, else ./config.toml, else defaults)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DEFAULT_PAIRING_PATTERN = r"^(?P<base>.+_\d{4})(?P<suffix>_a|_b)?\.(?:jpe?g|tiff?)$"


class PathsConfig(BaseModel):
    inbox: Path = Path("/mnt/photo_vault/inbox")
    archive: Path = Path("/mnt/photo_vault/archive")
    sorted: Path = Path("/mnt/photo_vault/sorted")
    data_dir: Path = Path("/srv/banana")


class PairingConfig(BaseModel):
    pattern: str = DEFAULT_PAIRING_PATTERN
    front_variant: Literal["original", "enhanced"] = "original"


class AnalysisConfig(BaseModel):
    blank_edge_density: float = 0.001  # backs below this are treated as blank
    dhash_max_distance: int = 6  # Hamming distance for "possible rescan"
    read_text: bool = True  # OCR photo backs during ingest/re-analyze (needs the "ocr" extra)
    ocr_max_side: int = 1800  # long side of the image given to OCR
    derive_entities: bool = True  # fill empty People/Places/Events from the description (needs the "ner" extra for NER)
    orientation_search: bool = True  # auto-rotate front+back from the back's OCR orientation (needs the "ocr" extra)
    orientation_search_max_side: int = 300  # long side per rotation tried; small and cheap, tried up to 4x


class DatesConfig(BaseModel):
    two_digit_year_pivot: int | None = None


class ScannerConfig(BaseModel):
    host: str = ""  # e.g. 192.168.16.178; empty hides the status indicator and disables scanning
    port: int = 1865  # Epson network scan protocol; the FF-680W has no eSCL/AirScan
    # SANE (Linux). or TWAIN (Windows) The FF-680W works with the built-in epsonds backend over the network.
    sane_device: str = ""  # default: epsonds:net:<host>
    scanimage: str = "scanimage"
    source: str = "ADF Duplex"  # or "ADF Front" for fronts only
    mode: Literal["Color", "Gray", "Lineart"] = "Color"
    resolution: int = 600  # epsonds offers up to 600 dpi for this model
    auto_crop: bool = True
    skew_correction: bool = True
    timeout_seconds: int = 3600
    max_feeder_count: int = 36  # the FF-680W ADF hopper's measured capacity; caps "Whole stack" so it can't jam past it
    after_scan: Literal["review", "inbox"] = "review"  # default destination; the UI can choose per scan
    # Which side of each duplex pair SANE delivers first. The FF-680W (photos loaded face down) reads the back first.
    first_side: Literal["front", "back"] = "back"

    @property
    def device(self) -> str:
        return self.sane_device or f"epsonds:net:{self.host}"

    @property
    def duplex(self) -> bool:
        return "duplex" in self.source.lower()


class ExifToolConfig(BaseModel):
    path: str = "exiftool"


class ImmichConfig(BaseModel):
    # Hard opt-in: filling in url/api_key alone must never start any network activity. The operator-saved
    # ImmichSetting DB row (banana/immich/settings.py) overrides all of this once anything has been saved there.
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    library_id: str = ""
    import_path_prefix: str = "/mnt/photo_vault/sorted"


class Settings(BaseModel):
    paths: PathsConfig = PathsConfig()
    pairing: PairingConfig = PairingConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    dates: DatesConfig = DatesConfig()
    scanner: ScannerConfig = ScannerConfig()
    exiftool: ExifToolConfig = ExifToolConfig()
    immich: ImmichConfig = ImmichConfig()

    @property
    def db_path(self) -> Path:
        return self.paths.data_dir / "banana.db"


def load_settings(path: Path | None = None) -> Settings:
    if path is None:
        env = os.environ.get("BANANA_CONFIG")
        path = Path(env) if env else Path("config.toml")
    if not path.exists():
        return Settings()
    # utf-8-sig: tolerate a BOM from Windows editors.
    return Settings.model_validate(tomllib.loads(path.read_text(encoding="utf-8-sig")))
