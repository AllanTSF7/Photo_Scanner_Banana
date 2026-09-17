#!/usr/bin/env bash
# Local test setup with no NAS or Immich: inbox/archive/library in a Windows folder, DB inside WSL.
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/TSF2/Photo_Scanner_Banana/scripts/serve_local.sh [port]
# Then point Epson ScanSmart at C:\Users\TSF2\Banana_Test\inbox and open http://localhost:<port>
set -euo pipefail

PORT=${1:-8000}
# 127.0.0.1 = this PC only. BANANA_HOST=0.0.0.0 to test from phones/tablets (then run scripts/share_on_lan.ps1 as admin).
HOST=${BANANA_HOST:-127.0.0.1}
WIN_ROOT=${BANANA_WIN_ROOT:-/mnt/c/Users/TSF2/Banana_Test}
STATE=${BANANA_STATE:-$HOME/banana-local}   # SQLite + previews stay on the Linux filesystem
APP=${BANANA_DST:-$HOME/Photo_Scanner_Banana}

if [ "${BANANA_SKIP_TESTS:-0}" = "1" ]; then
    bash "$(dirname "$0")/wsl_build.sh" --co -q > /tmp/banana-build.log 2>&1 || { tail -30 /tmp/banana-build.log; exit 1; }
    echo "build (tests skipped)"
else
    bash "$(dirname "$0")/wsl_build.sh" -q > /tmp/banana-build.log 2>&1 || { tail -30 /tmp/banana-build.log; exit 1; }
    echo "build + tests: $(tail -1 /tmp/banana-build.log)"
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
pkill -f "uvicorn banana.web.api" 2>/dev/null || true   # the live-reload dev server uses the same port
sleep 1
cd "$APP"
. .venv/bin/activate
export BANANA_CONFIG="$STATE/config.toml"
banana doctor
echo "UI: http://localhost:$PORT   API docs: http://localhost:$PORT/docs   (listening on $HOST)"
exec banana serve --host "$HOST" --port "$PORT"
