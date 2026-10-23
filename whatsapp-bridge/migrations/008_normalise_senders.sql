-- Migration 008: Give bare messages.sender values their JID server
-- Target: store/messages.db
-- Date: 2026-10-23
-- Backward compatible: YES (rewrites only bare-digit senders, adds no schema)
--
-- The live handler used to store msg.Info.Sender.User, so `sender` mixes bare
-- user parts ("4915112345678", or a LID number) with full JIDs written by the
-- history-sync and send paths. A bare value cannot be told apart as phone or
-- LID without a lookup, and code that turned it into a JID guessed: reactions
-- and quotes in groups went out with a participant that had no server and
-- silently targeted nothing. The bridge now stores full JIDs; this fixes the
-- rows written before that.
--
-- Only rows with evidence are touched, in this order:
--   1. the sender is the other side of a non-group chat: take the chat JID
--      (this also covers newsletters, whose posts carry the newsletter id);
--   2. the digits are a LID the identity directory knows: append @lid;
--   3. the digits are a phone number the directory knows: append @s.whatsapp.net.
-- The LID is kept as a LID rather than mapped to the phone JID: it is what the
-- message was actually addressed from, and it stays resolvable through
-- `identities`. Anything left bare is unknown to the directory and is resolved
-- at use time instead (bare digits = phone unless they match a known LID).
--
-- Idempotent: every statement only matches senders that are still bare digits.
-- Re-running after the directory learns more mappings normalises more rows.
-- The bridge applies this itself on start (normaliseSendersSQL in
-- internal/database/senders.go, kept identical to this file); the file is here
-- for running against a store by hand.

UPDATE messages
SET sender = chat_jid
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND chat_jid NOT LIKE '%@g.us'
  AND instr(chat_jid, '@') > 0
  AND substr(chat_jid, 1, instr(chat_jid, '@') - 1) = sender;

UPDATE messages
SET sender = sender || '@lid'
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND EXISTS (SELECT 1 FROM identities i WHERE i.lid = messages.sender || '@lid');

UPDATE messages
SET sender = sender || '@s.whatsapp.net'
WHERE sender <> '' AND instr(sender, '@') = 0 AND sender NOT GLOB '*[^0-9]*'
  AND EXISTS (
    SELECT 1 FROM identities i
    WHERE i.phone_jid = messages.sender || '@s.whatsapp.net'
       OR i.jid = messages.sender || '@s.whatsapp.net'
       OR i.phone = messages.sender
  );
