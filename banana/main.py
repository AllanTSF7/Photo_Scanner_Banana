from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import typer

from banana import core, db, jobs
from banana.config import load_settings

cli = typer.Typer(no_args_is_help=True, help="Photo Scanner")


@cli.command()
def doctor(config: Path | None = typer.Option(None, help="Config TOML path")) -> None:
    """Report which runtime pieces are available."""
    settings = load_settings(config)
    typer.echo(f"native banana_core : {'yes' if core.NATIVE_AVAILABLE else 'no (NumPy reference)'}")
    exiftool = shutil.which(settings.exiftool.path)
    typer.echo(f"exiftool           : {exiftool or 'MISSING'}")
    for name, path in settings.paths:
        typer.echo(f"{name:<19}: {path} ({'ok' if Path(path).exists() else 'missing'})")


@cli.command()
def export(
    manual_json: Path = typer.Option(..., "--manual-json", exists=True, help="Batch spec (see docs/manual-export.md)"),
    config: Path | None = typer.Option(None),
) -> None:
    """Import a hand-written batch spec and export it to the library (Phase 2 check against Immich)."""
    from banana.export.exiftool_writer import ExifToolWriter
    from banana.export.exporter import Exporter
    from banana.export.service import ManualBatch, export_scan, import_manual

    settings = load_settings(config)
    spec = ManualBatch.model_validate(json.loads(manual_json.read_text(encoding="utf-8-sig")))
    engine = db.make_engine(settings.db_path)
    with db.session(engine) as session, ExifToolWriter(settings.exiftool.path) as writer:
        exporter = Exporter(settings.paths.sorted, writer)
        scans = import_manual(session, spec, manual_json.parent, settings.dates.two_digit_year_pivot)
        for scan in scans:
            record = export_scan(session, scan, exporter)
            typer.echo(f"SCAN {scan.id:06d}: {record.front_rel}" + (f" + {record.back_rel}" if record.back_rel else ""))


@cli.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run("banana.web.api:app", host=host, port=port)


@cli.command()
def worker(config: Path | None = typer.Option(None)) -> None:
    settings = load_settings(config)
    engine = db.make_engine(settings.db_path)
    stop = threading.Event()
    try:
        jobs.run_worker(engine, stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    cli()
