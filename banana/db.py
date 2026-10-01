from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from banana import models  # noqa: F401  (registers tables)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # API and worker share the file
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        # A safety net, not the fix: ingest now holds the write lock for milliseconds per photo. 5 s was too
        # short while it held it for a whole batch (20 "database is locked" saves on 2026-09-30).
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _protect_correction_events(engine)
    return engine


def _protect_correction_events(engine: Engine) -> None:
    """Correction events are append-only: the database itself refuses UPDATE and DELETE."""
    with engine.begin() as conn:
        for op in ("UPDATE", "DELETE"):
            conn.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS correction_event_no_{op.lower()} BEFORE {op} ON correction_event "
                "BEGIN SELECT RAISE(ABORT, 'correction events are append-only'); END"
            )


def _add_missing_columns(engine: Engine) -> None:
    """Minimal forward migration: add columns that exist on the models but not in an older database file."""
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None and column.default.is_scalar else None
                if isinstance(default, bool):
                    default = int(default)
                clause = f" DEFAULT {default!r}" if isinstance(default, (int, float, str)) else ""
                conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}{clause}')


def session(engine: Engine) -> Session:
    return Session(engine, expire_on_commit=False)
