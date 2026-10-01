from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from banana.dates import PhotoDate, Precision


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScanStatus(str, Enum):
    INGESTED = "ingested"
    ANALYZED = "analyzed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    EXPORTED = "exported"
    STACKED = "stacked"
    REJECTED = "rejected"


class Batch(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    box_label: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Scan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="batch.id", index=True)
    source_key: str = Field(unique=True, index=True)  # scanner base name or original path
    # sha256 of the source front file: tells a re-dropped photo from a new one that reuses an old name.
    source_sha256: str | None = None

    front_path: str
    front_enhanced_path: str | None = None
    back_path: str | None = None
    back_type: str | None = None  # blank | watermark_only | handwriting | stamp | sticker
    keep_back: bool = True
    # Non-destructive edits applied to previews and exports; archive files stay untouched.
    front_crop: list | None = Field(default=None, sa_column=Column(JSON))  # [x0, y0, x1, y1] full-res, end-exclusive
    back_crop: list | None = Field(default=None, sa_column=Column(JSON))
    front_rotation: int = 0  # clockwise degrees: 0 | 90 | 180 | 270
    back_rotation: int = 0
    # What the app proposed, per field, with the producer that proposed it: compared with the approved values
    # to write correction events (banana/core/corrections.py).
    suggestions: dict = Field(default_factory=dict, sa_column=Column(JSON))
    ocr_frame: dict | None = Field(default=None, sa_column=Column(JSON))  # {crop, rotation, max_side} OCR boxes refer to

    dhash_hex: str | None = None
    ocr_lines: list = Field(default_factory=list, sa_column=Column(JSON))
    ocr_engine: str | None = None

    date_precision: str = Precision.UNKNOWN.value
    date_year: int | None = None
    date_month: int | None = None
    date_day: int | None = None
    date_season: str | None = None
    date_circa: bool = False
    date_source: str | None = None  # back_ocr | front_imprint | manual | batch

    description: str | None = None
    people: list = Field(default_factory=list, sa_column=Column(JSON))
    places: list = Field(default_factory=list, sa_column=Column(JSON))
    events: list = Field(default_factory=list, sa_column=Column(JSON))
    tags: list = Field(default_factory=list, sa_column=Column(JSON))

    status: str = Field(default=ScanStatus.INGESTED.value, index=True)
    duplicate_group_id: int | None = Field(default=None, index=True)
    operator_duplicate: bool = False  # operator says this scan is a duplicate (Flag duplicate)
    # A separate signal from duplicate_group_id (a *local* rescan of another Scan row): this is "matches
    # something already in the configured Immich library" (banana/immich/dedup.py). Never auto-applied - only
    # ever a suggestion, like everything else, and never conflated with the local-duplicate bookkeeping.
    immich_duplicate_asset_id: str | None = Field(default=None, index=True)
    operator_immich_duplicate: bool = False
    is_keeper: bool = True

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def photo_date(self) -> PhotoDate:
        return PhotoDate(
            Precision(self.date_precision), self.date_year, self.date_month, self.date_day,
            self.date_season, self.date_circa,
        )

    def set_photo_date(self, d: PhotoDate, source: str) -> None:
        self.date_precision = d.precision.value
        self.date_year, self.date_month, self.date_day = d.year, d.month, d.day
        self.date_season, self.date_circa = d.season, d.circa
        self.date_source = source


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    type: str = Field(index=True)
    scan_id: int | None = Field(default=None, foreign_key="scan.id")
    state: str = Field(default="queued", index=True)  # queued | running | done | failed
    attempts: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CorrectionEvent(SQLModel, table=True):
    """Append-only record of one field on one approval: what was suggested vs what the operator committed.
    UPDATE and DELETE are blocked by database triggers (banana/db.py)."""

    __tablename__ = "correction_event"

    id: int | None = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="scan.id", index=True)
    batch: str | None = None
    approval_id: str = Field(index=True)  # groups the events written by one approval
    created_at: datetime = Field(default_factory=utcnow)
    field: str = Field(index=True)  # ocr_line | person | place | event | date | rotation | crop | pairing | duplicate | blank_back | immich_duplicate
    action: str  # kept | edited | removed | added
    suggested: dict | list | str | int | None = Field(default=None, sa_column=Column(JSON))
    approved: dict | list | str | int | None = Field(default=None, sa_column=Column(JSON))
    producer: str  # name@version of the model/rule behind the suggestion ("operator" for operator-only additions)
    asset_ref: dict | None = Field(default=None, sa_column=Column(JSON))


class SettingOverride(SQLModel, table=True):
    """Thresholds the operator explicitly applied from a learning proposal (never written silently)."""

    __tablename__ = "setting_override"

    key: str = Field(primary_key=True)  # e.g. analysis.blank_edge_density
    value: dict | list | str | int | float | None = Field(default=None, sa_column=Column(JSON))
    previous: dict | list | str | int | float | None = Field(default=None, sa_column=Column(JSON))
    evidence: dict | None = Field(default=None, sa_column=Column(JSON))
    applied_at: datetime = Field(default_factory=utcnow)


class Export(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="scan.id", unique=True)
    front_rel: str
    front_sha256: str
    front_sha1: str | None = None  # what Immich's bulk-upload-check compares by (it hashes with SHA1, not 256)
    back_rel: str | None = None
    back_sha256: str | None = None
    back_sha1: str | None = None
    exported_at: datetime = Field(default_factory=utcnow)
    immich_front_id: str | None = None  # set once bulk-upload-check confirms this exact file reached Immich
    immich_back_id: str | None = None
    immich_checked_at: datetime | None = None  # distinguishes "never checked" from "checked, no match"
    stack_id: str | None = None


class ImmichSetting(SQLModel, table=True):
    """Operator-entered Immich connection, layered over config.toml's [immich] block. Singleton row (id=1).
    Not SettingOverride: that table is shaped for one learning-loop threshold with a revert, and is dumped
    unmasked by GET /api/learning - reusing it here would leak the API key through an unrelated endpoint."""

    __tablename__ = "immich_setting"

    id: int = Field(default=1, primary_key=True)
    enabled: bool = False
    url: str | None = None
    api_key: str | None = None
    library_id: str | None = None
    import_path_prefix: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    """An operator login. Passwords are stored only as scrypt hashes (banana/auth.py)."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    disabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class AuthSession(SQLModel, table=True):
    """A signed-in browser. Only the SHA-256 of the cookie token is stored, so a copied database can't be
    used to impersonate anyone, and deleting the row signs that browser out."""

    __tablename__ = "auth_session"

    token_hash: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class ImmichAssetHash(SQLModel, table=True):
    """One cached dHash per Immich asset (banana/immich/dedup.py), keyed by Immich's own reported checksum so
    a re-run only re-fetches/re-hashes assets that actually changed."""

    __tablename__ = "immich_asset_hash"

    asset_id: str = Field(primary_key=True)
    checksum: str | None = None
    dhash_hex: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
