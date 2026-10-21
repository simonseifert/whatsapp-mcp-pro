## Description
Brief description of the change and why.

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Changes Made
-
-

## Testing
- [ ] `go vet ./... && go test ./...` (bridge)
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest -q` (MCP server)
- [ ] Added/updated tests
- [ ] Tested against a real linked device (if it touches sending, sync or media)

## Checklist
- [ ] Upstream files changed only where needed (see CONTRIBUTING.md)
- [ ] `.env.example` and docs updated for new or renamed settings
- [ ] New schema changes are a new migration file, not an edit
- [ ] No personal data: real numbers, JIDs, hosts, paths, `.env`, `store/`

## Related Issues
Fixes #
