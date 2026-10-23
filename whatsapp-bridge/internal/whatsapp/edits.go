package whatsapp

import (
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
	out, err := c.applyChange(store, ch, c.chatCandidates(msg.Info.Chat), sender, msg.Info.IsFromMe, database.SourceLive)
	if err != nil {
		c.logger.Warnf("Failed to store %s of %s: %v", ch.Kind, ch.TargetID, err)
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
		by := sender
		if wmi.GetMessageStubType() == waWeb.WebMessageInfo_ADMIN_REVOKE {
			by = "" // the stub names the author, not the admin who deleted it
		}
		if _, err := c.applyChange(store, ch, c.historyChats(chatJID, jid), by, isFromMe, database.SourceHistory); err != nil {
			c.logger.Warnf("Failed to store history %s of %s: %v", ch.Kind, ch.TargetID, err)
		}
		return true
	}
	at := time.Unix(int64(wmi.GetMessageTimestamp()), 0)
	// Live calls are filed under the canonical chat; match that so a stub or
	// call-log entry finds the call the bridge already saw.
	callChat := c.chatCandidates(jid)[0]
	if isVideo, isGroup, ok := historyMissedCall(wmi); ok && wmi.GetKey().GetID() != "" {
		c.recordCallLog(store, database.CallEvent{
			CallID:    wmi.GetKey().GetID(),
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
	if evt != nil && evt.Message.GetCallLogMesssage() != nil && wmi.GetKey().GetID() != "" {
		c.recordCallLog(store, callLogEvent(evt.Message.GetCallLogMesssage(), wmi.GetKey().GetID(), callChat, at, isFromMe, database.SourceHistory))
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
			at := msg.Info.Timestamp
			if ms := pm.GetTimestampMS(); ms > 0 {
				at = time.UnixMilli(ms)
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
		return messageChange{
			Kind:       changeEdit,
			TargetID:   msg.Info.ID,
			NewContent: ExtractTextContent(msg.Message),
			At:         msg.NewsletterMeta.EditTS,
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
	Applied    bool // an edit that changed the current text, or a first revoke
}

// applyChange stores an edit or revoke. sender is who sent the change event,
// which is the editor, or for a revoke the one who deleted (the author, or a
// group admin).
func (c *Client) applyChange(store *database.MessageStore, ch messageChange, chats []string, sender string, fromMe bool, source string) (ChangeOutcome, error) {
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
			Editor:     sender,
			EditorIsMe: fromMe,
			StanzaID:   ch.StanzaID,
			Source:     source,
		})
		if err != nil {
			return out, err
		}
		out.ChatJID, out.OldContent, out.Applied = res.ChatJID, res.OldContent, res.Applied && !res.Duplicate
		return out, nil
	case changeRevoke:
		chatJID, newly, err := store.MarkDeleted(ch.TargetID, chats, ch.At, sender)
		if err != nil {
			return out, err
		}
		out.ChatJID, out.Applied = chatJID, newly
		return out, nil
	}
	return out, nil
}
