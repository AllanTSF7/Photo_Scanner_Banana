#!/usr/bin/env bash
# Live development server: runs straight from the Windows checkout, restarts on Python changes,
# and the open browser page reloads itself on any change (static files or server restart).
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/dev_live.sh [port]
# Uses the same local test folders as serve_local.sh. Doesn't run the test suite.
set -euo pipefail

PORT=${1:-8000}
# 127.0.0.1 = this PC only. BANANA_HOST=0.0.0.0 lets Windows forward LAN traffic in (see scripts/share_on_lan.ps1).
HOST=${BANANA_HOST:-127.0.0.1}
SRC=${BANANA_SRC:-/mnt/c/Users/TSF2/Photo_Scanner_Banana}
WIN_ROOT=${BANANA_WIN_ROOT:-/mnt/c/Users/TSF2/Banana_Test}
STATE=${BANANA_STATE:-$HOME/banana-local}
APP=${BANANA_DST:-$HOME/Photo_Scanner_Banana}

if [ ! -x "$APP/.venv/bin/python" ]; then
    echo "No build found; running wsl_build.sh once"
    bash "$SRC/scripts/wsl_build.sh" -q
fi

mkdir -p "$WIN_ROOT"/{inbox,archive,library} "$STATE/data"
cat > "$STATE/config.toml" <<EOF
[paths]
inbox = "$WIN_ROOT/inbox"
archive = "$WIN_ROOT/archive"
sorted = "$WIN_ROOT/library"
data_dir = "$STATE/data"

[scanner]
host = "${BANANA_SCANNER_HOST:-192.168.16.178}"
EOF

pkill -f "banana serve" 2>/dev/null || true
pkill -f "uvicorn banana.web.api" 2>/dev/null || true

. "$APP/.venv/bin/activate"
export BANANA_CONFIG="$STATE/config.toml" BANANA_DEV_RELOAD=1 WATCHFILES_FORCE_POLLING=true
export PYTHONPATH="$SRC"   # import the Windows checkout, not the rsynced copy
cd "$SRC"
echo "LIVE: http://localhost:$PORT  (bound to $HOST; editing $SRC/banana reloads the server and the page)"
exec python -m uvicorn banana.web.api:app --host "$HOST" --port "$PORT" \
    --reload --reload-dir "$SRC/banana" --reload-include '*.py'
