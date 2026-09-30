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
DATA="${WA_DATA_DIR:-/home/simon/.local/share/whatsapp-mcp-pro}"

log() { printf '\033[1m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[deploy] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$DATA/.env" ] || die "data .env missing at $DATA — wrong data dir?"
[ -f "$DATA/store/whatsapp.db" ] || die "session store missing at $DATA/store — refusing to restart"

# ── 1. Build & test before touching a running service ───────────────────────
log "building bridge"
(cd "$REPO/whatsapp-bridge" && go build -o whatsapp-bridge .)
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

# ── 3. Restart (bridge first, MCP follows via its dependency) ───────────────
log "restarting services"
sudo systemctl restart whatsapp-bridge.service
sudo systemctl restart whatsapp-mcp.service

# ── 4. Verify the bridge comes back connected ───────────────────────────────
API_KEY="$(grep -E '^API_KEY=' "$DATA/.env" | head -1 | cut -d= -f2-)"
log "waiting for bridge to report connected"
for _ in $(seq 1 30); do
  state="$(curl -sf -m 3 -H "X-API-Key: $API_KEY" http://localhost:8080/api/connection 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("connected"))' 2>/dev/null || true)"
  if [ "$state" = "True" ]; then
    log "✓ bridge connected"
    sudo journalctl -u whatsapp-bridge --since "1 min ago" --no-pager 2>/dev/null | grep -i "\[IDENTITY\]" | tail -2 || true
    log "done (store backup: $BACKUP_DIR)"
    exit 0
  fi
  sleep 4
done
die "bridge not connected within 2 min — journalctl -u whatsapp-bridge -n 50 (backup: $BACKUP_DIR)"
