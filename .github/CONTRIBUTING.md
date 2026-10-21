# Contributing to whatsapp-mcp-pro

Thanks for helping. This is a fork of
[FelixIsaac/whatsapp-mcp-extended](https://github.com/FelixIsaac/whatsapp-mcp-extended)
that adds recall, transcription, an identity directory and a few optional
interfaces on top of upstream's bridge and MCP server. See
[ARCHITECTURE.md](../ARCHITECTURE.md) for how the pieces fit.

## Get the code

```bash
git clone https://github.com/simonseifert/whatsapp-mcp-pro
cd whatsapp-mcp-pro
cp .env.example .env        # set API_KEY at least: openssl rand -hex 32
```

Native is the primary path. `docker/` and `docker-compose.yaml` come from
upstream and still work, but they are not what the maintainer runs.

## Bridge (Go, `whatsapp-bridge/`)

Go 1.26+; `go.mod` pins the exact toolchain.

```bash
cd whatsapp-bridge
go build ./... && go vet ./... && go test ./...
go build -o whatsapp-bridge . && ./whatsapp-bridge     # pair by QR on first run
```

CI lints with golangci-lint v2. Build it with the toolchain `go.mod` pins,
because golangci-lint refuses to lint code that targets a newer Go than it was
built with:

```bash
cd whatsapp-bridge
export GOTOOLCHAIN="$(awk '/^toolchain /{print $2}' go.mod)"
go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.15.0
golangci-lint run --timeout=5m
```

## MCP server (Python, `whatsapp-mcp-server/`)

Python 3.11+ with [uv](https://docs.astral.sh/uv/).

```bash
cd whatsapp-mcp-server
uv sync --extra dev                 # add --extra pro for recall/transcription
uv run python check.py              # fast preflight
uv run ruff check . && uv run ruff format --check . && uv run mypy .
uv run pytest -q
```

CI runs `ruff check` **and** `ruff format --check`; a clean check does not mean
format is clean, so run both. mypy is clean; keep it that way.

## Web UI (`whatsapp-web-ui/`)

Upstream's admin panel (pairing, webhooks). It uses pnpm:

```bash
cd whatsapp-web-ui && pnpm install && pnpm dev
```

## Conventions

- **Go:** use the package `logger`, not `fmt.Println`. Godoc on exported
  functions, table-driven tests, wrap errors with `%w`.
- **Python:** use `logger` from `lib.utils`, not `print()`. Type hints and
  docstrings on public functions. Raise on error rather than returning empty.
- **MCP tools** are registered through the `@tool()` wrapper in `main.py`,
  never `@mcp.tool()` directly; that bypasses toolset gating and the annotations
  the tests check. Prefer extending an existing tool to adding a one-endpoint
  wrapper, give new tools a toolset, a title and accurate `read_only` /
  `destructive` flags (they become the MCP annotations), and cover them in `tests/test_main_tools.py`.
- **Schema changes** go in `whatsapp-bridge/migrations/` as new, sequentially
  numbered, idempotent SQL files. Never edit or delete an existing migration.
- **Comments explain why, not what.** Many record a trap someone already hit;
  keep them when you edit nearby code.
- **No personal data.** Use obvious fakes in tests and examples
  (`4915112345678@s.whatsapp.net`, `123456789012345@lid`). Never commit a
  `.env`, a `store/` or a database.

## Staying mergeable with upstream

This fork regularly merges upstream. To keep that cheap:

- Change upstream files only where you need to, and keep the change local to
  the lines that matter. Don't reformat or reorganise them.
- Put fork-only features in their own modules (as `lib/recall.py`,
  `lib/transcribe.py` and `lib/identity.py` are) rather than spreading them
  through shared files.
- If a fix applies to upstream too, say so in the PR. It can be offered there.

## Pull requests

1. Branch from `main`.
2. Keep it to one fix or feature.
3. Run the Go and Python checks above; CI runs the same ones.
4. Update `.env.example` if you add or rename an environment variable, and the
   docs if behaviour changes.

## Issues

Use the issue templates. For security problems, see [SECURITY.md](SECURITY.md)
and report privately instead.
