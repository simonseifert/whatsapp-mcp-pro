#!/usr/bin/env bash
# Auto-restart wrapper for the WhatsApp bridge.
#
# The bridge's WATCHDOG calls os.Exit after 3m15s of disconnection on the
# assumption that something (Docker, systemd, Kubernetes) will restart the
# process. Standalone installs don't have that — the binary just stays dead
# until manually relaunched. See issue #2.
#
# Run this script from the whatsapp-bridge/ directory; it sources ../.env
# automatically so the bridge picks up API_KEY etc.
#
# Stop with Ctrl-C (the script handles SIGINT and exits the loop cleanly).

set -u

trap 'echo "[run-bridge] received SIGINT, exiting wrapper" >&2; exit 130' INT

# Source .env if present so API_KEY and friends are available.
ENV_FILE="../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# Wait for the previous instance to release the REST port; a restart that
# loses this race leaves a bridge connected to WhatsApp but without REST.
for _ in $(seq 1 30); do
    lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 || break
    echo "[run-bridge] port 8080 still held, waiting..." >&2
    sleep 1
done

if [[ ! -x ./whatsapp-bridge ]]; then
    echo "[run-bridge] ./whatsapp-bridge not found or not executable in $(pwd)" >&2
    echo "[run-bridge] run this script from the whatsapp-bridge/ directory after building" >&2
    exit 1
fi

while true; do
    ./whatsapp-bridge
    exit_code=$?
    echo "[run-bridge] bridge exited (code $exit_code), restarting in 5s..." >&2
    sleep 5
done
