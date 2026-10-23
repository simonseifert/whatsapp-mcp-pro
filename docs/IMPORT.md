# Importing WhatsApp chat exports

Linked-device history sync only delivers what the phone decides to send, so
older conversations stop at some date. The phone's own **Export chat** goes
further back. `lib.chat_import` turns such an export into rows of
`messages.db`, so `list_messages`, `recall`, `download_media` and transcription
see that history too.

It is a CLI on purpose. There is no MCP tool for it: writing history into the
store is a decision for the person who owns it, not for an agent.

## 1. Export on the phone

Open the chat, then **⋮ / contact name → More → Export chat**. Choose
**Include media** if you want photos, voice notes and documents. You get
`WhatsApp Chat with X.zip`, which holds `_chat.txt` (iOS) or
`WhatsApp Chat with X.txt` (Android) plus the media files. A bare `.txt`
works too; attachments are then looked up in the same folder.

Both iOS (`[23.10.26, 09:41:12] Name: text`) and Android
(`23/10/2026, 09:41 - Name: text`, 12h or 24h, any date order, including
Croatian `23. 10. 2026. 09:41` and German `23.10.26, 09:41`) are recognised.

## 2. Dry run

```bash
cd whatsapp-mcp-server
WA_STORE_PATH=~/.local/share/whatsapp-mcp-pro/store \
  uv run python -m lib.chat_import "WhatsApp Chat with Bob.zip" \
  --chat 4915112345678@s.whatsapp.net
```

Nothing is written without `--apply`. The report shows:

- the detected format (iOS/Android, date order, minute or second precision);
- who "me" is and how that was decided;
- every sender name and the JID it maps to, with the evidence, and the
  names it could not map;
- counts: lines parsed, system lines / deleted / omitted media skipped,
  messages already in the store (exact and fuzzy matches), already imported,
  new, new from unmapped senders, media to copy;
- a few of the new rows (`--sample 0` hides them).

Options:

| Option | Meaning |
|---|---|
| `--chat JID` | Required. The target chat. Must exist in the store or be a well-formed JID (`…@s.whatsapp.net`, `…@lid`, `…@g.us`); a new one gets a chat row. |
| `--me "Name"` | Your own name as the export shows it. Detected when omitted: from export lines that match messages the store already has as yours, then your account's push name, then (1:1 chats) by elimination. |
| `--tz Europe/Zagreb` | The phone's time zone when it exported. Exports carry local time with no offset. |
| `--date-order DMY\|MDY\|YMD` | Only needed when every date in the file reads both ways (`01/02`). One day above 12 anywhere decides it. |
| `--db PATH` | `messages.db` to use, if not the one under `WA_STORE_PATH`. |
| `--backup-dir DIR` | Where `--apply` writes its backup (default `~/backups/whatsapp`). |

## 3. Apply

Add `--apply`. The importer then:

1. takes an online backup of `messages.db` (`messages-preimport-<stamp>.db`);
2. copies attachments into `store/media/<chat>/<id><ext>`, the layout
   `/api/download` serves as `via: "saved"`;
3. inserts the new rows in one short `BEGIN IMMEDIATE` transaction.

**Safe while the bridge runs.** The bridge opens the store in WAL mode with a
5 s busy timeout; the import transaction holds the write lock for milliseconds.
If the bridge stored messages for this chat inside the imported time span
between the dry run and the write (a history sync landing), the import stops
and asks to be re-run, rather than writing rows it never compared.

## How rows are matched and marked

Exports have no message IDs and minute (Android) or second (iOS) timestamps.
A line counts as already stored when a row in the same chat (or its LID/phone
twin) is from the same sender and close in time:

- **exact**: same text (or, for media, a media row whose caption agrees),
  within two minutes of the exported time;
- **fuzzy**: anything from that sender inside the exported minute/second that
  nothing else claimed. This catches messages the bridge stores differently
  from the export (mentions as numbers, locations, polls, contact cards).

Imported rows get IDs `import-<hash>`, derived from chat, time, the exported
name, text and position, so re-running the same export imports nothing twice.
Unmapped group members are stored with sender `import:<name>`, never a guessed
JID; `list_messages` shows them with `sender_name_source: "chat_export"`.

Group members are mapped, in order, from: a name that is a phone number;
export lines that already match one person's stored messages; a directory or
nickname name only one person in the chat carries (agent-inferred nicknames do
not count).

## What the bridge does with imported rows

- `/api/download` serves their media from the copy the importer saved, checked
  against `file_sha256`, and never goes to the network for them.
- Reactions, quotes, edits and deletes targeting an `import-` ID are refused
  with 422: WhatsApp has never seen those IDs.
- On-demand history requests anchor on the oldest *real* message, so an import
  does not stop `request_history` from working.

## Limits

- Run `request_history` **before** importing. History the phone delivers later
  for the same period arrives with real IDs and is not merged with imported
  rows, so you would see both.
- System lines (group events, calls, security-code notices), deleted messages
  and "media omitted" placeholders are skipped. Reactions and replies are not
  in exports at all.
- During the autumn DST hour every local time occurs twice; the importer
  follows the transcript order, which is right unless the first message after
  the clock went back is later in wall time than the last one before it. The
  report counts such lines (`dst_ambiguous`).
- The Croatian and German placeholder strings (deleted, edited, omitted) are
  best effort; anything not recognised is imported as plain text.
- Recall embeds imported rows on its next indexing pass (every
  `RECALL_INDEX_INTERVAL`, default 10 min, under the shared server).
