#!/usr/bin/env bash
# wa-dispatch installer — generates machine-specific files from your config.
# Safe to re-run: everything it writes is regenerated, and the Claude settings
# edit is additive and idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/config.env"
OK=0; WARN=0; FAIL=0
say()  { printf '  %s\n' "$*"; }
good() { printf '  \033[32mok\033[0m   %s\n' "$*"; OK=$((OK+1)); }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31mfail\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

echo
echo "wa-dispatch installer"
echo "====================="

# ── config ───────────────────────────────────────────────────────────────────
echo
echo "config"
if [[ ! -f "$CONFIG" ]]; then
  cp "$HERE/config.example.env" "$CONFIG"
  warn "created config.env from the example — EDIT IT, then re-run this script"
  say  "     \$EDITOR $CONFIG"
  exit 0
fi
good "config.env present"
# shellcheck disable=SC1090
set -a; source "$CONFIG"; set +a
expand() { eval echo "${1/#\~/$HOME}"; }

# ── prerequisites ────────────────────────────────────────────────────────────
echo
echo "prerequisites"
for c in python3 sqlite3 tmux; do
  command -v "$c" >/dev/null 2>&1 && good "$c" || bad "$c not found"
done
CLAUDE_PATH="$(expand "${CLAUDE_BIN:-claude}")"
if [[ -x "$CLAUDE_PATH" ]]; then good "claude ($CLAUDE_PATH)"
elif command -v claude >/dev/null 2>&1; then
  CLAUDE_PATH="$(command -v claude)"
  warn "CLAUDE_BIN not executable; using $CLAUDE_PATH — update config.env"
else bad "claude not found — set CLAUDE_BIN in config.env"; fi

# ── bridge reachability ──────────────────────────────────────────────────────
echo
echo "bridge"
DB="$(expand "${BRIDGE_DB}")"
if [[ "${BRIDGE_MODE:-local}" == "ssh" ]]; then
  if [[ -z "${BRIDGE_SSH_HOST:-}" ]]; then bad "BRIDGE_MODE=ssh but BRIDGE_SSH_HOST is empty"
  elif ssh -o BatchMode=yes -o ConnectTimeout=8 "$BRIDGE_SSH_HOST" true 2>/dev/null; then
    good "ssh $BRIDGE_SSH_HOST"
    if ssh -o BatchMode=yes "$BRIDGE_SSH_HOST" "test -r ${BRIDGE_DB}" 2>/dev/null; then
      good "message store readable on $BRIDGE_SSH_HOST"
    else bad "cannot read $BRIDGE_DB on $BRIDGE_SSH_HOST"; fi
  else bad "ssh $BRIDGE_SSH_HOST failed (needs passwordless key auth)"; fi
else
  [[ -r "$DB" ]] && good "message store readable ($DB)" \
                 || bad "cannot read $DB — is the bridge installed and paired?"
fi

# ── generated: spawn-settings.json (deny hook wiring) ────────────────────────
echo
echo "generated files"
cat > "$HERE/spawn-settings.json" <<JSON
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "$HERE/hooks/deny.py" }
        ]
      }
    ]
  }
}
JSON
good "spawn-settings.json"

mkdir -p "$HOME/Library/LaunchAgents"
PLIST_DISPATCH="$HOME/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist"
cat > "$PLIST_DISPATCH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wa-dispatch.dispatcher</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$HERE/wa-dispatch.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$CLAUDE_PATH"):/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/wa-dispatch.log</string>
  <key>StandardErrorPath</key><string>/tmp/wa-dispatch.log</string>
</dict></plist>
PLIST
good "launchd plist (dispatcher)"

if [[ "${APPROVE_ENABLED:-true}" == "true" ]]; then
  PLIST_APPROVE="$HOME/Library/LaunchAgents/com.wa-dispatch.approve.plist"
  cat > "$PLIST_APPROVE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wa-dispatch.approve</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$HERE/wa-approve.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/wa-approve.log</string>
  <key>StandardErrorPath</key><string>/tmp/wa-approve.log</string>
</dict></plist>
PLIST
  good "launchd plist (approve)"
  if [[ "${APPROVE_BIND:-}" == "0.0.0.0" ]]; then
    bad "APPROVE_BIND=0.0.0.0 — this endpoint can send WhatsApp messages. Use a VPN/tailnet address or 127.0.0.1."
  fi
fi

if [[ -n "${FATHOM_KEY_FILE:-}" ]]; then
  cat > "$HOME/Library/LaunchAgents/com.wa-dispatch.meetings.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wa-dispatch.meetings</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$HERE/meet-dispatch.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$CLAUDE_PATH"):/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/meet-dispatch.log</string>
  <key>StandardErrorPath</key><string>/tmp/meet-dispatch.log</string>
</dict></plist>
PLIST
  good "launchd plist (meetings)"
  [[ -r "$(expand "$FATHOM_KEY_FILE")" ]] && good "fathom key readable" \
    || bad "FATHOM_KEY_FILE set but not readable: $FATHOM_KEY_FILE"
fi

# ── Claude Stop hook (additive, idempotent) ──────────────────────────────────
echo
echo "claude integration"
python3 - "$HERE" <<'PY'
import json, os, sys, shutil
here = sys.argv[1]
p = os.path.expanduser("~/.claude/settings.json")
hook = os.path.join(here, "hooks", "inbox-stop-hook.py")
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    d = json.load(open(p))
except Exception:
    d = {}
if os.path.exists(p):
    shutil.copy(p, p + ".bak-wa-dispatch")
stop = d.setdefault("hooks", {}).setdefault("Stop", [])
if any(hook in json.dumps(e) for e in stop):
    print("  \033[32mok\033[0m   Stop hook already installed")
else:
    stop.append({"hooks": [{"type": "command", "command": hook, "timeout": 10}]})
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, p)
    print("  \033[32mok\033[0m   Stop hook installed (existing hooks preserved)")
PY

# ── routes ───────────────────────────────────────────────────────────────────
echo
echo "routes"
if [[ ! -f "$HERE/routes.json" ]]; then
  cp "$HERE/routes.example.json" "$HERE/routes.json"
  warn "created routes.json from the example — nothing is routed until you edit it"
else
  python3 - "$HERE" <<'PY'
import json, os, sys
r = json.load(open(os.path.join(sys.argv[1], "routes.json")))
on = [x for x in r.get("routes", []) if x.get("enabled", True)]
if not on:
    print("  \033[33mwarn\033[0m no routes enabled — nothing will be dispatched")
for x in on:
    d = os.path.expanduser(x["project"])
    if os.path.isdir(d):
        print("  \033[32mok\033[0m   %s -> %s" % (x.get("label"), x["project"]))
    else:
        print("  \033[31mfail\033[0m %s -> %s (missing)" % (x.get("label"), x["project"]))
PY
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo
echo "----------------------------------------------------------------"
printf '  %d ok, %d warn, %d fail\n' "$OK" "$WARN" "$FAIL"
if (( FAIL > 0 )); then
  echo "  Fix the failures above, then re-run ./install.sh"
  exit 1
fi
cat <<EOF

  Start it:
    launchctl load ~/Library/LaunchAgents/com.wa-dispatch.dispatcher.plist
$( [[ "${APPROVE_ENABLED:-true}" == "true" ]] && echo "    launchctl load ~/Library/LaunchAgents/com.wa-dispatch.approve.plist" )

  Watch it:
    tail -f /tmp/wa-dispatch.log

  Dry run (shows routing decisions, starts nothing):
    ./wa-dispatch.py --dry-run

  First run records the current max message id, so it will not backfill
  your history. Only messages arriving from now on are dispatched.
EOF
