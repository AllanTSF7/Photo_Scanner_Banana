"""Entry point for the packaged desktop build (PyInstaller one-folder bundle; see docs/HANDOFF.md).

Per-device layout: config and data live under %LOCALAPPDATA%\\PhotoScanner\\, separate from the program
folder, so replacing the program on update never touches them. Binds to 127.0.0.1 only unless the operator
opts into a wider address (e.g. a Tailscale IP) by editing the generated config - `banana serve` itself
still defaults to 0.0.0.0 for the server/dev case, that default is deliberately not carried over here.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8420  # unlikely to collide with anything else already on the machine

_CONFIG_TEMPLATE = """\
# Generated on first run. Safe to edit; the app never overwrites an existing config.toml.
[paths]
inbox = "{inbox}"
archive = "{archive}"
sorted = "{sorted_}"
data_dir = "{data_dir}"

[exiftool]
path = "{exiftool}"

[auth]
# The first launch offers a "create the first account" page; it closes as soon as one account exists.
setup_page = true

[scanner]
# Filled in automatically: on every launch the app looks the Epson up by name on the network and updates
# this if the scanner's address changed. Scanning uses Epson's own driver (Epson Scan 2), which must be installed.
host = ""
"""


def _app_dir() -> Path:
    """Where bundled data (exiftool, static assets) actually lives: PyInstaller's `_MEIPASS` in a
    packaged build - the `_internal/` folder for a one-folder build, a temp extraction dir for
    one-file - else the repo root. NOT simply the exe's own folder: PyInstaller 6+ nests one-folder
    data under `_internal/` rather than beside the exe, and this must match wherever the spec's
    `datas` entries actually land."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _data_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "PhotoScanner"


def _user_docs_root() -> Path:
    return Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\") / "PhotoScanner"


def _ensure_config(app_dir: Path) -> Path:
    home = _data_home()
    config_dir = home / "config"
    config_path = config_dir / "config.toml"
    if config_path.exists():
        return config_path
    config_dir.mkdir(parents=True, exist_ok=True)
    docs = _user_docs_root()
    for name in ("inbox", "archive", "sorted"):
        (docs / name).mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    exiftool = app_dir / "tools" / "exiftool" / "exiftool.exe"
    config_path.write_text(
        _CONFIG_TEMPLATE.format(
            inbox=(docs / "inbox").as_posix(), archive=(docs / "archive").as_posix(),
            sorted_=(docs / "sorted").as_posix(), data_dir=(home / "data").as_posix(),
            exiftool=exiftool.as_posix() if exiftool.exists() else "exiftool",
        ),
        encoding="utf-8",
    )
    return config_path


def _redirect_streams_if_windowed(log_dir: Path) -> None:
    """A PyInstaller windowed build (console=False, what this bundle uses so nothing flashes a
    terminal on launch) sets sys.stdout/stderr/stdin to actually None, not just closed. Nothing that
    assumes a real stream survives that - uvicorn's own logging setup calls sys.stdout.isatty() and
    crashes with "Unable to configure formatter 'default'" before the server ever starts. Redirecting
    to a log file both fixes that and gives us something to look at from a machine we can't see."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "photoscanner.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or log_file
    sys.stderr = sys.stderr or log_file
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def _set_scanner_host(config_text: str, host: str) -> str:
    """Replace only the `host = "..."` line inside [scanner]; everything else the operator wrote is kept."""
    lines, section, done = config_text.splitlines(keepends=True), None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "scanner" and not done:
                lines.insert(i, f'host = "{host}"\n')
                done = True
                break
            section = stripped[1:-1].strip()
        elif section == "scanner" and stripped.split("=")[0].strip() == "host":
            lines[i] = f'host = "{host}"\n'
            done = True
            break
    if not done:
        if section != "scanner":
            lines.append("\n[scanner]\n")
        lines.append(f'host = "{host}"\n')
    return "".join(lines)


def _ensure_auth_section(config_path: Path) -> None:
    """Configs written by builds from before accounts existed have no [auth] section; without the setup page
    the operator of a desktop install would have no way to create the first account. Only adds a missing
    section - an operator's own [auth] settings are never changed."""
    text = config_path.read_text(encoding="utf-8")
    if not any(line.strip() == "[auth]" for line in text.splitlines()):
        config_path.write_text(text.rstrip("\n") + "\n\n[auth]\nsetup_page = true\n", encoding="utf-8")


def _ensure_scanner_host(config_path: Path, find=None) -> str | None:
    """Keep a configured scanner address that answers; otherwise look the scanner up by name on the
    network (mDNS) and write the address it's at now. Returns the host in use, or None."""
    import tomllib

    try:
        current = tomllib.loads(config_path.read_text(encoding="utf-8")).get("scanner", {}).get("host", "")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if current and _port_open(current, 1865):
        return current
    if find is None:
        from banana.scanner.discover import find_scanners, pick_epson

        def find():
            return pick_epson(find_scanners())
    found = find()
    if not found:
        return current or None
    if found["host"] != current:
        config_path.write_text(_set_scanner_host(config_path.read_text(encoding="utf-8"), found["host"]), encoding="utf-8")
    return found["host"]


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _open_browser_when_ready(url: str, host: str, port: int) -> None:
    for _ in range(100):  # ~20s
        if _port_open(host, port):
            webbrowser.open(url)
            return
        time.sleep(0.2)


def main() -> None:
    app_dir = _app_dir()
    _redirect_streams_if_windowed(_data_home() / "logs")
    host, port = "127.0.0.1", DEFAULT_PORT
    url = f"http://{host}:{port}/"

    if _port_open(host, port):
        # Already running (a second launch, e.g. double-clicking the exe again): just show it.
        webbrowser.open(url)
        return

    config_path = _ensure_config(app_dir)
    _ensure_auth_section(config_path)
    try:
        _ensure_scanner_host(config_path)
    except Exception:  # noqa: BLE001 - a scanner lookup failure must never stop the app from starting
        pass
    os.environ.setdefault("BANANA_CONFIG", str(config_path))

    import uvicorn

    threading.Thread(target=_open_browser_when_ready, args=(url, host, port), daemon=True).start()
    uvicorn.run("banana.web.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
