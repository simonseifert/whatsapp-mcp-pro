#!/usr/bin/env bash
# wabridge — quick status snapshot for the WhatsApp bridge.
#
# Tells you in one screen whether the bridge is healthy, zombie (process
# alive but ingestion frozen), or dead. Designed for `alias wabridge=...`
# in your shell so you can type `wabridge` from anywhere.
#
# Exit codes: 0 healthy, 1 stale/zombie, 2 dead/missing process.

set -u

REPO="${WABRIDGE_REPO:-/Users/simon/Code/tools/whatsapp-mcp-extended-pro}"
DB="$REPO/whatsapp-bridge/store/messages.db"
STALE_MIN="${WABRIDGE_STALE_MIN:-30}"   # minutes of no messages == zombie

# ANSI colors. Bypass with NO_COLOR=1 if piping to a file.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
    GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi

pid=$(pgrep -f "./whatsapp-bridge" 2>/dev/null | head -1)
port_pid=$(lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null | head -1)

if [[ -z "$pid" && -z "$port_pid" ]]; then
    echo "${RED}● DEAD${RESET}  no whatsapp-bridge process, port 8080 unbound"
    echo "${DIM}  fix: tmux a -t wa-bridge   (then in the pane: ../scripts/run-bridge.sh)${RESET}"
    exit 2
fi

# Process up — gather details
uptime=$(ps -o etime= -p "${pid:-$port_pid}" 2>/dev/null | tr -d ' ')
listening="no"; [[ -n "$port_pid" ]] && listening="yes"

# Uptime in seconds — used to suppress false-zombie reports during cold start.
# macOS `ps` only exposes the formatted `etime` (MM:SS, HH:MM:SS, or D-HH:MM:SS),
# so parse it ourselves.
parse_etime() {
    local raw="$1" days=0 h=0 m=0 s=0
    if [[ "$raw" == *-* ]]; then
        days="${raw%-*}"
        raw="${raw#*-}"
    fi
    local IFS=:
    local parts=( $raw )
    case ${#parts[@]} in
        2) m=${parts[0]}; s=${parts[1]} ;;
        3) h=${parts[0]}; m=${parts[1]}; s=${parts[2]} ;;
        *) echo 0; return ;;
    esac
    # Strip leading zeros so arithmetic doesn't treat them as octal.
    h=$((10#${h:-0})); m=$((10#${m:-0})); s=$((10#${s:-0})); days=$((10#${days:-0}))
    echo $(( days*86400 + h*3600 + m*60 + s ))
}
uptime_sec=$(parse_etime "$uptime")

# DB freshness (only if DB exists)
if [[ -f "$DB" ]]; then
    row=$(sqlite3 "$DB" "SELECT MAX(timestamp) || '|' || (strftime('%s','now') - strftime('%s', MAX(timestamp))) FROM messages;" 2>/dev/null)
    latest="${row%|*}"
    age_sec="${row##*|}"
    [[ -z "$age_sec" || "$age_sec" == "$row" ]] && age_sec=-1
    if (( age_sec >= 0 )); then
        age_min=$(( age_sec / 60 ))
    else
        age_min=-1
    fi
else
    latest="-"; age_sec=-1; age_min=-1
fi

# Verdict
if [[ "$listening" != "yes" ]]; then
    color="$RED"; status="● UNHEALTHY"
    note="process alive but port 8080 not bound"; rc=2
elif (( age_sec < 0 )); then
    color="$YELLOW"; status="● UNKNOWN  "
    note="messages.db not found at $DB"; rc=1
elif (( uptime_sec < 600 )); then
    color="$GREEN"; status="● STARTING "
    note="bridge started ${uptime_sec}s ago — quiet chats are normal during warmup"
    rc=0
elif (( age_min >= STALE_MIN )); then
    color="$YELLOW"; status="● ZOMBIE   "
    note="process alive, websocket dead — wrapper won't restart it on its own"
    rc=1
else
    color="$GREEN"; status="● HEALTHY  "
    note="ingesting"; rc=0
fi

echo "${color}${status}${RESET}  ${note}"
echo "${DIM}  pid ${pid:-?}    uptime ${uptime:-?}    port-8080 ${listening}${RESET}"
if (( age_sec >= 0 )); then
    if (( age_min > 0 )); then
        echo "${DIM}  last message ${latest} (${age_min}m ago)${RESET}"
    else
        echo "${DIM}  last message ${latest} (${age_sec}s ago)${RESET}"
    fi
fi

# Suggested fix line for non-healthy states
if (( rc == 1 )) && [[ -n "$pid" ]]; then
    echo "${DIM}  fix: kill ${pid}   (wrapper will relaunch in ~5s)${RESET}"
fi

exit "$rc"
