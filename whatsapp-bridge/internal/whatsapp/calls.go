package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
	bridgeTypes "whatsapp-bridge/internal/types"
)

// callJIDs is what callEventFrom needs to know about identities: which JIDs
// are the user's own, and how a LID maps to the phone JID the store files
// chats under. A struct of funcs so the translation is testable offline.
type callJIDs struct {
	isOwn     func(types.JID) bool
	normalize func(types.JID) types.JID
	// inGroup reports whether member (either JID form) is in group, from the
	// cached group info. nil, or false when it cannot be checked.
	inGroup func(group, member types.JID) bool
}

// sameAccount reports whether a and b are one account, directly or through
// the LID map. Never by equal digits: a LID's digits can be someone else's
// phone number.
func (ids callJIDs) sameAccount(a, b types.JID) bool {
	if a.IsEmpty() || b.IsEmpty() {
		return false
	}
	a, b = a.ToNonAD(), b.ToNonAD()
	if a == b {
		return true
	}
	return ids.normalize != nil && ids.normalize(a) == ids.normalize(b)
}

// callEventFrom translates a whatsmeow call event into a database.CallEvent.
// ok is false for event types that carry nothing for the call log (relay
// latency, transport, pre-accept).
//
// Who is who: From is the device that sent this stanza, CallCreator the
// account that placed the call. An accept or reject from one of the user's
// own devices means the phone handled it (ByMe). The chat is the group for a
// group call, otherwise whichever side is not the user.
func callEventFrom(evt any, ids callJIDs) (database.CallEvent, bool) {
	var meta types.BasicCallMeta
	ev := database.CallEvent{Source: database.SourceLive}
	switch v := evt.(type) {
	case *events.CallOffer:
		meta, ev.Kind = v.BasicCallMeta, database.CallEventOffer
		// A video offer carries a <video> codec element next to <audio>.
		if v.Data != nil {
			_, ev.IsVideo = v.Data.GetOptionalChildByTag("video")
		}
	case *events.CallOfferNotice:
		meta, ev.Kind = v.BasicCallMeta, database.CallEventOffer
		ev.IsVideo = v.Media == "video"
		ev.IsGroup = v.Type == "group"
	case *events.CallAccept:
		meta, ev.Kind = v.BasicCallMeta, database.CallEventAccept
	case *events.CallReject:
		meta, ev.Kind = v.BasicCallMeta, database.CallEventReject
	case *events.CallTerminate:
		meta, ev.Kind = v.BasicCallMeta, database.CallEventTerminate
		ev.Reason = v.Reason
	default:
		return database.CallEvent{}, false
	}
	if meta.CallID == "" {
		return database.CallEvent{}, false
	}
	ev.CallID = meta.CallID
	ev.At = meta.Timestamp
	if ev.At.IsZero() {
		ev.At = time.Now()
	}

	// Only From is the server's word; CallCreator, CallCreatorAlt and GroupJID
	// are attributes the sending client wrote. Believe the creator when From is
	// one of our own devices or the creator is From's own account. A stranger
	// claiming the call was ours is not believed either: that is how a forged
	// "outgoing call" would land in their chat. An unbelieved offer is filed as
	// From's own call; any other unbelieved event may only update a call
	// already recorded (a callee's or third participant's accept/terminate
	// legitimately names someone else as creator, but must not plant a call).
	from := meta.From.ToNonAD()
	fromOwn := ids.isOwn(from)
	creator, creatorAlt := meta.CallCreator, meta.CallCreatorAlt
	if creator.IsEmpty() {
		creator, creatorAlt = meta.From, types.EmptyJID
	}
	creator = creator.ToNonAD()
	if !fromOwn && !ids.sameAccount(creatorAlt, from) {
		// The alt is what CallerJID prefers, so it must pass on its own: a
		// creator matching From says nothing about an alt naming someone else.
		creatorAlt = types.EmptyJID
	}
	vouched := fromOwn || ids.sameAccount(creator, from)
	if !vouched {
		if ev.Kind == database.CallEventOffer {
			creator, creatorAlt = from, types.EmptyJID
		} else {
			ev.UpdateOnly = true
		}
	}
	meta.CallCreatorAlt = creatorAlt
	fromMeCreator := ids.isOwn(creator) || (!creatorAlt.IsEmpty() && ids.isOwn(creatorAlt))
	if fromMeCreator {
		ev.Direction = "outgoing"
	} else {
		ev.Direction = "incoming"
		// The alt form is the phone JID when the creator came as a LID.
		if creator.Server == types.HiddenUserServer && meta.CallCreatorAlt.Server == types.DefaultUserServer {
			ev.CallerJID = meta.CallCreatorAlt.ToNonAD().String()
		} else {
			ev.CallerJID = ids.normalize(creator).String()
		}
	}
	if ev.Kind == database.CallEventAccept || ev.Kind == database.CallEventReject {
		ev.ByMe = ids.isOwn(meta.From.ToNonAD())
	}

	switch {
	case !meta.GroupJID.IsEmpty():
		group := meta.GroupJID.ToNonAD()
		ev.ChatJID = group.String()
		ev.IsGroup = true
		// A group call is filed in the group only for a sender who is in it;
		// an unverified one may still update a call already recorded there.
		if !fromOwn && (ids.inGroup == nil || !ids.inGroup(group, from)) {
			ev.UpdateOnly = true
		}
	case !fromMeCreator:
		ev.ChatJID = ev.CallerJID
	case !meta.From.IsEmpty() && !ids.isOwn(meta.From.ToNonAD()):
		ev.ChatJID = ids.normalize(meta.From.ToNonAD()).String()
	}
	return ev, true
}

// callLogEvent turns the phone's call-log message into a CallEvent. The phone
// sends one to its companions after each call; it is the only source of a
// duration for a call answered on the phone. Callers accept it only from the
// user's own devices. It says nothing about direction (the phone sends it
// for both), so it leaves that to the live events.
//
// The ID is the message's, namespaced so it can never coincide with a live
// call ID; RecordCallEvent also refuses to fold it into a call in another chat.
func callLogEvent(log *waE2E.CallLogMessage, msgID, chatJID string, at time.Time, source string) database.CallEvent {
	ev := database.CallEvent{
		CallID:   "log:" + msgID,
		Kind:     database.CallEventLog,
		ChatJID:  chatJID,
		IsVideo:  log.GetIsVideo(),
		IsGroup:  len(log.GetParticipants()) > 1,
		At:       at,
		Duration: time.Duration(log.GetDurationSecs()) * time.Second,
		Source:   source,
	}
	switch log.GetCallOutcome() {
	case waE2E.CallLogMessage_CONNECTED, waE2E.CallLogMessage_ACCEPTED_ELSEWHERE:
		ev.Outcome = database.CallAnswered
	case waE2E.CallLogMessage_MISSED, waE2E.CallLogMessage_SILENCED_BY_DND, waE2E.CallLogMessage_SILENCED_UNKNOWN_CALLER:
		ev.Outcome = database.CallMissed
	case waE2E.CallLogMessage_REJECTED:
		ev.Outcome = database.CallRejected
	default:
		ev.Outcome = database.CallUnknown
	}
	return ev
}

// isOwnJID reports whether jid is the linked account, as phone JID or LID.
func (c *Client) isOwnJID(jid types.JID) bool {
	if jid.IsEmpty() || c == nil || c.Client == nil || c.Store == nil || c.Store.ID == nil {
		return false
	}
	if jid.User == c.Store.ID.User && jid.Server == c.Store.ID.Server {
		return true
	}
	return !c.Store.LID.IsEmpty() && jid.User == c.Store.LID.User && jid.Server == c.Store.LID.Server
}

func (c *Client) callIDs() callJIDs {
	return callJIDs{
		isOwn: c.isOwnJID,
		normalize: func(j types.JID) types.JID {
			if c == nil || c.Client == nil || c.Store == nil {
				return j
			}
			return c.NormalizeChatJID(j)
		},
		inGroup: func(group, member types.JID) bool {
			if c == nil || c.Client == nil {
				return false
			}
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			info, err := c.cachedGroupInfo(ctx, group)
			if err != nil {
				c.logger.Warnf("[CALL] could not check members of %s: %v", group, err)
				return false
			}
			return groupHasParticipant(info, member)
		},
	}
}

// groupHasParticipant reports whether jid is a member of the group under any
// of the forms a participant carries.
func groupHasParticipant(info *types.GroupInfo, jid types.JID) bool {
	if info == nil || jid.IsEmpty() {
		return false
	}
	for _, p := range info.Participants {
		for _, form := range []types.JID{p.JID, p.LID, p.PhoneNumber} {
			if form.User == jid.User && form.Server == jid.Server {
				return true
			}
		}
	}
	return false
}

// HandleCallEvent records a whatsmeow call event in the call log and keeps the
// chat's "call-<id>" pseudo-message (which predates the calls table and which
// readers of the message history still rely on) in step with it. It never
// answers or rejects a call. Returns false for events that are not calls.
func (c *Client) HandleCallEvent(store *database.MessageStore, webhookManager any, evt any) bool {
	ev, ok := callEventFrom(evt, c.callIDs())
	if !ok {
		switch evt.(type) {
		case *events.CallOffer, *events.CallOfferNotice, *events.CallAccept, *events.CallReject, *events.CallTerminate:
			return true // a call event without an ID; nothing to key it on
		}
		return false
	}
	call, err := store.RecordCallEvent(ev)
	if errors.Is(err, database.ErrCallUnknown) {
		// Routine for a call this device never saw start; see callEventFrom.
		c.logger.Debugf("[CALL] ignored %s for unrecorded call %s from %s", ev.Kind, ev.CallID, ev.ChatJID)
		return true
	}
	if err != nil {
		c.logger.Warnf("[CALL] failed to record %s for %s: %v", ev.Kind, ev.CallID, err)
		return true
	}
	c.logger.Infof("[CALL] %s %s (%s, outcome %s)", ev.Kind, ev.CallID, call.Direction, call.Outcome)
	c.storeCallMessage(store, call)
	emitChange(webhookManager, "call", bridgeTypes.WebhookMessageInfo{
		ID:        "call-" + call.CallID,
		ChatJID:   call.ChatJID,
		Sender:    call.CallerJID,
		Content:   callSummary(call, ""),
		Timestamp: ev.At.Format(time.RFC3339),
		IsFromMe:  call.Direction == "outgoing",
		MediaType: "call",
	}, callInfo(call, ev.Kind))
	return true
}

// recordCallLog stores a call-log message or a history "missed call" stub.
func (c *Client) recordCallLog(store *database.MessageStore, ev database.CallEvent) {
	call, err := store.RecordCallEvent(ev)
	if err != nil {
		c.logger.Warnf("[CALL] failed to record call log entry %s: %v", ev.CallID, err)
		return
	}
	c.logger.Debugf("[CALL] call log %s -> %s (%s)", ev.CallID, call.CallID, call.Outcome)
}

// storeCallMessage writes the pseudo-message for a call. Only calls this
// bridge saw live have one; a call-log entry for an unseen call stays in the
// calls table so history does not grow messages nobody sent.
func (c *Client) storeCallMessage(store *database.MessageStore, call database.Call) {
	if call.ChatJID == "" {
		return
	}
	name := call.ChatJID
	if c.Client != nil && c.Store != nil {
		if jid, err := types.ParseJID(call.ChatJID); err == nil {
			name = c.GetChatName(store, jid, call.ChatJID, nil, jid.User)
		}
	}
	if err := store.StoreChat(call.ChatJID, name, call.StartedAt); err != nil {
		c.logger.Warnf("[CALL] failed to store chat for call %s: %v", call.CallID, err)
	}
	sender := call.CallerJID
	if call.Direction == "outgoing" && c.Client != nil && c.Store != nil && c.Store.ID != nil {
		sender = c.Store.ID.ToNonAD().String()
	}
	if err := store.StoreMessage("call-"+call.CallID, call.ChatJID, sender, name, callSummary(call, name), call.StartedAt,
		call.Direction == "outgoing", "call", "", "", "", nil, nil, nil, 0); err != nil {
		c.logger.Warnf("[CALL] failed to store call message %s: %v", call.CallID, err)
	}
}

// callSummary is the pseudo-message and webhook text for a call.
func callSummary(call database.Call, name string) string {
	if name == "" {
		name = call.ChatJID
	}
	kind := "call"
	if call.IsVideo {
		kind = "video call"
	}
	if call.IsGroup {
		kind = "group " + kind
	}
	out := call.Direction == "outgoing"
	switch call.Outcome {
	case database.CallMissed:
		if out {
			return fmt.Sprintf("📞 Unanswered %s to %s", kind, name)
		}
		return fmt.Sprintf("📞 Missed %s from %s", kind, name)
	case database.CallRejected:
		if out {
			return fmt.Sprintf("📞 Declined %s to %s", kind, name)
		}
		return fmt.Sprintf("📞 Declined %s from %s", kind, name)
	case database.CallAnswered:
		if call.DurationSeconds.Valid {
			return fmt.Sprintf("📞 %s with %s (%s)", capitalise(kind), name, formatCallDuration(call.DurationSeconds))
		}
		return fmt.Sprintf("📞 %s with %s (answered)", capitalise(kind), name)
	}
	if out {
		return fmt.Sprintf("📞 Outgoing %s to %s", kind, name)
	}
	return fmt.Sprintf("📞 Incoming %s from %s", kind, name)
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	return string(s[0]-'a'+'A') + s[1:]
}

func formatCallDuration(d sql.NullInt64) string {
	secs := d.Int64
	if secs < 0 {
		secs = 0
	}
	return fmt.Sprintf("%d:%02d", secs/60, secs%60)
}

func callInfo(call database.Call, kind string) *bridgeTypes.WebhookCallInfo {
	info := &bridgeTypes.WebhookCallInfo{
		CallID:    call.CallID,
		Event:     kind,
		Direction: call.Direction,
		IsVideo:   call.IsVideo,
		IsGroup:   call.IsGroup,
		Outcome:   call.Outcome,
	}
	if call.DurationSeconds.Valid {
		d := call.DurationSeconds.Int64
		info.DurationSeconds = &d
	}
	return info
}
