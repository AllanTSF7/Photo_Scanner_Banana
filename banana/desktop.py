"""Entry point for the packaged desktop build (PyInstaller one-folder bundle; see docs/HANDOFF.md).

Per-device layout: config and data live under %LOCALAPPDATA%\\PhotoScanner\\, separate from the program
folder, so replacing the program on update never touches them. Binds to 127.0.0.1 only unless the operator
opts into a wider address (e.g. a Tailscale IP) by editing the generated config - `banana serve` itself
still defaults to 0.0.0.0 for the server/dev case, that default is deliberately not carried over here.

The app opens as its own window, not a browser tab: a native window (pywebview over the Edge WebView2
runtime that ships with Windows 11) on the main thread, with the web server in a background thread.
Closing that window ends the app. Without pywebview or WebView2 it falls back to an Edge/Chrome "app mode"
window (no tabs, no address bar), and only then to the default browser. PHOTOSCANNER_BROWSER=1 forces the
browser, for debugging.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8420  # unlikely to collide with anything else already on the machine
WINDOW_TITLE = "Photo Scanner"
WINDOW_SIZE = (1440, 960)
WINDOW_MIN_SIZE = (400, 600)  # the UI is built to stay usable down to 400 px

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
# One computer, one person, reachable only from this machine: no sign-in. Set required = true (and create an
# account with the setup page) before opening the app to other machines.
required = false
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


def _set_string(config_text: str, section_name: str, key: str, value: str) -> str:
    """Replace only the `key = "..."` line inside [section]; everything else the operator wrote is kept.
    The key is added to the section, or the section to the file, when missing."""
    lines, section, done = config_text.splitlines(keepends=True), None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == section_name and not done:
                lines.insert(i, f'{key} = "{value}"\n')
                done = True
                break
            section = stripped[1:-1].strip()
        elif section == section_name and stripped.split("=")[0].strip() == key:
            lines[i] = f'{key} = "{value}"\n'
            done = True
            break
    if not done:
        if section != section_name:
            lines.append(f"\n[{section_name}]\n")
        lines.append(f'{key} = "{value}"\n')
    return "".join(lines)


def _set_scanner_host(config_text: str, host: str) -> str:
    return _set_string(config_text, "scanner", "host", host)


def _ensure_exiftool_path(config_path: Path, app_dir: Path) -> str | None:
    """A config from an earlier install names that install's bundled ExifTool by absolute path. When the program
    folder is replaced somewhere else, that path is dead and every export would fail. Point it at the ExifTool
    shipped with this copy. A path that still resolves (or a bare command on PATH) is left alone.
    Returns the path written, or None when nothing changed."""
    import tomllib

    bundled = app_dir / "tools" / "exiftool" / "exiftool.exe"
    if not bundled.exists():
        return None
    try:
        text = config_path.read_text(encoding="utf-8")
        current = str(tomllib.loads(text).get("exiftool", {}).get("path", "") or "")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if current and (Path(current).exists() or shutil.which(current)):
        return None
    config_path.write_text(_set_string(text, "exiftool", "path", bundled.as_posix()), encoding="utf-8")
    return bundled.as_posix()


def _ensure_auth_section(config_path: Path) -> None:
    """Configs written by earlier desktop builds either have no [auth] section or one without `required`.
    A desktop install is one computer and one person, so sign-in is off by default: a missing section is
    added, and a missing `required` key is added as false. A value the operator wrote is never changed."""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not any(line.strip() == "[auth]" for line in lines):
        config_path.write_text(text.rstrip("\n") + "\n\n[auth]\nrequired = false\nsetup_page = true\n", encoding="utf-8")
        return
    section = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
        elif section == "auth" and stripped.split("=")[0].strip() == "required":
            return  # the operator decided; leave it
    index = next(i for i, line in enumerate(lines) if line.strip() == "[auth]")
    lines.insert(index + 1, "required = false")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def _wait_ready(host: str, port: int, alive=lambda: True, timeout: float = 20.0) -> bool:
    """True once the server answers on its port; False if `alive()` reports the server gone or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        if not alive():
            return False
        time.sleep(0.2)
    return False


def _browser_forced() -> bool:
    return os.environ.get("PHOTOSCANNER_BROWSER") == "1"


def _icon_path() -> Path | None:
    """The scanner icon (built by scripts/make_icon.py), shipped with the static assets so it is in the same place
    in a dev checkout and in the packaged bundle."""
    icon = _app_dir() / "banana" / "web" / "static" / "icon.ico"
    return icon if icon.exists() else None


APP_USER_MODEL_ID = "PhotoScanner.Desktop"


def _claim_taskbar_identity() -> None:
    """Give the process its own taskbar identity. Without this Windows groups the window under, and shows the icon
    of, whatever exe hosts it (python.exe in a dev run) and pins that exe instead of the app."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001 - cosmetic; never worth failing the launch over
        pass


def _run_native_window(url: str) -> bool:
    """Show the app in a native window and block until it is closed. Must run on the main thread (WebView2).
    Returns False, having shown nothing, when pywebview or the WebView2 runtime is unavailable, so the
    caller can fall back to a browser window."""
    if _browser_forced():
        return False
    try:
        import webview
    except ImportError:
        return False
    _claim_taskbar_identity()
    try:
        webview.create_window(
            WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=WINDOW_MIN_SIZE, text_select=True,
        )
        # private_mode=False keeps the sign-in cookie and the page's remembered choices between launches. The icon
        # is what the title bar and taskbar show; without it the window wears the host exe's icon, which in a dev
        # run is Python's.
        icon = _icon_path()
        webview.start(private_mode=False, storage_path=str(_data_home() / "webview"), icon=str(icon) if icon else None)
    except Exception:  # noqa: BLE001 - no WebView2 runtime, no desktop session: a browser window still works
        return False
    return True


def _app_mode_browser() -> str | None:
    """Edge or Chrome, whichever is installed: both open a URL as its own chrome-less window with --app."""
    program_files = [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")]
    relative = ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"]
    for base in filter(None, program_files):
        for rel in relative:
            candidate = Path(base) / rel
            if candidate.exists():
                return str(candidate)
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _show_in_browser(url: str) -> str:
    """Fallback window: an Edge/Chrome app-mode window (its own window, no tabs or address bar), else the
    default browser. Returns which one was used."""
    browser = None if _browser_forced() else _app_mode_browser()
    if browser:
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "app"
        except OSError:
            pass
    webbrowser.open(url)
    return "browser"


def _show(url: str) -> str:
    """Open the running app in its own window: native if possible, otherwise a browser window."""
    if _run_native_window(url):
        return "native"
    return _show_in_browser(url)


def main() -> None:
    app_dir = _app_dir()
    _redirect_streams_if_windowed(_data_home() / "logs")
    host, port = "127.0.0.1", DEFAULT_PORT
    url = f"http://{host}:{port}/"

    if _port_open(host, port):
        # Already running (a second launch, e.g. double-clicking the exe again): just show it.
        _show(url)
        return

    config_path = _ensure_config(app_dir)
    _ensure_auth_section(config_path)
    _ensure_exiftool_path(config_path, app_dir)
    try:
        _ensure_scanner_host(config_path)
    except Exception:  # noqa: BLE001 - a scanner lookup failure must never stop the app from starting
        pass
    os.environ.setdefault("BANANA_CONFIG", str(config_path))

    import uvicorn

    # The server runs in a background thread so the window can own the main thread (WebView2 requires it).
    server = uvicorn.Server(uvicorn.Config("banana.web.api:app", host=host, port=port, log_level="info"))
    thread = threading.Thread(target=server.run, name="photoscanner-server", daemon=True)
    thread.start()
    if not _wait_ready(host, port, alive=thread.is_alive):
        return  # the server failed to start; its error is in the log
    if _run_native_window(url):
        server.should_exit = True  # closing the window ends the app
        thread.join(10)
        return
    _show_in_browser(url)
    thread.join()  # browser fallback: keep serving until the process is ended


if __name__ == "__main__":
    main()
