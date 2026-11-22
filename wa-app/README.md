# wa-app

A self-hosted, login-protected chat PWA that rides the whatsapp-mcp-pro bridge. Visual contract: [DESIGN.md](DESIGN.md).

Status: M1, read and reply (chat list, chat view, media, live updates, read state, sending). AI drafts come in M2. Install and screenshots: see the repo [README](../README.md#app).

## Layout

- `server/` Starlette app (`wa-app` console script): JSON API, session auth, serves `web/dist`.
- `web/` Vite + React + TypeScript + Tailwind v4 front end. `src/styles/tokens.css` is generated from `DESIGN.md`, never edited by hand.
- `deploy/wa-app.service` systemd user unit template.

## Configuration

| Variable | Default | Meaning |
|-|-|-|
| `WA_APP_HOST` | `127.0.0.1` | Bind address |
| `WA_APP_PORT` | `8085` | Bind port |
| `WA_APP_DATA` | `~/.local/share/wa-app` | Where `app.db` lives |
| `WA_STORE_PATH` | same rule as `whatsapp-mcp-server` | Bridge store dir holding `messages.db` |
| `WA_APP_INSECURE_COOKIES` | unset | `1` drops the `Secure` cookie flag, for local http development only |

The login rate limit keys on the client address; behind a loopback proxy (tailscale serve) it uses the last `X-Forwarded-For` hop (the proxy appends the real client; earlier hops are client-controlled), trusted only from a loopback peer.

Expose it with `tailscale serve` (never Funnel by default). Session cookies are `Secure`, so they need https.

## Develop

```sh
cd web && pnpm install && pnpm build   # builds web/dist
cd ../server && uv sync && WA_APP_INSECURE_COOKIES=1 uv run wa-app
```

## Tests

```sh
cd server && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy .
cd web && pnpm lint && pnpm exec tsc -b && pnpm test && pnpm build
cd web && pnpm exec playwright install chromium webkit && pnpm e2e   # uses WA_APP_PORT (default 18085)
```

## Regenerating tokens

```sh
cd web && pnpm exec design.md export ../DESIGN.md --format css-tailwind > src/styles/tokens.css
```
