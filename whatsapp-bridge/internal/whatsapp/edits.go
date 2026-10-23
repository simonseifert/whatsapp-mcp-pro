package whatsapp

import (
	"context"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
	bridgeTypes "whatsapp-bridge/internal/types"
)

// changeEventSink is the part of the webhook manager that delivers
// message_edited, message_deleted and call events; an interface because
// HandleMessage takes the manager untyped.
type changeEventSink interface {
	DeliverEvent(eventType string, info bridgeTypes.WebhookMessageInfo, call *bridgeTypes.WebhookCallInfo)
}

func emitChange(webhookManager any, eventType string, info bridgeTypes.WebhookMessageInfo, call *bridgeTypes.WebhookCallInfo) {
	if sink, ok := webhookManager.(changeEventSink); ok && sink != nil {
		sink.DeliverEvent(eventType, info, call)
	}
}

// handleChange applies a live edit or revoke and tells the webhooks about it.
func (c *Client) handleChange(store *database.MessageStore, webhookManager any, msg *events.Message, ch messageChange, sender string) {
	actor := c.messageActor(msg, sender)
	if ch.Kind == changeRevoke {
		ch.Author = c.revokedAuthor(msg, actor)
	}
	isAdmin := c.adminCheck(msg.Info.Chat, msg.Info.Sender, msg.Info.SenderAlt, msg.Info.IsFromMe)
	out, err := c.applyChange(store, ch, c.chatCandidates(msg.Info.Chat), actor, isAdmin, database.SourceLive, false)
	if err != nil {
		c.logger.Warnf("Failed to store %s of %s: %v", ch.Kind, ch.TargetID, err)
		return
	}
	if out.Refused != "" {
		c.logger.Warnf("Ignored %s of %s in %s from %s: %s", ch.Kind, ch.TargetID, msg.Info.Chat, sender, out.Refused)
		return
	}
	if !out.Applied {
		return
	}
	c.logger.Infof("Stored %s of %s in %s", ch.Kind, out.MessageID, out.ChatJID)
	info := bridgeTypes.WebhookMessageInfo{
		ID:        out.MessageID,
		ChatJID:   out.ChatJID,
		Sender:    sender,
		Timestamp: ch.At.Format(time.RFC3339),
		PushName:  msg.Info.PushName,
		IsFromMe:  msg.Info.IsFromMe,
	}
	eventType := "message_deleted"
	if ch.Kind == changeEdit {
		eventType = "message_edited"
		info.Content = out.NewContent
		info.PreviousContent = out.OldContent
	}
	emitChange(webhookManager, eventType, info, nil)
}

// historyChange is classifyChange for a history-sync message. A message
// revoked before the sync arrives as a REVOKE stub keyed on the original
// message (an admin's as ADMIN_REVOKE), not as a protocol message; edits and
// revokes still in the history as protocol messages go through the same
// unwrapping as live ones (evt is unwrapHistory's result).
func historyChange(wmi *waWeb.WebMessageInfo, evt *events.Message) messageChange {
	if wmi == nil {
		return messageChange{}
	}
	switch wmi.GetMessageStubType() {
	case waWeb.WebMessageInfo_REVOKE, waWeb.WebMessageInfo_ADMIN_REVOKE:
		id := wmi.GetKey().GetID()
		if id == "" {
			return messageChange{}
		}
		at := time.Unix(int64(wmi.GetMessageTimestamp()), 0)
		if ts := wmi.GetRevokeMessageTimestamp(); ts > 0 {
			at = time.Unix(int64(ts), 0)
		}
		return messageChange{Kind: changeRevoke, TargetID: id, At: at, StanzaID: id}
	}
	return classifyChange(evt)
}

// unwrapHistory strips the same envelopes (ephemeral, device-sent, edited...)
// from a history message that whatsmeow strips from a live one. nil when the
// entry has no message (a stub).
func unwrapHistory(wmi *waWeb.WebMessageInfo) *events.Message {
	if wmi == nil || wmi.GetMessage() == nil {
		return nil
	}
	return (&events.Message{
		Info:       types.MessageInfo{ID: wmi.GetKey().GetID(), Timestamp: time.Unix(int64(wmi.GetMessageTimestamp()), 0)},
		RawMessage: wmi.GetMessage(),
	}).UnwrapRaw()
}

// historyMissedCall reads a "missed call" stub from history sync. The
// history carries no other trace of calls the phone handled while no
// companion was connected.
func historyMissedCall(wmi *waWeb.WebMessageInfo) (isVideo, isGroup, ok bool) {
	switch wmi.GetMessageStubType() {
	case waWeb.WebMessageInfo_CALL_MISSED_VOICE:
		return false, false, true
	case waWeb.WebMessageInfo_CALL_MISSED_VIDEO:
		return true, false, true
	case waWeb.WebMessageInfo_CALL_MISSED_GROUP_VOICE:
		return false, true, true
	case waWeb.WebMessageInfo_CALL_MISSED_GROUP_VIDEO:
		return true, true, true
	}
	return false, false, false
}

// historyChats is the chat candidate list for a history message: the raw
// conversation ID first, because history sync files messages under it, then
// the other forms a live message may have used.
func (c *Client) historyChats(raw string, jid types.JID) []string {
	out := []string{raw}
	for _, cand := range c.chatCandidates(jid) {
		if cand != raw {
			out = append(out, cand)
		}
	}
	return out
}

// handleHistoryNonMessage stores what a history-sync entry says about other
// messages or calls: an edit, a revoke, a missed-call stub or the phone's
// call-log entry. It returns true when the entry was one of those and must not
// also be stored as a message of its own.
func (c *Client) handleHistoryNonMessage(store *database.MessageStore, chatJID string, jid types.JID, wmi *waWeb.WebMessageInfo) bool {
	evt := unwrapHistory(wmi)
	sender, isFromMe := c.historyMessageSender(jid, wmi.GetKey())
	if ch := historyChange(wmi, evt); ch.Kind != changeNone {
		actor := database.Actor{JID: sender, IsMe: isFromMe}
		// A revoke stub is the phone's own record of a revoke the server
		// accepted; an edit or revoke still in protocol-message form is only
		// what someone sent, and gets the same checks as a live one.
		stub := wmi.GetMessage() == nil
		// A stub is keyed on the revoked message, so its key names the author.
		ch.Author = database.Actor{JID: sender, IsMe: isFromMe}
		if wmi.GetMessageStubType() == waWeb.WebMessageInfo_ADMIN_REVOKE {
			actor.JID = "" // the stub names the author, not the admin who deleted it
		}
		var isAdmin func() bool
		if p, err := types.ParseJID(sender); err == nil && !stub {
			isAdmin = c.adminCheck(jid, p, types.EmptyJID, isFromMe)
		}
		out, err := c.applyChange(store, ch, c.historyChats(chatJID, jid), actor, isAdmin, database.SourceHistory, stub)
		if err != nil {
			c.logger.Warnf("Failed to store history %s of %s: %v", ch.Kind, ch.TargetID, err)
		} else if out.Refused != "" {
			c.logger.Warnf("Ignored history %s of %s in %s: %s", ch.Kind, ch.TargetID, chatJID, out.Refused)
		}
		return true
	}
	at := time.Unix(int64(wmi.GetMessageTimestamp()), 0)
	// Live calls are filed under the canonical chat; match that so a stub or
	// call-log entry finds the call the bridge already saw.
	callChat := c.chatCandidates(jid)[0]
	if isVideo, isGroup, ok := historyMissedCall(wmi); ok && wmi.GetKey().GetID() != "" {
		c.recordCallLog(store, database.CallEvent{
			CallID:    "stub:" + wmi.GetKey().GetID(),
			Kind:      database.CallEventMissed,
			ChatJID:   callChat,
			CallerJID: sender,
			Direction: "incoming",
			IsVideo:   isVideo,
			IsGroup:   isGroup,
			At:        at,
			Source:    database.SourceHistory,
		})
		return true
	}
	if evt != nil && evt.Message.GetCallLogMesssage() != nil {
		// Only the user's own phone writes call-log entries; one from anyone
		// else is a forgery and is dropped like any other non-message.
		if isFromMe && wmi.GetKey().GetID() != "" {
			c.recordCallLog(store, callLogEvent(evt.Message.GetCallLogMesssage(), wmi.GetKey().GetID(), callChat, at, database.SourceHistory))
		}
		return true
	}
	return false
}

// Change kinds a message event can carry instead of new content.
const (
	changeNone   = ""
	changeEdit   = "edit"
	changeRevoke = "revoke"
)

// messageChange is an edit or revoke extracted from a message event.
type messageChange struct {
	Kind       string
	TargetID   string // the message being edited or revoked
	NewContent string // edits only
	At         time.Time
	StanzaID   string // the edit's own message ID, the dedupe key
	// Author wrote the revoked message, as the revoke's key names it; only
	// for the stand-in row of a revoke of a message not stored. May be empty.
	Author database.Actor
}

// classifyChange reports whether msg edits or revokes an earlier message.
//
// whatsmeow unwraps the EditedMessage envelope (setting IsEdit) but leaves
// the ProtocolMessage inside it, so a normal edit and an older client's bare
// MESSAGE_EDIT protocol message look the same here. Before this existed both,
// and revokes, fell through to ExtractTextContent, found no text and were
// dropped, leaving the store with the original as if nothing had happened.
//
// Newsletters are the exception: an edited post arrives as the new content
// under the original ID, flagged only by NewsletterMeta.EditTS.
func classifyChange(msg *events.Message) messageChange {
	if msg == nil || msg.Message == nil {
		return messageChange{}
	}
	if pm := msg.Message.GetProtocolMessage(); pm != nil {
		target := pm.GetKey().GetID()
		switch pm.GetType() {
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			if target == "" {
				return messageChange{}
			}
			// TimestampMS is the sender's claim; Info.Timestamp is when the
			// server took the edit. A claim later than that would make a forged
			// or replayed edit outrank every genuine one, so it is clamped.
			at := msg.Info.Timestamp
			if ms := pm.GetTimestampMS(); ms > 0 {
				if claimed := time.UnixMilli(ms); at.IsZero() || claimed.Before(at) {
					at = claimed
				}
			}
			return messageChange{
				Kind:       changeEdit,
				TargetID:   target,
				NewContent: ExtractTextContent(pm.GetEditedMessage()),
				At:         at,
				StanzaID:   msg.Info.ID,
			}
		case waE2E.ProtocolMessage_REVOKE:
			if target == "" {
				return messageChange{}
			}
			return messageChange{Kind: changeRevoke, TargetID: target, At: msg.Info.Timestamp, StanzaID: msg.Info.ID}
		}
		return messageChange{}
	}
	if msg.NewsletterMeta != nil && !msg.NewsletterMeta.EditTS.IsZero() {
		at := msg.NewsletterMeta.EditTS
		if now := time.Now(); at.After(now) {
			at = now
		}
		return messageChange{
			Kind:       changeEdit,
			TargetID:   msg.Info.ID,
			NewContent: ExtractTextContent(msg.Message),
			At:         at,
			StanzaID:   msg.Info.ID + "@" + msg.NewsletterMeta.EditTS.UTC().Format(time.RFC3339Nano),
		}
	}
	return messageChange{}
}

// chatCandidates lists the forms a chat may be stored under, canonical first:
// a 1:1 conversation can still be split between its LID and phone JID.
func (c *Client) chatCandidates(chat types.JID) []string {
	canonical, alt := c.ChatJIDForms(chat)
	out := []string{canonical.String()}
	if !alt.IsEmpty() {
		out = append(out, alt.String())
	}
	if raw := chat.ToNonAD().String(); raw != out[0] && (len(out) < 2 || raw != out[1]) {
		out = append(out, raw)
	}
	return out
}

// StoredChatForms lists the forms chatJID may be stored under, canonical
// first; an unparsable JID is returned as is.
func (c *Client) StoredChatForms(chatJID string) []string {
	jid, err := types.ParseJID(chatJID)
	if err != nil || jid.User == "" {
		return []string{chatJID}
	}
	return c.chatCandidates(jid)
}

// OwnJID is the linked account's phone JID without a device suffix, or "".
func (c *Client) OwnJID() string {
	if c == nil || c.Client == nil || c.Store == nil || c.Store.ID == nil {
		return ""
	}
	return c.Store.ID.ToNonAD().String()
}

// ChangeOutcome is what applying an edit or revoke did, for logging and the
// webhook.
type ChangeOutcome struct {
	Kind       string
	ChatJID    string
	MessageID  string
	OldContent string
	NewContent string
	Applied    bool   // an edit that changed the current text, or a first revoke
	Refused    string // why a change was not applied: not genuine, or not checkable
}

// applyChange stores an edit or revoke. actor is whoever sent the change
// event; isAdmin (may be nil) says whether they administer the group, for a
// revoke of someone else's message. trusted skips the authorship checks.
func (c *Client) applyChange(store *database.MessageStore, ch messageChange, chats []string, actor database.Actor,
	isAdmin func() bool, source string, trusted bool) (ChangeOutcome, error) {
	out := ChangeOutcome{Kind: ch.Kind, MessageID: ch.TargetID, NewContent: ch.NewContent}
	switch ch.Kind {
	case changeEdit:
		if ch.NewContent == "" {
			// An edit of something ExtractTextContent cannot read (a poll, say);
			// recording an empty "new text" would blank the message.
			return out, nil
		}
		res, err := store.ApplyEdit(database.Edit{
			MessageID:  ch.TargetID,
			ChatJIDs:   chats,
			NewContent: ch.NewContent,
			EditedAt:   ch.At,
			Editor:     actor,
			StanzaID:   ch.StanzaID,
			Source:     source,
			Trusted:    trusted,
		})
		if err != nil {
			return out, err
		}
		out.ChatJID, out.OldContent, out.Applied = res.ChatJID, res.OldContent, res.Applied && !res.Duplicate
		if res.Rejected && !res.Duplicate {
			out.Refused = "editor is not the author"
		}
		return out, nil
	case changeRevoke:
		res, err := store.MarkDeleted(database.Revoke{
			MessageID: ch.TargetID,
			ChatJIDs:  chats,
			At:        ch.At,
			By:        actor,
			Author:    ch.Author,
			IsAdmin:   isAdmin,
			Trusted:   trusted,
		})
		if err != nil {
			return out, err
		}
		out.ChatJID, out.Applied = res.ChatJID, res.Newly
		switch {
		case res.Rejected:
			out.Refused = "revoker is neither the author nor a group admin"
		case res.Ignored:
			out.Refused = "message not stored and revoker not a group admin"
		}
		return out, nil
	}
	return out, nil
}

// messageActor is who sent a live edit or revoke, in every form the store
// may have their messages under. A newsletter post can only come from the
// newsletter itself, through WhatsApp's servers.
func (c *Client) messageActor(msg *events.Message, sender string) database.Actor {
	if msg.Info.Chat.Server == types.NewsletterServer {
		return database.Actor{JID: msg.Info.Chat.String(), IsMe: msg.Info.IsFromMe}
	}
	a := database.Actor{JID: sender, IsMe: msg.Info.IsFromMe}
	if alt := msg.Info.SenderAlt; !alt.IsEmpty() {
		a.Forms = append(a.Forms, StoredSender(alt))
	}
	if c != nil && c.Client != nil && c.Store != nil && !msg.Info.Sender.IsEmpty() {
		if alt, err := c.Store.GetAltJID(context.Background(), msg.Info.Sender.ToNonAD()); err == nil && !alt.IsEmpty() {
			a.Forms = append(a.Forms, alt.ToNonAD().String())
		}
	}
	return a
}

// revokedAuthor reads who wrote the message a live revoke names. The key is
// written from the revoker's point of view: FromMe means the revoker's own
// message; otherwise a group key names the author as Participant. A 1:1 key
// without FromMe is left unknown rather than guessed.
func (c *Client) revokedAuthor(msg *events.Message, revoker database.Actor) database.Actor {
	key := msg.Message.GetProtocolMessage().GetKey()
	if key == nil {
		return database.Actor{}
	}
	if key.GetFromMe() {
		return database.Actor{JID: revoker.JID, IsMe: revoker.IsMe}
	}
	p, err := types.ParseJID(key.GetParticipant())
	if err != nil || p.User == "" {
		return database.Actor{}
	}
	author := database.Actor{JID: StoredSender(p)}
	if c != nil && c.Client != nil && c.Store != nil {
		own := []types.JID{c.Store.LID.ToNonAD()}
		if c.Store.ID != nil {
			own = append(own, c.Store.ID.ToNonAD())
		}
		for _, o := range own {
			if o.User != "" && o.User == p.User && o.Server == p.Server {
				author.IsMe = true
			}
		}
	}
	return author
}

// newsletterEditOfUnstored reports whether ch is a newsletter post's edit
// flag on a post the store does not have. A newsletter delivers an edited post
// as the whole post under its original ID, so when the original was missed it
// is the only copy there is: it is stored as a normal message, as before edits
// were tracked, rather than left waiting as a pending edit of nothing.
func newsletterEditOfUnstored(store *database.MessageStore, msg *events.Message, ch messageChange, chats []string) bool {
	if ch.Kind != changeEdit || msg.NewsletterMeta == nil || msg.Message.GetProtocolMessage() != nil || store == nil {
		return false
	}
	_, found, err := store.LookupMessageAuthor(ch.TargetID, chats)
	return err == nil && !found
}

// OwnGroupAdmin reports whether the linked account administers group chatJID.
// The API's /api/delete asks before revoking someone else's message, using
// the same cached group info the revoke check uses. False for a non-group.
func (c *Client) OwnGroupAdmin(chatJID string) bool {
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return false
	}
	if check := c.adminCheck(chat, types.EmptyJID, types.EmptyJID, true); check != nil {
		return check()
	}
	return false
}

// adminCheck returns a lazy "is this sender a group admin" for a revoke in a
// group, nil elsewhere. Lazy because it may cost a group-info query, and only
// a revoke of someone else's message needs it.
func (c *Client) adminCheck(chat, sender, senderAlt types.JID, fromMe bool) func() bool {
	if chat.Server != types.GroupServer || c == nil || c.Client == nil {
		return nil
	}
	return func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		info, err := c.cachedGroupInfo(ctx, chat)
		if err != nil {
			c.logger.Warnf("Could not check admins of %s for a revoke: %v", chat, err)
			return false
		}
		jids := []types.JID{sender.ToNonAD(), senderAlt.ToNonAD()}
		if fromMe && c.Store != nil {
			if c.Store.ID != nil {
				jids = append(jids, c.Store.ID.ToNonAD())
			}
			jids = append(jids, c.Store.LID.ToNonAD())
		}
		return participantIsAdmin(info, jids...)
	}
}

// participantIsAdmin reports whether any of jids is an admin of the group.
// Participants carry both a LID and a phone JID; the sender may be either.
func participantIsAdmin(info *types.GroupInfo, jids ...types.JID) bool {
	if info == nil {
		return false
	}
	for _, p := range info.Participants {
		if !p.IsAdmin && !p.IsSuperAdmin {
			continue
		}
		for _, j := range jids {
			if j.IsEmpty() || j.User == "" {
				continue
			}
			for _, form := range []types.JID{p.JID, p.LID, p.PhoneNumber} {
				if form.User == j.User && form.Server == j.Server {
					return true
				}
			}
		}
	}
	return false
}
