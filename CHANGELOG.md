# Changelog

All notable changes to this fork are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions carry a
`-pro` suffix and name the upstream release they are based on.

## [Unreleased]

### Added

- `list_unnamed_contacts` and `set_contact_names`: an agent-driven loop for
  naming people known only by number. Names are stored in `contact_nicknames`
  (the `manage_nickname` store). Directory reads, `list_chats` and
  `list_messages` (new `sender_name` field) overlay live nicknames, so a name
  shows before the bridge's next directory rebuild. Names carry provenance
  (`contact_nicknames.source`, added by the MCP): `set_contact_names` requires
  `confirmed_by_user`; unconfirmed names are stored as `agent_inferred`, shown
  as "(unverified)", and can't replace a user nickname or an address-book name.
  Directory records expose `name_source`, messages `sender_name_source`.

## [v0.4.0-pro] - 2026-10-22

Based on upstream [v0.4.0](https://github.com/FelixIsaac/whatsapp-mcp-extended/tree/v0.4.0)
(FelixIsaac/whatsapp-mcp-extended). Everything upstream ships is included; this
entry lists what the pro layer adds on top, and the fixes from the October 2026
review.

### Added

- `recall`: multilingual semantic search over the full history, vectors stored
  in the bridge's SQLite, kept current by a background indexer. Model-scoped,
  CPU by default.
- `transcribe_audio` with pluggable backends: whisper.cpp server
  (`WHISPER_SERVER_URL`), mlx-whisper, faster-whisper and Groq.
  `WHISPER_BACKEND=auto` tries them in that order. `AUTO_TRANSCRIBE_VOICE`
  transcribes incoming voice notes in the background into the store.
- `check_inbox`: "what arrived since I last asked", with a cursor that starts
  at now.
- Identity directory: an `identities` table joining phone JIDs and LIDs into one
  row per person, group, newsletter or broadcast list, exposed as
  `lookup_identity`, `search_directory` and the `whatsapp://directory` resource.
- LID chat merging (`MERGE_LID_CHATS`, on by default): chats split across a LID
  and a phone JID are folded into one; merged rows are kept and stamped with
  `chats.merged_into`.
- Shared streamable-HTTP MCP server (`serve_http.py`) with scoped bearer
  tokens: full, and read-only limited to tools annotated `readOnlyHint=true`.
- Media retry for expired media (`MEDIA_RETRY_ENABLED`, opt-in): asks the
  sender's phone to re-upload when the CDN copy is gone.
- Headless pairing: the current QR payload is written to `store/pair-qr.txt`
  while the bridge waits for a scan.
- Poll votes decrypted and stored, `get_poll_results` tool.
- `request_history` finds its anchor message automatically, and history can be
  backfilled in the background.
- `WEBHOOK_ALLOWED_ADDRS`: allow specific private webhook targets without
  turning off the SSRF check.
- `scripts/deploy-live.sh`: build to a side file, run Go and Python tests, back
  up the store online, swap the binary, restart, and roll back if any bridge on
  the binary fails to report `connected` within two minutes. Covers every bridge
  that runs the binary, not only the primary.
- Quote and forward context stored for every message.

### Changed

- MCP server ported to mcp 2.x (`MCPServer`).
- whatsmeow updated to 2026-10-17; bridge built with Go 1.26, toolchain 1.27.3.
- The bridge exits with code 3 on "Stream replaced", so a supervisor can stop
  restarting into a session another process has taken
  (`RestartPreventExitStatus=3`).
- Media retry is opt-in, since every retry shows "Finished syncing" on the
  user's phone.
- Authenticated loopback callers are exempt from the bridge's rate limit.
- CI builds golangci-lint v2 with the toolchain from `go.mod`; the errcheck and
  govet findings it surfaced are fixed.
- Stale macOS binaries and `.bak` files are no longer tracked.

### Fixed

- Message writes are upserts that keep existing transcripts and refreshed media
  fields when WhatsApp re-delivers a message.
- `recall` date filters compare UTC instants, and the embedding model loads from
  the local cache instead of contacting the hub at startup.
- Message timestamp filters in the MCP server compare instants, not text.
- Forced reconnects go through `ResetConnection()`, so auto-reconnect and the
  watchdog stay armed afterwards.
- The first connect after boot retries with backoff and hands a failure to the
  watchdog, instead of leaving a live process that ingests nothing when DNS was
  not ready yet.
- Two media files saved in the same second no longer serve the wrong message:
  saved files are checked against their hash, and a colliding name gets the
  message ID added.
- Auto-download paths stay inside the chat's directory and never overwrite the
  alternate file.
- Every MCP entry point that takes a chat JID follows `chats.merged_into`, so a
  merged LID still finds its messages.
- `check_inbox` cursor writes are atomic and locked, so concurrent calls can't
  lose or corrupt the cursor.
- `deploy-live.sh` tests the bridge before replacing the live binary.

### Security

- Media guard in both the bridge and the MCP server: local paths for
  `send_file` and `send_audio_message` must be in `/tmp` or `MEDIA_ALLOWED_DIRS`,
  and even there dotfiles, dot-directories, `.env`, `.pem`, `.key`, SSH keys,
  credential files, any SQLite database and its WAL/SHM/journal sidecars, and
  files at the top level of a `store/` directory are refused. Symlinks are
  resolved before checking; `store/` segments match case-insensitively.
- `send_audio_message` validates its source file before converting it.
- Two exact-match send allowlists (`SEND_ALLOWED_JIDS`,
  `WHATSAPP_ALLOWLIST_JIDS`); no substring or prefix matching.

[v0.4.0-pro]: https://github.com/simonseifert/whatsapp-mcp-pro/compare/v0.4.0...main
