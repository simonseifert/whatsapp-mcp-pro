# wa-client

Self-hosted WhatsApp web client that rides the bridge's device session — zero
extra linked-device slots, unlimited browsers. See the "wa-client" section of
the [main README](../README.md).

## Run

```bash
# uses whatsapp-mcp-server's venv (starlette/uvicorn/httpx)
../whatsapp-mcp-server/.venv/bin/python app.py
```

Open http://127.0.0.1:8084. Configuration is env-based — see the docstring at
the top of `app.py` (`WA_WEB_HOST`, `WA_WEB_PORT`, `WA_BRIDGE_URL`,
`WA_CLIENT_DATA`, `WA_MCP_URL`).

## Real-time push

Register a bridge webhook pointing at this app so new messages push instantly
instead of polling (the secret is auto-generated at `.webhook-secret` in the
data dir on first run):

```bash
curl -X POST localhost:8080/api/webhooks \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d "{\"name\":\"wa-client push\",
       \"webhook_url\":\"http://<WA_WEB_HOST>:8084/api/webhook\",
       \"secret_token\":\"$(cat .webhook-secret)\",
       \"enabled\":true,
       \"triggers\":[{\"trigger_type\":\"all\",\"trigger_value\":\"\",\"match_type\":\"exact\",\"enabled\":true}]}"
```

If the bridge rejects the URL as private/reserved, set `DISABLE_SSRF_CHECK=true`
in the bridge `.env` (fine on a single-user box; webhook creation is
API-key-gated).

Without a webhook the client still works — it falls back to 30s polling.

## Security

No login of its own: bind to 127.0.0.1 or a VPN/tailnet address only. The
bridge API key stays server-side; browsers never see it.
