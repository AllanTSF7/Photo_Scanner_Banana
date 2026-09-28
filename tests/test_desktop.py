"""banana.desktop: first-run config generation for the packaged build. No real network or app dir touched."""

from __future__ import annotations

import importlib

import banana.desktop as desktop


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


def test_ensure_config_falls_back_to_path_exiftool_when_not_bundled(tmp_path, monkeypatch):
    mod = _reload_with_env(monkeypatch, tmp_path)
    text = mod._ensure_config(tmp_path / "app").read_text(encoding="utf-8")
    assert 'path = "exiftool"' in text
