"""Helpers for scripts/build_windows.ps1 (run with the build venv's Python).

    python scripts/windows_update.py backup <banana.db> <dest.db>
        Consistent copy of the live database through SQLite's backup API (safe with WAL and a running app),
        then an integrity check of the copy. Exit code 1 if the copy is not sound.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def backup(src: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(dest)
    try:
        source.backup(copy)
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        scans = copy.execute("SELECT count(*) FROM scan").fetchone()[0]
    finally:
        copy.close()
        source.close()
    print(f"backup {dest}: integrity {integrity}, {scans} scans")
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "backup":
        sys.exit(backup(Path(sys.argv[2]), Path(sys.argv[3])))
    sys.exit(__doc__)
