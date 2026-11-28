# Cross-platform install and update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new user on Linux, macOS or Windows can install, pair, run, keep running and update the bridge, the MCP server and wa-app with one tool and a short README, and an existing user (Nirmal) can update with one command. Everything that runs on Linux today also runs on macOS and Windows.

**Architecture:** One cross-platform control script, `scripts/wactl.py` (Python standard library only, run as `uv run scripts/wactl.py <command>`, since uv is already a prerequisite), with thin launchers `wactl` (POSIX shell) and `wactl.cmd` (Windows) at the repo root. It loads `.env` itself on every OS, so launchd, systemd and Task Scheduler entries all call `wactl run <component>` instead of each re-implementing env loading. Runtime code loses its POSIX-only assumptions (`/tmp`, `os.getuid`). CI runs the test suites and an install smoke test on ubuntu, macos and windows runners, which is the only way the macOS and Windows claims get verified.

**Tech Stack:** Python 3.11+ stdlib (wactl), Go (bridge, cgo sqlite), Python/uv (MCP server, wa-app server), Node 20+ with pnpm via corepack (wa-app web), GitHub Actions matrix.

**Spec:** this document. Source: Simon's request on 2026-11-28 ("make it so it also runs on macOS smoothly and Windows too, fill those gaps, polish everything and update the README") after the gap list given to him: update order, push subject, Linux-only install, stale README, history fill discoverability.

## Global Constraints

- Branch `feat/xplat` from `origin/main`, worktree `~/.cache/wa-tools/wt/xplat`. Never commit on main. Do not push (the controller pushes).
- Never touch Simon's live install: the checkout `~/Code/tools/whatsapp-mcp-pro`, its systemd units (`whatsapp-bridge`, `whatsapp-mcp`, `wa-app`, `whatsapp-bridge-friday`) and `~/.local/share/whatsapp-mcp-pro`. `scripts/deploy-live.sh` keeps working unchanged. Tests and smoke runs use temp directories only, never pair a real WhatsApp account, never start a second bridge against a real store.
- Public repo: no real numbers, JIDs, names, hosts, IPs, emails or personal paths in tracked files.
- Docs and CLI output: plain language, sentence case, no em dashes.
- Defaults stay backward compatible: the bridge's working directory stays `whatsapp-bridge/` (store at `whatsapp-bridge/store`) unless `WA_DATA_DIR` / `--data` says otherwise; `WA_STORE_PATH` resolution in `whatsapp-mcp-server/lib/utils.py` is unchanged; existing `.env` keys keep their meaning.
- wactl uses the Python standard library only (no third-party imports), supports Python 3.11+, and never prints secret values (API_KEY, tokens): it prints `KEY=<set>` / `<missing>`.
- The bridge exit code 3 ("stream replaced") means do not restart; every supervisor wactl writes or runs honours it.
- Bridge sqlite stays `mattn/go-sqlite3` (cgo). Windows needs a C compiler; wactl doctor detects and explains it.
- Checks: server `cd wa-app/server && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy .`; web `cd wa-app/web && pnpm lint && pnpm exec tsc -b && pnpm test && pnpm build`; bridge `cd whatsapp-bridge && go build ./... && go vet ./... && go test ./... && test -z "$(gofmt -l .)"`; MCP `cd whatsapp-mcp-server && uv run --extra dev pytest -q && uv run --extra dev ruff check .` (add `--with numpy` locally if the recall tests need it); wactl `uv run --with pytest pytest -q scripts/tests` plus `uvx ruff check scripts`.

## Review Focus

1. **Temp paths on every OS.** Uploads, audio conversion and the bridge media allowlist agree on one temp root per OS (`tempfile.gettempdir()` / `os.TempDir()`, resolved for symlinks like macOS `/private/tmp`), and the allowlist still refuses paths outside it.
2. **Supervisor semantics.** `wactl run bridge` restarts on crash with backoff, stops on exit code 3, and forwards Ctrl-C/stop cleanly; services on all three OSes call it the same way.
3. **Update safety.** `wactl update` refuses on a dirty tree or a non-fast-forward pull, backs up the store before touching the bridge, restarts bridge then app, verifies health, and on failure puts the previous bridge binary back and says so.
4. **Secrets.** No command prints API_KEY or tokens; generated service files that must embed env (launchd) are written mode 600 and the docs say so.
5. **Windows reality.** Paths with spaces, `\` separators, `.exe` names, no `os.getuid`, `schtasks` quoting; CI on windows-latest proves build, tests and the smoke test.

---

### Task 1: Runtime fixes for macOS and Windows

**Files:**
- Modify: `wa-app/server/wa_app/sending.py` (upload staging under `tempfile.gettempdir()`, stale sweep without `os.getuid` on Windows), `wa-app/server/wa_app/api.py` (comment)
- Modify: `whatsapp-mcp-server/whatsapp.py` (`MEDIA_TEMP_DIR` from `tempfile.gettempdir()`, allowlist checks use it), `whatsapp-mcp-server/audio.py` (ffmpeg missing → clear error naming the install command per OS)
- Modify: `whatsapp-bridge/internal/whatsapp/messages.go` (default allowlist includes `os.TempDir()` with symlink resolution, `/tmp` kept on POSIX), any other POSIX-only bits found by the CI matrix
- Tests next to each

- [ ] Failing tests: upload staging uses the platform temp dir (monkeypatch `tempfile.tempdir`); sweep works when `os.getuid` is absent; MCP temp dir and allowlist accept a file in the platform temp dir and refuse a sibling outside it; bridge allowlist accepts `os.TempDir()` paths and its resolved form, refuses outside; ffmpeg missing gives the named error.
- [ ] Implement; run all four check sets; commit `fix: platform temp dir and no POSIX-only calls`.

### Task 2: CI on macOS and Windows

**Files:**
- Modify: `.github/workflows/go-test.yml`, `python-test.yml` (and a wa-app job if it lives elsewhere): add an OS matrix `[ubuntu-latest, macos-latest, windows-latest]` for bridge build + test, MCP tests, wa-app server tests and web build. Keep lint/type-check/security jobs on ubuntu only.
- Modify: tests that encode POSIX-only behaviour: fix the product code if the behaviour is a real bug on that OS; otherwise mark the test `skipif(sys.platform == "win32", reason=...)` with a specific reason. Never skip a whole module to get green.

- [ ] Push to the branch is done by the controller; the implementer runs what it can locally (Linux) and writes the matrix. The controller pushes, reads CI, and resumes the implementer with the failure logs until green (this task is complete only when all three OS jobs pass).
- [ ] Commit `ci: run tests on macOS and Windows`.

### Task 3: wactl, the cross-platform control script

**Files:**
- Create: `scripts/wactl.py`, `wactl` (sh launcher, executable), `wactl.cmd` (Windows launcher), `scripts/tests/test_wactl.py`
- Modify: `.env.example` (document `WA_DATA_DIR`, `WA_APP_PUSH_SUBJECT`, ports), `.gitignore` if needed

**Commands (all idempotent, all accept `--data DIR`; `WA_DATA_DIR` in `.env` is the default; repo root is found from the script's location):**
- `doctor`: OS, Python, uv, Go (version from `go.mod`), C compiler (Windows: gcc on PATH; macOS: `xcode-select -p`), Node >= 20, pnpm (or corepack available), ffmpeg (optional), tailscale (optional), `.env` present with API_KEY set (shown as `<set>`), ports 8080/8085 free or in use by our components. Each failed check prints the exact install command for this OS (apt / brew / winget). Exit 1 if a required check fails.
- `setup [--minimal|--recall] [--yes]`: create `.env` from `.env.example` with a random API_KEY if missing (never overwrite); ask once for `WA_APP_PUSH_SUBJECT` (skip with `--yes`, explain how to set it later); build the bridge (`go build -o whatsapp-bridge[.exe] .`); `uv sync` the MCP server (extra: `pro` on Apple Silicon, `pro-cpu` elsewhere, none with `--minimal`); `uv sync` wa-app server; `corepack enable` if needed, `pnpm install --frozen-lockfile && pnpm build` in wa-app/web. Ends with the next step (`wactl pair`).
- `pair`: run the bridge in the foreground in the data dir so the QR shows; on success tell the user to stop it and run `wactl service install` (or `wactl start`).
- `run bridge|app|mcp`: foreground supervisor. Loads `.env`, sets `WA_STORE_PATH` for app/mcp to the bridge store, cwd for the bridge = data dir. Restarts on non-zero exit with backoff 2s doubling to 60s, resets after 10 min up; exit code 3 → stop with a message; SIGINT/SIGTERM (Windows: Ctrl-C/CTRL_BREAK) forwarded to the child, then exit.
- `start|stop|restart|status [component]`: through the installed service manager if services are installed, else `start` spawns detached `run` processes with a pid file in the data dir. `status` shows each component (service state or pid) and health: bridge `GET http://127.0.0.1:<port>/api/health`, app `GET http://127.0.0.1:8085/api/health`.
- `service install|uninstall|status [--with-mcp]`: Linux systemd user units (`~/.config/systemd/user/wa-bridge.service`, `wa-app.service`, optional `wa-mcp.service`), enable + `loginctl enable-linger` hint; macOS LaunchAgents (`~/Library/LaunchAgents/com.wactl.<component>.plist`, KeepAlive false since wactl run supervises, RunAtLoad true, logs to data dir); Windows Task Scheduler at-logon tasks (`schtasks /Create /SC ONLOGON /TN wactl-<component> /TR "...wactl.cmd run <component>" /RL LIMITED /F`), hidden window. Every service calls `wactl run <component>`. Unit names must not collide with Simon's existing `whatsapp-bridge`, `whatsapp-mcp`, `wa-app` units: use the `wa-` / `wactl-` prefixes above, and `service install` refuses (with an explanation) if a non-wactl unit named `wa-app.service` already exists.
- `update [--yes]`: refuse if `git status --porcelain` is non-empty or the branch can't fast-forward; `git pull --ff-only`; back up `messages.db` and `whatsapp.db` with the sqlite3 backup API to `<data>/backups/<timestamp>/` plus the current bridge binary; rebuild bridge, uv sync both, pnpm build; restart bridge, wait for health (60 s), restart app, wait for health; on any failure after the backup: restore the previous bridge binary, restart, and print what failed and where the backup is. Prints a one-line summary of what changed (`git log --oneline old..new | head`).
- `logs [component]`: tail the component log (journalctl on systemd, log file otherwise).

- [ ] Failing tests (pure-function level, no real services): .env parse/merge (comments, quotes, no overwrite, API_KEY generation length 64 hex), service file rendering for each OS (exact paths, `run <component>` command, mode 600 for plist), schtasks argument quoting with a path containing spaces, supervisor restart policy (exit 0/1/3, backoff sequence, reset after uptime) with a fake child, update refusal on dirty tree / non-ff (using a temp git repo), doctor hint selection per OS, no secret value in any output (capture and assert API_KEY value absent).
- [ ] Implement; `wactl doctor` and `wactl setup --minimal --yes --data <tmp>` run on this Linux box against the worktree (no pairing, no services installed); commit `feat: wactl, one control script for Linux, macOS and Windows`.

### Task 4: Install smoke test in CI

**Files:**
- Create: `.github/workflows/install-smoke.yml`: matrix ubuntu/macos/windows. Steps: setup Go (from go.mod), Python, uv, Node 20 + corepack; `wactl doctor` (allowed to warn on optional checks); `wactl setup --minimal --yes --data $RUNNER_TEMP/wa`; create an empty bridge store fixture (sqlite file with the bridge schema, made by a tiny helper the bridge tests already use or by `go run` of the migrations) so wa-app can start; `wactl start app` (no bridge: app must start and report the bridge as down, not crash); poll `/api/health` until 200 (60 s); `wactl status`; `wactl stop app`. Also render-only `wactl service install --dry-run` on each OS and assert the output names the right manager.

- [ ] Controller pushes and reads CI; implementer fixes until all three pass. Commit `ci: install smoke test on Linux, macOS and Windows`.

### Task 5: First-run polish in wa-app

**Files:**
- Modify: `wa-app/web/src/screens/Settings.tsx` (+ tests), server routes only if needed for status data.

- A "Getting started" card at the top of Settings that appears while any item is open and can be dismissed: Notifications not set up (push subject missing: say which setting to add in `.env` and that `wactl setup` asks for it), History thin (count of thin chats, button runs "Fill thin chats"), AI drafts not set up (link to the AI section). Each row disappears once done. Uses existing endpoints (`/api/push/key` 503, `/api/history/coverage`, AI backends list); add none unless an existing one can't answer.
- The notifications switch, when push is off server-side, shows the same explanation instead of a dead switch.

- [ ] Failing tests for each row's show/hide condition and the dismiss; implement; checks; commit `feat(wa-app): getting started card`.

### Task 6: Docs

- [ ] README.md: rewrite Status (current milestones), App section and Quickstart around `wactl` with one block per OS (Linux, macOS, Windows): prerequisites with install commands (apt / brew / winget incl. the Windows C compiler), `wactl doctor`, `wactl setup`, `wactl pair`, `wactl service install`, open the app (https via `tailscale serve` on each OS, adding to the home screen), `wactl update`. Keep the manual commands in SETUP.md and link there.
- [ ] SETUP.md: step 7 "Keep it running" points to `wactl service` first and keeps the manual systemd/launchd notes; add Windows; add "Updating" (order: bridge then app, what `wactl update` does, manual equivalent).
- [ ] wa-app/README.md: install/update via wactl; push subject explained.
- [ ] CHANGELOG, DECISIONS D28 (wactl design, unit names, cgo/C compiler on Windows, temp dir rule, what CI proves), docs/progress.md.
- [ ] Commit `docs: install and update on Linux, macOS and Windows`.

### Task 7: Review and ship

- [ ] Final whole-branch review (most capable model), one fix wave, CI green on all three OSes.
- [ ] PR; merge with Simon's OK; Simon's live install is not changed by this (deploy-live.sh path), so no deploy beyond `git pull` of docs; draft the Nirmal update message with writing-as-simon for Simon's approval.
