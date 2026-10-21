#!/bin/bash
# Build and restart the live WhatsApp services from this repo.
#
# Topology (debian-native, set up 2026-08-21):
#   code   ~/Code/tools/whatsapp-mcp-pro           (this git repo)
#   data   ~/.local/share/whatsapp-mcp-pro/        (store/ + .env — NEVER in the repo)
#   units  /etc/systemd/system/whatsapp-{bridge,mcp}.service
#          run the repo's binary/venv with WorkingDirectory/WA_STORE_PATH pointed
#          at the data dir, guarded by wa-bridge-health.timer.
#
# There is no separate deploy copy any more: the services execute straight out of
# this repo, so "deploy" is build -> test -> restart. Data is never touched.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${WA_DATA_DIR:-/home/alice/.local/share/whatsapp-mcp-pro}"

log() { printf '\033[1m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[deploy] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$DATA/.env" ] || die "data .env missing at $DATA — wrong data dir?"
[ -f "$DATA/store/whatsapp.db" ] || die "session store missing at $DATA/store — refusing to restart"

# ── 1. Build & test before touching a running service ───────────────────────
# Build to a side file: the unit executes $BIN directly, and Restart=always / the
# health timer can relaunch it at any moment, so it must only ever be replaced by
# a binary that has already passed the tests.
BIN="$REPO/whatsapp-bridge/whatsapp-bridge"
trap 'rm -f "$BIN.new"' EXIT
log "building bridge"
(cd "$REPO/whatsapp-bridge" && go build -o whatsapp-bridge.new .)
log "go tests"
(cd "$REPO/whatsapp-bridge" && go test ./... >/dev/null) || die "Go tests failed — not deploying"
log "python deps + checks"
(cd "$REPO/whatsapp-mcp-server" && uv sync --extra dev --extra pro --quiet \
  && uv run ruff check --quiet . \
  && uv run pytest -q --ignore=tests/test_recall.py >/dev/null) || die "Python checks failed — not deploying"

# ── 2. Back up the live store (online, no downtime) ─────────────────────────
BACKUP_DIR="$DATA/backups/$(date +%Y%m%d-%H%M%S)"
log "backing up store -> $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
for db in messages.db whatsapp.db; do
  sqlite3 "$DATA/store/$db" ".backup '$BACKUP_DIR/$db'"
done
ls -dt "$DATA/backups"/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf   # keep 5

# ── 3. Swap in the new binary, keeping the old one for rollback ─────────────
[ -f "$BIN" ] && cp -p "$BIN" "$BIN.prev"
mv -f "$BIN.new" "$BIN"

rollback() {
  [ -f "$BIN.prev" ] || die "$1 — and no $BIN.prev to roll back to (backup: $BACKUP_DIR)"
  log "rolling back to previous binary"
  mv -f "$BIN.prev" "$BIN"
  sudo systemctl restart whatsapp-bridge.service
  sudo systemctl restart whatsapp-mcp.service
  restart_others 2>/dev/null || true
  die "$1 — rolled back to the previous bridge binary (backup: $BACKUP_DIR)"
}

# ── 4. Restart (bridge first, MCP follows via its dependency) ───────────────
log "restarting services"
sudo systemctl restart whatsapp-bridge.service
sudo systemctl restart whatsapp-mcp.service

# ── 5. Verify every bridge on this binary comes back connected ──────────────
# Other numbers run the same binary from their own data dirs (Friday's is a
# user unit). They must be restarted onto the new build and checked too, or a
# bad build only shows up at their next unrelated restart.
OTHER_BRIDGES="${OTHER_BRIDGES:-friday|whatsapp-bridge-friday|8083|/home/alice/.local/share/whatsapp-friday/.env}"

wait_connected() {  # wait_connected <port> <env file>
  local key state
  key="$(grep -E '^API_KEY=' "$2" | head -1 | cut -d= -f2-)"
  for _ in $(seq 1 30); do
    state="$(curl -sf -m 3 -H "X-API-Key: $key" "http://localhost:$1/api/connection" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("connected"))' 2>/dev/null || true)"
    [ "$state" = "True" ] && return 0
    sleep 4
  done
  return 1
}

restart_others() {
  local name unit port envf
  while IFS='|' read -r name unit port envf; do
    [ -n "$name" ] || continue
    systemctl --user restart "$unit"
  done <<< "$OTHER_BRIDGES"
}

log "waiting for bridge to report connected"
wait_connected 8080 "$DATA/.env" \
  || rollback "bridge not connected within 2 min — see journalctl -u whatsapp-bridge -n 50"
log "✓ bridge connected"
sudo journalctl -u whatsapp-bridge --since "1 min ago" --no-pager 2>/dev/null | grep -i "\[IDENTITY\]" | tail -2 || true

restart_others
while IFS='|' read -r name unit port envf; do
  [ -n "$name" ] || continue
  log "waiting for the $name bridge"
  wait_connected "$port" "$envf" \
    || rollback "$name bridge not connected within 2 min — see journalctl --user -u $unit -n 50"
  log "✓ $name bridge connected"
done <<< "$OTHER_BRIDGES"
log "done (store backup: $BACKUP_DIR)"
