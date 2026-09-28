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

[scanner]
# Left blank: this build expects photos to arrive via the scanner's own software into the inbox folder
# above, then "Ingest inbox" in the app. Fill in host = "<ip>" only if this PC drives the scanner directly.
host = ""
"""


def _app_dir() -> Path:
    """The program's own folder: the PyInstaller bundle dir in a packaged build, else the repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
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
    host, port = "127.0.0.1", DEFAULT_PORT
    url = f"http://{host}:{port}/"

    if _port_open(host, port):
        # Already running (a second launch, e.g. double-clicking the exe again): just show it.
        webbrowser.open(url)
        return

    os.environ.setdefault("BANANA_CONFIG", str(_ensure_config(app_dir)))

    import uvicorn

    threading.Thread(target=_open_browser_when_ready, args=(url, host, port), daemon=True).start()
    uvicorn.run("banana.web.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
