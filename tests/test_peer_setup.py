"""scripts/peer/setup_windows_peer.ps1: the sshd_config edit, run for real through Windows PowerShell with
-ConfigOnly against a sample file (no admin, no service, no account). Windows only."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "peer" / "setup_windows_peer.ps1"
POWERSHELL = shutil.which("powershell")
pytestmark = pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="Windows PowerShell only")

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOa8p4mMisQPDvz3l2ptc9f/CTaHrmaWfJeZ8gjYS+s3 photoscanner@laptop"

# The tail of Windows' shipped sshd_config: it already ends in a Match block, so ours must come after it.
DEFAULT = (
    "# This is the sshd server system-wide configuration file.\n"
    "#Port 22\n"
    "AuthorizedKeysFile\t.ssh/authorized_keys\n"
    "Subsystem\tsftp\tsftp-server.exe\n"
    "\n"
    "Match Group administrators\n"
    "       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\n"
)


def run(config: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-ConfigOnly", "-ConfigFile", str(config), *extra],
        capture_output=True, text=True, timeout=60,
    )


def sample(tmp_path: Path, newline: str = "\n") -> Path:
    config = tmp_path / "sshd_config"
    config.write_bytes(DEFAULT.replace("\n", newline).encode("ascii"))
    return config


def test_adds_one_locked_down_block_after_the_existing_match(tmp_path):
    config = sample(tmp_path)
    result = run(config, "-PublicKey", KEY)
    assert result.returncode == 0, result.stderr
    text = config.read_text(encoding="utf-8")
    assert text.startswith(DEFAULT.rstrip("\n"))  # everything already there is untouched
    assert text.index("Match User photoscanner-relay") > text.index("Match Group administrators")
    for line in ("PasswordAuthentication no", 'ChrootDirectory "C:/PhotoScannerRelay"', "ForceCommand internal-sftp",
                 "AllowTcpForwarding no", "PermitTTY no",
                 "AuthorizedKeysFile __PROGRAMDATA__/ssh/photoscanner_photoscanner-relay_authorized_keys"):
        assert f"    {line}\n" in text, line
    assert len(list(tmp_path.glob("sshd_config.bak-*"))) == 1  # backed up first


def test_running_again_changes_nothing(tmp_path):
    config = sample(tmp_path)
    run(config, "-PublicKey", KEY)
    once = config.read_bytes()
    assert run(config, "-PublicKey", KEY).returncode == 0
    assert config.read_bytes() == once
    assert config.read_text(encoding="utf-8").count("Match User photoscanner-relay") == 1


def test_remove_restores_the_original_byte_for_byte(tmp_path):
    config = sample(tmp_path)
    run(config, "-PublicKey", KEY)
    assert run(config, "-Remove").returncode == 0
    assert config.read_bytes() == DEFAULT.encode("ascii")


def test_windows_line_endings_are_kept(tmp_path):
    config = sample(tmp_path, "\r\n")
    run(config, "-PublicKey", KEY)
    data = config.read_bytes()
    assert b"Match User photoscanner-relay\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")  # no bare LF slipped in
    assert run(config, "-Remove").returncode == 0
    assert config.read_bytes() == DEFAULT.replace("\n", "\r\n").encode("ascii")


def test_a_landing_folder_with_spaces_is_quoted(tmp_path):
    config = sample(tmp_path)
    assert run(config, "-PublicKey", KEY, "-Root", r"D:\Photo Relay").returncode == 0
    assert '    ChrootDirectory "D:/Photo Relay"\n' in config.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [
    "",                                                  # forgotten
    "ssh-rsa AAAAB3NzaC1yc2E photoscanner@laptop",       # not the key type we generate
    "ssh-ed25519 AAAAC3Nza\nC1lZDI1NTE5 split@laptop",   # broken across lines while pasting
])
def test_a_bad_key_is_refused_before_anything_changes(tmp_path, bad):
    config = sample(tmp_path)
    result = run(config, "-PublicKey", bad)
    assert result.returncode != 0
    assert "public key" in (result.stdout + result.stderr)
    assert config.read_bytes() == DEFAULT.encode("ascii")
    assert not list(tmp_path.glob("sshd_config.bak-*"))


def test_an_unsafe_account_name_is_refused(tmp_path):
    config = sample(tmp_path)
    result = run(config, "-PublicKey", KEY, "-User", "relay user")
    assert result.returncode != 0 and config.read_bytes() == DEFAULT.encode("ascii")
