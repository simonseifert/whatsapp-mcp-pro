#!/bin/sh
# Starts wa-app against a throwaway data dir and a fictional bridge store. Used by playwright.config.ts.
set -eu
# A known path, so e2e specs can write to the fictional store the way the bridge would (see e2e/store.ts).
tmp="${TMPDIR:-/tmp}/wa-app-e2e-${WA_APP_PORT:-18085}"
rm -rf "$tmp" && mkdir -p "$tmp"
stub_pid=""
trap '[ -n "$stub_pid" ] && kill "$stub_pid" 2>/dev/null; rm -rf "$tmp"' EXIT INT TERM
cd "$(dirname "$0")/../../server"
store=$(uv run python -c "import sys; from pathlib import Path; from tests.conftest import make_store; print(make_store(Path(sys.argv[1])))" "$tmp")
# A stand-in bridge on a free port (its port lands in $tmp/stub.port for the specs): tests must never reach a live one.
uv run python ../web/e2e/stub_bridge.py "$store/messages.db" "$tmp/stub.port" &
stub_pid=$!
for _ in $(seq 1 100); do [ -s "$tmp/stub.port" ] && break; sleep 0.1; done
[ -s "$tmp/stub.port" ] || { echo "stub bridge did not publish its port" >&2; exit 1; }
export BRIDGE_HOST="127.0.0.1:$(cat "$tmp/stub.port")"
export WA_STORE_PATH="$store" WA_APP_DATA="$tmp/data" WA_APP_PORT="${WA_APP_PORT:-18085}" WA_APP_INSECURE_COOKIES=1
uv run wa-app
