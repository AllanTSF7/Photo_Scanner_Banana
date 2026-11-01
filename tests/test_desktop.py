"""banana.desktop: first-run config generation for the packaged build. No real network or app dir touched."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest

import banana.desktop as desktop

# Windows-only by design: _user_docs_root()/_data_home() build Windows-style paths (SYSTEMDRIVE,
# LOCALAPPDATA), which pathlib's POSIX semantics can't round-trip correctly on Linux/macOS.
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="banana.desktop targets the Windows packaged build only")


def _reload_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "C:").rstrip(":"))
    return importlib.reload(desktop)


def test_ensure_config_creates_folders_and_a_usable_config(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    config_path = mod._ensure_config(tmp_path / "app")

    assert config_path.exists()
    text = config_path.read_text(encoding="utf-8")
    assert 'host = ""' in text  # no scanner configured by default: Ingest inbox is the packaged workflow

    for name in ("inbox", "archive", "sorted"):
        assert (mod._user_docs_root() / name).is_dir()
    assert (mod._data_home() / "data").is_dir()

    from banana.config import Settings

    settings = Settings.model_validate(__import__("tomllib").loads(text))
    assert settings.paths.inbox == mod._user_docs_root() / "inbox"
    assert settings.scanner.host == ""


def test_ensure_config_never_overwrites_an_existing_one(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    first = mod._ensure_config(tmp_path / "app")
    first.write_text("# operator's own edits\n" + first.read_text(encoding="utf-8"), encoding="utf-8")

    second = mod._ensure_config(tmp_path / "app")
    assert second == first
    assert second.read_text(encoding="utf-8").startswith("# operator's own edits")


def test_ensure_config_points_at_the_bundled_exiftool_when_present(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    app_dir = tmp_path / "app"
    exiftool = app_dir / "tools" / "exiftool" / "exiftool.exe"
    exiftool.parent.mkdir(parents=True)
    exiftool.write_bytes(b"")

    text = mod._ensure_config(app_dir).read_text(encoding="utf-8")
    assert exiftool.as_posix() in text


def test_redirect_streams_replaces_none_stdout_stderr_stdin(tmp_path, monkeypatch):
    # A PyInstaller windowed build (console=False - what this bundle uses) hands the process actually-None
    # stdout/stderr/stdin, not just closed streams. Real bug: uvicorn's logging setup calls
    # sys.stdout.isatty() at startup and crashed with "Unable to configure formatter 'default'" before the
    # server ever came up - caught only by launching the real exe with no redirection, the way an operator
    # actually double-clicks it (this session's earlier smoke test used Start-Process redirection, which
    # gives the child process real streams and silently hid the exact bug a real launch hits).
    monkeypatch.setattr(desktop.sys, "stdout", None, raising=False)
    monkeypatch.setattr(desktop.sys, "stderr", None, raising=False)
    monkeypatch.setattr(desktop.sys, "stdin", None, raising=False)

    log_dir = tmp_path / "logs"
    desktop._redirect_streams_if_windowed(log_dir)

    assert desktop.sys.stdout is not None and hasattr(desktop.sys.stdout, "isatty")
    assert desktop.sys.stdout.isatty() is False
    assert desktop.sys.stderr is not None
    assert desktop.sys.stdin is not None
    assert (log_dir / "photoscanner.log").exists()


def test_redirect_streams_leaves_real_streams_alone(tmp_path, monkeypatch):
    real_stdout = desktop.sys.stdout
    desktop._redirect_streams_if_windowed(tmp_path / "logs")
    assert desktop.sys.stdout is real_stdout
    assert not (tmp_path / "logs").exists()  # never even created when nothing needed redirecting


def test_scanner_host_is_filled_in_from_discovery_and_nothing_else_changes(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    config = mod._ensure_config(tmp_path / "app")
    before = config.read_text(encoding="utf-8")

    host = mod._ensure_scanner_host(config, find=lambda: {"host": "192.168.16.178"})
    assert host == "192.168.16.178"
    after = config.read_text(encoding="utf-8")
    assert 'host = "192.168.16.178"' in after
    assert after.replace('host = "192.168.16.178"', 'host = ""') == before  # only that one line changed


def test_scanner_host_is_refreshed_when_dhcp_moved_the_scanner(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    config = mod._ensure_config(tmp_path / "app")
    config.write_text(mod._set_scanner_host(config.read_text(encoding="utf-8"), "10.255.255.1"), encoding="utf-8")
    monkeypatch.setattr(mod, "_port_open", lambda host, port: False)  # old address no longer answers

    assert mod._ensure_scanner_host(config, find=lambda: {"host": "192.168.16.200"}) == "192.168.16.200"
    assert 'host = "192.168.16.200"' in config.read_text(encoding="utf-8")


def test_scanner_host_kept_when_it_still_answers_and_when_nothing_is_found(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    config = mod._ensure_config(tmp_path / "app")
    config.write_text(mod._set_scanner_host(config.read_text(encoding="utf-8"), "192.168.16.178"), encoding="utf-8")

    monkeypatch.setattr(mod, "_port_open", lambda host, port: True)
    assert mod._ensure_scanner_host(config, find=lambda: pytest.fail("no lookup when it answers")) == "192.168.16.178"

    monkeypatch.setattr(mod, "_port_open", lambda host, port: False)
    assert mod._ensure_scanner_host(config, find=lambda: None) == "192.168.16.178"  # keep, don't blank it


def test_new_configs_open_first_account_setup(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    import tomllib

    config = mod._ensure_config(tmp_path / "app")
    assert tomllib.loads(config.read_text(encoding="utf-8"))["auth"]["setup_page"] is True


def test_older_configs_without_auth_get_setup_added_but_operator_auth_is_left_alone(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    import tomllib

    old = tmp_path / "old.toml"
    old.write_text('[paths]\ninbox = "x"\n[scanner]\nhost = ""\n', encoding="utf-8")
    mod._ensure_auth_section(old)
    assert tomllib.loads(old.read_text(encoding="utf-8"))["auth"] == {"required": False, "setup_page": True}

    # An [auth] the operator wrote keeps every value; only the missing `required` is filled in (desktop = no sign-in).
    mine = tmp_path / "mine.toml"
    mine.write_text('[auth]\nsetup_page = false\n', encoding="utf-8")
    mod._ensure_auth_section(mine)
    assert mine.read_text(encoding="utf-8") == '[auth]\nrequired = false\nsetup_page = false\n'


def test_set_scanner_host_adds_a_scanner_section_when_missing():
    text = '[paths]\ninbox = "x"\n'
    assert desktop._set_scanner_host(text, "1.2.3.4").endswith('[scanner]\nhost = "1.2.3.4"\n')


def test_app_dir_resolves_to_meipass_when_frozen_not_the_exe_folder(tmp_path, monkeypatch):
    # PyInstaller 6+ one-folder builds put bundled data (exiftool, static assets) under _internal/,
    # not beside the exe - a real bug caught here: _app_dir() used to return the exe's own folder,
    # which pointed at the wrong place and made the packaged build unable to find exiftool at all.
    mod = _reload_with_env(monkeypatch, tmp_path)
    meipass = tmp_path / "dist" / "PhotoScanner" / "_internal"
    exe_dir = tmp_path / "dist" / "PhotoScanner"
    monkeypatch.setattr(mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mod.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(mod.sys, "executable", str(exe_dir / "PhotoScanner.exe"), raising=False)

    assert mod._app_dir() == meipass


def test_ensure_config_falls_back_to_path_exiftool_when_not_bundled(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    text = mod._ensure_config(tmp_path / "app").read_text(encoding="utf-8")
    assert 'path = "exiftool"' in text


def test_new_desktop_configs_switch_sign_in_off(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    text = mod._ensure_config(tmp_path / "app").read_text(encoding="utf-8")
    from banana.config import Settings

    settings = Settings.model_validate(__import__("tomllib").loads(text))
    assert settings.auth.required is False  # one computer, one person: no accounts


def test_older_desktop_configs_get_required_false_added_unless_the_operator_chose(tmp_path, monkeypatch):
    import tomllib

    mod = _reload_with_env(monkeypatch, tmp_path)
    config = tmp_path / "config.toml"
    config.write_text('[paths]\ninbox = "x"\n\n[auth]\nsetup_page = true\nsession_days = 7\n', encoding="utf-8")
    mod._ensure_auth_section(config)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["auth"] == {"required": False, "setup_page": True, "session_days": 7}

    config.write_text('[auth]\nrequired = true\n', encoding="utf-8")
    mod._ensure_auth_section(config)
    assert tomllib.loads(config.read_text(encoding="utf-8"))["auth"] == {"required": True}  # the operator's choice stays


def test_native_window_is_skipped_without_pywebview_or_when_the_browser_is_forced(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "webview", None)  # import fails
    assert mod._run_native_window("http://127.0.0.1:1/") is False

    calls = []
    fake = type("webview", (), {"create_window": lambda *a, **k: calls.append(("create", a, k)),
                                "start": lambda *a, **k: calls.append(("start", a, k))})
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setenv("PHOTOSCANNER_BROWSER", "1")
    assert mod._run_native_window("http://127.0.0.1:1/") is False
    assert calls == []


def test_native_window_is_skipped_without_the_webview2_runtime(tmp_path, monkeypatch):
    """Without the runtime, pywebview would fail inside its own event loop (an error dialog, or nothing); the launcher
    checks first and goes straight to the Edge/Chrome app window instead."""
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PHOTOSCANNER_BROWSER", raising=False)
    calls = []
    fake = type("webview", (), {"create_window": lambda *a, **k: calls.append("create"), "start": lambda *a, **k: calls.append("start")})
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(mod, "_webview2_available", lambda: False)
    assert mod._run_native_window("http://127.0.0.1:1/") is False
    assert calls == []
    assert isinstance(desktop._webview2_available(), bool)  # the real check answers on this machine


def test_native_window_runs_pywebview_with_persistent_storage(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PHOTOSCANNER_BROWSER", raising=False)
    monkeypatch.setattr(mod, "_webview2_available", lambda: True)
    calls = []
    fake = type("webview", (), {"create_window": lambda *a, **k: calls.append(("create", a, k)),
                                "start": lambda *a, **k: calls.append(("start", a, k))})
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert mod._run_native_window("http://127.0.0.1:1/") is True
    (create, start) = calls
    assert create[1][:2] == ("Photo Scanner", "http://127.0.0.1:1/")
    assert create[2]["min_size"] == (400, 600)
    assert start[2]["private_mode"] is False  # sign-in cookie and remembered choices survive a relaunch
    assert start[2]["storage_path"].startswith(str(mod._data_home()))
    assert start[2]["icon"].endswith("icon.ico") and Path(start[2]["icon"]).exists()  # the window wears the app icon, not Python's


def test_native_window_failure_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_webview2_available", lambda: True)
    monkeypatch.delenv("PHOTOSCANNER_BROWSER", raising=False)

    def boom(*a, **k):
        raise RuntimeError("no WebView2 runtime")

    monkeypatch.setitem(sys.modules, "webview", type("webview", (), {"create_window": boom, "start": boom}))
    assert mod._run_native_window("http://127.0.0.1:1/") is False


def test_browser_fallback_prefers_an_app_mode_window(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PHOTOSCANNER_BROWSER", raising=False)
    launched, opened = [], []
    monkeypatch.setattr(mod, "_app_mode_browser", lambda: r"C:\edge\msedge.exe")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args, **k: launched.append(args))
    monkeypatch.setattr(mod.webbrowser, "open", lambda url: opened.append(url))
    assert mod._show_in_browser("http://127.0.0.1:1/") == "app"
    assert launched == [[r"C:\edge\msedge.exe", "--app=http://127.0.0.1:1/", "--window-size=1440,960"]]
    assert opened == []

    monkeypatch.setattr(mod, "_app_mode_browser", lambda: None)
    assert mod._show_in_browser("http://127.0.0.1:1/") == "browser"
    assert opened == ["http://127.0.0.1:1/"]

    monkeypatch.setattr(mod, "_app_mode_browser", lambda: r"C:\edge\msedge.exe")
    monkeypatch.setenv("PHOTOSCANNER_BROWSER", "1")
    assert mod._show_in_browser("http://127.0.0.1:1/") == "browser"


def test_app_mode_browser_finds_edge_under_program_files(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    edge = tmp_path / "pf" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "pf"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "nothing"))
    assert mod._app_mode_browser() == str(edge)
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "nothing"))
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod._app_mode_browser() is None


def test_wait_ready_gives_up_when_the_server_thread_dies(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_port_open", lambda host, port: False)
    assert mod._wait_ready("127.0.0.1", 1, alive=lambda: False, timeout=5) is False
    monkeypatch.setattr(mod, "_port_open", lambda host, port: True)
    assert mod._wait_ready("127.0.0.1", 1) is True


def test_dead_exiftool_path_from_a_moved_install_is_repointed_at_the_bundled_copy(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    import tomllib

    app = tmp_path / "new-program-folder"
    bundled = app / "tools" / "exiftool" / "exiftool.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    config = tmp_path / "config.toml"
    config.write_text(
        '[paths]\ninbox = "x"\n\n[exiftool]\n# where the old install kept it\n'
        'path = "C:/Old/PhotoScanner/_internal/tools/exiftool/exiftool.exe"\n\n[scanner]\nhost = ""\n',
        encoding="utf-8",
    )
    assert mod._ensure_exiftool_path(config, app) == bundled.as_posix()
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["exiftool"]["path"] == bundled.as_posix()
    assert parsed["paths"] == {"inbox": "x"} and parsed["scanner"] == {"host": ""}  # nothing else touched
    assert "# where the old install kept it" in config.read_text(encoding="utf-8")

    # Second launch: the path now resolves, so it is left alone.
    assert mod._ensure_exiftool_path(config, app) is None


def test_a_working_exiftool_path_or_command_is_left_alone(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    app = tmp_path / "program"
    (app / "tools" / "exiftool").mkdir(parents=True)
    (app / "tools" / "exiftool" / "exiftool.exe").write_bytes(b"")

    own = tmp_path / "my-exiftool.exe"
    own.write_bytes(b"")
    config = tmp_path / "config.toml"
    config.write_text(f'[exiftool]\npath = "{own.as_posix()}"\n', encoding="utf-8")
    assert mod._ensure_exiftool_path(config, app) is None

    monkeypatch.setattr(mod.shutil, "which", lambda name: str(own) if name == "exiftool" else None)
    config.write_text('[exiftool]\npath = "exiftool"\n', encoding="utf-8")
    assert mod._ensure_exiftool_path(config, app) is None
    assert config.read_text(encoding="utf-8") == '[exiftool]\npath = "exiftool"\n'

    # A config with no [exiftool] section at all (the server default) gets one pointing at the bundled copy.
    config.write_text('[paths]\ninbox = "x"\n', encoding="utf-8")
    assert mod._ensure_exiftool_path(config, app) is not None
    assert "[exiftool]" in config.read_text(encoding="utf-8")

    # No bundled ExifTool (a source checkout): never touched.
    config.write_text('[exiftool]\npath = "C:/nowhere/exiftool.exe"\n', encoding="utf-8")
    assert mod._ensure_exiftool_path(config, tmp_path / "no-such-app") is None
