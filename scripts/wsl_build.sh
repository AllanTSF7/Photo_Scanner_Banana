#!/usr/bin/env bash
# Sync the Windows checkout into the WSL filesystem (much faster than building on /mnt/c),
# build banana_core, and run the tests.
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/wsl_build.sh [pytest args]
set -euo pipefail

SRC=${BANANA_SRC:-/mnt/c/Users/TSF2/Photo_Scanner_Banana/}
DST=${BANANA_DST:-$HOME/Photo_Scanner_Banana}

rsync -a --delete --exclude .venv --exclude .tools --exclude __pycache__ --exclude .pytest_cache \
    --exclude 'native/build' "$SRC" "$DST/"
cd "$DST"

if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e '.[dev,ocr,ner]' > /tmp/pip-app.log 2>&1 || { tail -40 /tmp/pip-app.log; exit 1; }

echo "== building native =="
if ! pip install -v ./native > /tmp/pip-native.log 2>&1; then
    grep -nE 'error|Error' /tmp/pip-native.log | head -40
    tail -30 /tmp/pip-native.log
    exit 1
fi
grep -E 'warning:' /tmp/pip-native.log | head -20 || true
python -c 'import banana_core; print("native ok, HAVE_OPENCV =", banana_core.HAVE_OPENCV)'

echo "== tests =="
python -m pytest -q -rs "$@"
