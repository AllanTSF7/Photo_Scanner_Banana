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


@cli.command("repair-duplicates")
def repair_duplicates(config: Path | None = typer.Option(None)) -> None:
    """Fix any `duplicate_group_id` left pointing at a deleted scan (e.g. from before this repair existed).
    Safe to run any time; a no-op when there's nothing dangling."""
    from banana.ingest.service import repair_dangling_duplicate_groups

    settings = load_settings(config)
    engine = db.make_engine(settings.db_path)
    with db.session(engine) as session:
        changed = repair_dangling_duplicate_groups(session)
        session.commit()
    typer.echo(f"{changed} scan(s) repaired" if changed else "nothing to repair")


users = typer.Typer(no_args_is_help=True, help="Operator accounts")
cli.add_typer(users, name="user")


def _read_password(password_stdin: bool) -> str:
    if password_stdin:
        import sys

        return sys.stdin.readline().rstrip("\r\n")
    return typer.prompt("Password", hide_input=True, confirmation_prompt=True)


def _user_session(config: Path | None):
    return db.session(db.make_engine(load_settings(config).db_path))


@users.command("add")
def user_add(
    username: str,
    password_stdin: bool = typer.Option(False, help="Read the password from stdin instead of prompting"),
    config: Path | None = typer.Option(None),
) -> None:
    """Create an account. Prompts for the password so it never lands in shell history."""
    from banana import auth

    with _user_session(config) as session:
        try:
            user = auth.create_user(session, username, _read_password(password_stdin))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"created {user.username}")


@users.command("passwd")
def user_passwd(
    username: str,
    password_stdin: bool = typer.Option(False, help="Read the password from stdin instead of prompting"),
    config: Path | None = typer.Option(None),
) -> None:
    """Set a new password (signs that user out everywhere)."""
    from banana import auth

    with _user_session(config) as session:
        try:
            auth.set_password(session, username, _read_password(password_stdin))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"password changed for {username}")


@users.command("disable")
def user_disable(username: str, config: Path | None = typer.Option(None)) -> None:
    """Block an account and sign it out everywhere."""
    from banana import auth

    with _user_session(config) as session:
        auth.set_disabled(session, username, True)
    typer.echo(f"disabled {username}")


@users.command("enable")
def user_enable(username: str, config: Path | None = typer.Option(None)) -> None:
    from banana import auth

    with _user_session(config) as session:
        auth.set_disabled(session, username, False)
    typer.echo(f"enabled {username}")


@users.command("list")
def user_list(config: Path | None = typer.Option(None)) -> None:
    from sqlmodel import select

    from banana.models import User

    with _user_session(config) as session:
        for user in session.exec(select(User).order_by(User.username)):
            typer.echo(f"{user.username}{'  (disabled)' if user.disabled else ''}")


@cli.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Loopback by default; pass --host explicitly (e.g. the Tailscale IP) to listen elsewhere."""
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
