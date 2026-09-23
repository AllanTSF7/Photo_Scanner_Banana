"""Effective Immich configuration: config.toml's [immich] block, overridden field-by-field by whatever the
operator has saved through the UI (`ImmichSetting`, a singleton DB row - see banana/models.py for why this
isn't config.toml itself or the SettingOverride table).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session

from banana.config import Settings
from banana.models import ImmichSetting, utcnow


@dataclass(frozen=True)
class EffectiveImmichConfig:
    enabled: bool
    url: str
    api_key: str
    library_id: str
    import_path_prefix: str


def _row(session: Session) -> ImmichSetting | None:
    return session.get(ImmichSetting, 1)


def get_effective(session: Session, settings: Settings) -> EffectiveImmichConfig:
    cfg = settings.immich
    row = _row(session)
    if row is None:
        return EffectiveImmichConfig(cfg.enabled, cfg.url, cfg.api_key, cfg.library_id, cfg.import_path_prefix)
    return EffectiveImmichConfig(
        enabled=row.enabled,
        url=row.url if row.url is not None else cfg.url,
        api_key=row.api_key if row.api_key is not None else cfg.api_key,
        library_id=row.library_id if row.library_id is not None else cfg.library_id,
        import_path_prefix=row.import_path_prefix if row.import_path_prefix is not None else cfg.import_path_prefix,
    )


def get_masked(session: Session, settings: Settings) -> dict:
    """For GET responses: never the raw key, just whether one is set and its last 4 characters."""
    effective = get_effective(session, settings)
    key = effective.api_key
    return {
        "enabled": effective.enabled,
        "url": effective.url,
        "library_id": effective.library_id,
        "import_path_prefix": effective.import_path_prefix,
        "api_key_set": bool(key),
        "api_key_last4": key[-4:] if key else None,
    }


def save(
    session: Session, settings: Settings, *,
    enabled: bool | None = None, url: str | None = None, library_id: str | None = None,
    import_path_prefix: str | None = None, api_key: str | None = None,
) -> dict:
    """`api_key=None` leaves it unchanged; `api_key=""` explicitly clears it. Same for the other fields except
    `enabled`, which always takes the given value when passed (it has no "unchanged" sentinel: booleans have no
    spare value, and callers always mean to set it when they pass it)."""
    row = _row(session) or ImmichSetting(id=1, url=settings.immich.url, api_key=settings.immich.api_key,
                                          library_id=settings.immich.library_id,
                                          import_path_prefix=settings.immich.import_path_prefix,
                                          enabled=settings.immich.enabled)
    if enabled is not None:
        row.enabled = enabled
    if url is not None:
        row.url = url
    if library_id is not None:
        row.library_id = library_id
    if import_path_prefix is not None:
        row.import_path_prefix = import_path_prefix
    if api_key is not None:
        row.api_key = api_key
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    return get_masked(session, settings)
