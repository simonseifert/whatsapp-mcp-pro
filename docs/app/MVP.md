# wa-app: milestones and first-version cut

Status: approved by Simon, 2026-11-21. Each milestone ends with a fresh-context review
and counts as done only after the maintainer has used it on his real data and phone.

## M0: bridge groundwork (Go, no UI)
- **Security scan:** Go toolchain 1.27.4, which fixes the failing scan (GO-2026-6859).
- **Upstream:** pull upstream's 2 new commits (issue #7) first, as a separate commit,
  before any M0 change.
- **New stored data:**
  - reactions linked to their message;
  - pinned, muted and archived state from app-state;
  - group and community metadata (parent link, announce-only, admins);
  - stickers as media;
  - read events from your own devices, so unread counts follow the phone.
- Migrations `011+`, tests, CHANGELOG, deploy.
- **Done when:** the MCP tools and existing consumers still work, and the new tables
  fill on the live store.

## M1: the chat app, read and reply
- **Bridge:** contact cards keep their number and details as searchable text, not
  only `[Contact: name]`, so the app can show them as a card (idea from
  verygoodplugins/whatsapp-mcp; Simon, 2026-11-22).
- **Server:** login, a read-only store reader, live updates over SSE, read state and
  unread counts, the avatar cache, media and thumbnails.
- **Screens:**
  - **Chats list:** search, filter chips (All, Unread, Favourites, Groups), pinned
    first, muted and archived.
  - **Chat view:** text, media, replies, forwarded, edited and deleted messages,
    reactions, polls.
  - **Voice notes:** play, plus "Show text" with on-demand transcription.
- **Sending:** text, replies, reactions, read receipts. Every send goes through the
  bridge's safety checks.
- PWA install, dark and light themes, mobile-first. A desktop two-pane layout as well.
- **Done when:** the maintainer reads and answers his day on his phone through it for
  a week.

## M2: AI drafts
- **Per-chat settings:** on, backend, local-only, note.
- **Three backends:** OpenAI-compatible (incl. Ollama), Anthropic API, `claude` CLI.
- **The draft card:** debounced when away, instant when the app is open, stale drafts
  cleared.
- **Learning:** past replies as examples, edit outcomes, distilled per-chat rules you
  can read and edit, optional thumbs.
- **Done when:** more than half of replies in opted-in chats start from a draft for a
  week.

## M3: the rest of WhatsApp-parity
- **Push notifications:** spike pywebpush against Apple first.
- **Sending:** voice notes recorded in the app, and attachments (photo, file,
  document).
- **Search:** keyword search plus search by meaning, from the chats list and inside a
  chat.
- **Private reply:** reply privately to a group message, quoting it in the sender's
  own chat (idea from GOWA; Simon, 2026-11-22). Needs a bridge change to quote across
  chats.
- **Contacts and groups:** contact and group info from the directory, a Communities
  tab, a call log tab, and a polls UI.
- **Done when:** the maintainer goes a week without needing WhatsApp on the phone for
  anything but calls and statuses.

## Later (not in the first version)
- Viewing statuses.
- Scheduled send.
- A daily digest.
- A chat-import UI.
- Upstream PRs for reactions and communities.
- An installer and Docker image for the app.
- A demo mode with fictional data for screenshots.
