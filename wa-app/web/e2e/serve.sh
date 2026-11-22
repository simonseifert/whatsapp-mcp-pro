#!/bin/sh
# Starts wa-app against a throwaway data dir and a fictional bridge store. Used by playwright.config.ts.
set -eu
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
cd "$(dirname "$0")/../../server"
store=$(uv run python -c "import sys; from pathlib import Path; from tests.conftest import make_store; print(make_store(Path(sys.argv[1])))" "$tmp")
export BRIDGE_HOST="127.0.0.1:1" # a dead address: tests must never reach a live bridge
export WA_STORE_PATH="$store" WA_APP_DATA="$tmp/data" WA_APP_PORT="${WA_APP_PORT:-18085}" WA_APP_INSECURE_COOKIES=1
uv run wa-app
