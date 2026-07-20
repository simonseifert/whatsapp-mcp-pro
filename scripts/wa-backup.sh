#!/usr/bin/env bash
# Daily backup of the WhatsApp bridge SQLite stores (messages + session).
# sqlite3 .backup is safe against a live writer (uses the online backup API).
# Retention: 14 days. Run daily from launchd/cron.
#
# Override with WA_STORE / WA_BACKUP_DEST. Uses the online backup API rather
# than copying the file: a plain cp of a live SQLite database omits the -wal,
# which can leave the copy missing recent writes or torn mid-page.
#
# Verify a backup is actually restorable — an untested backup is a guess:
#   gunzip -c messages-YYYYMMDD.db.gz > /tmp/t.db
#   sqlite3 /tmp/t.db "PRAGMA integrity_check; SELECT COUNT(*) FROM messages;"
set -euo pipefail

STORE="${WA_STORE:-$HOME/whatsapp-mcp-pro/whatsapp-bridge/store}"
DEST="${WA_BACKUP_DEST:-$HOME/backups/whatsapp}"
STAMP="$(date +%Y%m%d)"

mkdir -p "$DEST"

for db in messages whatsapp; do
    sqlite3 "$STORE/$db.db" ".backup '$DEST/$db-$STAMP.db'"
done
gzip -f "$DEST/messages-$STAMP.db" "$DEST/whatsapp-$STAMP.db"

find "$DEST" -name "*.db.gz" -mtime +14 -delete

echo "[wa-backup] $(date +%F) ok"
