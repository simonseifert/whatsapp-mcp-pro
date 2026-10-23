package whatsapp

import (
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	"whatsapp-bridge/internal/database"
	bridgeTypes "whatsapp-bridge/internal/types"
)

var (
	phoneJID = types.JID{User: "4915112345678", Server: types.DefaultUserServer}
	lidJID   = types.JID{User: "123456789012345", Server: types.HiddenUserServer}
	ownJID   = types.JID{User: "4915100000001", Server: types.DefaultUserServer}
	groupJID = types.JID{User: "120363000000000001", Server: types.GroupServer}
)

func editMsg(target, text string, ms int64) *waE2E.Message {
	return &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type:          waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(),
		Key:           &waCommon.MessageKey{ID: proto.String(target)},
		EditedMessage: &waE2E.Message{Conversation: proto.String(text)},
		TimestampMS:   proto.Int64(ms),
	}}
}

func revokeMsg(target string) *waE2E.Message {
	return &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type: waE2E.ProtocolMessage_REVOKE.Enum(),
		Key:  &waCommon.MessageKey{ID: proto.String(target)},
	}}
}

func TestClassifyChange(t *testing.T) {
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	// An edit's claimed time is normally a little before the server stamps it.
	editAt := at.Add(-30 * time.Second)
	info := types.MessageInfo{ID: "STANZA1", Timestamp: at}
	tests := []struct {
		name string
		msg  *events.Message
		want messageChange
	}{
		{"plain text", &events.Message{Info: info, Message: &waE2E.Message{Conversation: proto.String("hi")}}, messageChange{}},
		{"nil message", &events.Message{Info: info}, messageChange{}},
		{"edit", &events.Message{Info: info, Message: editMsg("ORIG", "fixed", editAt.UnixMilli())},
			messageChange{Kind: changeEdit, TargetID: "ORIG", NewContent: "fixed", At: editAt, StanzaID: "STANZA1"}},
		{"edit without timestamp uses message time", &events.Message{Info: info, Message: editMsg("ORIG", "fixed", 0)},
			messageChange{Kind: changeEdit, TargetID: "ORIG", NewContent: "fixed", At: at, StanzaID: "STANZA1"}},
		{"edit claiming a future time is clamped to the server time", &events.Message{Info: info, Message: editMsg("ORIG", "fixed", at.Add(48*time.Hour).UnixMilli())},
			messageChange{Kind: changeEdit, TargetID: "ORIG", NewContent: "fixed", At: at, StanzaID: "STANZA1"}},
		{"edit without target", &events.Message{Info: info, Message: editMsg("", "x", 1)}, messageChange{}},
		{"revoke", &events.Message{Info: info, Message: revokeMsg("ORIG")},
			messageChange{Kind: changeRevoke, TargetID: "ORIG", At: at, StanzaID: "STANZA1"}},
		{"other protocol message", &events.Message{Info: info, Message: &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
			Type: waE2E.ProtocolMessage_EPHEMERAL_SETTING.Enum()}}}, messageChange{}},
		{"newsletter edit", &events.Message{Info: types.MessageInfo{ID: "77", Timestamp: at},
			Message:        &waE2E.Message{Conversation: proto.String("new post")},
			NewsletterMeta: &events.NewsletterMessageMeta{EditTS: editAt}},
			messageChange{Kind: changeEdit, TargetID: "77", NewContent: "new post", At: editAt, StanzaID: "77@" + editAt.Format(time.RFC3339Nano)}},
	}
	before := time.Now()
	future := before.Add(time.Hour)
	got := classifyChange(&events.Message{Info: types.MessageInfo{ID: "78", Timestamp: at},
		Message: &waE2E.Message{Conversation: proto.String("x")}, NewsletterMeta: &events.NewsletterMessageMeta{EditTS: future}})
	if got.At.Before(before) || got.At.After(time.Now()) {
		t.Errorf("newsletter edit from the future: at %v, want clamped to now", got.At)
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := classifyChange(tc.msg)
			if got.Kind != tc.want.Kind || got.TargetID != tc.want.TargetID || got.NewContent != tc.want.NewContent ||
				!got.At.Equal(tc.want.At) || got.StanzaID != tc.want.StanzaID {
				t.Errorf("got %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestClassifyChangeUnwrapsEditedEnvelope(t *testing.T) {
	// How a current client sends an edit: the protocol message inside an
	// EditedMessage future-proof envelope.
	raw := &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: editMsg("ORIG", "fixed", 1_800_000_000_000)}}
	evt := (&events.Message{Info: types.MessageInfo{ID: "S"}, RawMessage: raw}).UnwrapRaw()
	if got := classifyChange(evt); got.Kind != changeEdit || got.TargetID != "ORIG" || got.NewContent != "fixed" {
		t.Errorf("got %+v", got)
	}
}

func TestHistoryChangeAndMissedCall(t *testing.T) {
	stub := func(st waWeb.WebMessageInfo_StubType) *waWeb.WebMessageInfo {
		return &waWeb.WebMessageInfo{
			Key:                    &waCommon.MessageKey{ID: proto.String("H1")},
			MessageTimestamp:       proto.Uint64(1_800_000_000),
			MessageStubType:        st.Enum(),
			RevokeMessageTimestamp: proto.Uint64(1_800_000_100),
		}
	}
	tests := []struct {
		name                 string
		wmi                  *waWeb.WebMessageInfo
		wantKind             string
		wantAt               int64
		missed, video, group bool
	}{
		{"revoke stub", stub(waWeb.WebMessageInfo_REVOKE), changeRevoke, 1_800_000_100, false, false, false},
		{"admin revoke stub", stub(waWeb.WebMessageInfo_ADMIN_REVOKE), changeRevoke, 1_800_000_100, false, false, false},
		{"missed voice", stub(waWeb.WebMessageInfo_CALL_MISSED_VOICE), changeNone, 0, true, false, false},
		{"missed video", stub(waWeb.WebMessageInfo_CALL_MISSED_VIDEO), changeNone, 0, true, true, false},
		{"missed group voice", stub(waWeb.WebMessageInfo_CALL_MISSED_GROUP_VOICE), changeNone, 0, true, false, true},
		{"missed group video", stub(waWeb.WebMessageInfo_CALL_MISSED_GROUP_VIDEO), changeNone, 0, true, true, true},
		{"edit in history", &waWeb.WebMessageInfo{
			Key:              &waCommon.MessageKey{ID: proto.String("H2")},
			MessageTimestamp: proto.Uint64(1_800_000_000),
			Message:          &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: editMsg("ORIG", "v2", 1_799_999_950_000)}},
		}, changeEdit, 1_799_999_950, false, false, false},
		{"ordinary message", &waWeb.WebMessageInfo{
			Key:     &waCommon.MessageKey{ID: proto.String("H3")},
			Message: &waE2E.Message{Conversation: proto.String("hi")},
		}, changeNone, 0, false, false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ch := historyChange(tc.wmi, unwrapHistory(tc.wmi))
			if ch.Kind != tc.wantKind {
				t.Fatalf("kind = %q, want %q", ch.Kind, tc.wantKind)
			}
			if tc.wantKind != changeNone && ch.At.Unix() != tc.wantAt {
				t.Errorf("at = %d, want %d", ch.At.Unix(), tc.wantAt)
			}
			video, group, missed := historyMissedCall(tc.wmi)
			if missed != tc.missed || video != tc.video || group != tc.group {
				t.Errorf("missed=%v video=%v group=%v", missed, video, group)
			}
		})
	}
}

func TestCallEventFrom(t *testing.T) {
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	ids := callJIDs{
		isOwn: func(j types.JID) bool { return j.User == ownJID.User },
		normalize: func(j types.JID) types.JID {
			if j == lidJID {
				return phoneJID
			}
			return j
		},
	}
	ownDevice := types.JID{User: ownJID.User, Server: types.DefaultUserServer, Device: 3}
	videoOffer := &binary.Node{Tag: "offer", Content: []binary.Node{{Tag: "audio"}, {Tag: "video"}}}
	meta := func(from, creator, alt, group types.JID) types.BasicCallMeta {
		return types.BasicCallMeta{From: from, Timestamp: at, CallCreator: creator, CallCreatorAlt: alt, CallID: "CALL1", GroupJID: group}
	}
	tests := []struct {
		name                  string
		evt                   any
		ok                    bool
		kind, dir, chat, from string
		video, group, byMe    bool
	}{
		{"incoming video offer from LID with phone alt",
			&events.CallOffer{BasicCallMeta: meta(lidJID, lidJID, phoneJID, types.EmptyJID), Data: videoOffer},
			true, database.CallEventOffer, "incoming", phoneJID.String(), phoneJID.String(), true, false, false},
		{"incoming audio offer, LID normalised",
			&events.CallOffer{BasicCallMeta: meta(lidJID, lidJID, types.EmptyJID, types.EmptyJID), Data: &binary.Node{Tag: "offer"}},
			true, database.CallEventOffer, "incoming", phoneJID.String(), phoneJID.String(), false, false, false},
		{"group offer notice",
			&events.CallOfferNotice{BasicCallMeta: meta(phoneJID, phoneJID, types.EmptyJID, groupJID), Media: "video", Type: "group"},
			true, database.CallEventOffer, "incoming", groupJID.String(), phoneJID.String(), true, true, false},
		{"accepted on own phone",
			&events.CallAccept{BasicCallMeta: meta(ownDevice, phoneJID, types.EmptyJID, types.EmptyJID)},
			true, database.CallEventAccept, "incoming", phoneJID.String(), phoneJID.String(), false, false, true},
		{"outgoing call accepted by the other side",
			&events.CallAccept{BasicCallMeta: meta(phoneJID, ownJID, types.EmptyJID, types.EmptyJID)},
			true, database.CallEventAccept, "outgoing", phoneJID.String(), "", false, false, false},
		{"reject",
			&events.CallReject{BasicCallMeta: meta(ownDevice, phoneJID, types.EmptyJID, types.EmptyJID)},
			true, database.CallEventReject, "incoming", phoneJID.String(), phoneJID.String(), false, false, true},
		{"terminate", &events.CallTerminate{BasicCallMeta: meta(phoneJID, phoneJID, types.EmptyJID, types.EmptyJID), Reason: "timeout"},
			true, database.CallEventTerminate, "incoming", phoneJID.String(), phoneJID.String(), false, false, false},
		{"no call ID", &events.CallTerminate{}, false, "", "", "", "", false, false, false},
		{"not a call event", &events.CallRelayLatency{}, false, "", "", "", "", false, false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ev, ok := callEventFrom(tc.evt, ids)
			if ok != tc.ok {
				t.Fatalf("ok = %v", ok)
			}
			if !ok {
				return
			}
			if ev.Kind != tc.kind || ev.Direction != tc.dir || ev.ChatJID != tc.chat || ev.CallerJID != tc.from ||
				ev.IsVideo != tc.video || ev.IsGroup != tc.group || ev.ByMe != tc.byMe || !ev.At.Equal(at) || ev.CallID != "CALL1" {
				t.Errorf("got %+v", ev)
			}
		})
	}
}

func TestCallLogEvent(t *testing.T) {
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	chat := phoneJID.String()
	tests := []struct {
		outcome waE2E.CallLogMessage_CallOutcome
		want    string
	}{
		{waE2E.CallLogMessage_CONNECTED, database.CallAnswered},
		{waE2E.CallLogMessage_ACCEPTED_ELSEWHERE, database.CallAnswered},
		{waE2E.CallLogMessage_MISSED, database.CallMissed},
		{waE2E.CallLogMessage_SILENCED_BY_DND, database.CallMissed},
		{waE2E.CallLogMessage_SILENCED_UNKNOWN_CALLER, database.CallMissed},
		{waE2E.CallLogMessage_REJECTED, database.CallRejected},
		{waE2E.CallLogMessage_FAILED, database.CallUnknown},
	}
	for _, tc := range tests {
		t.Run(tc.outcome.String(), func(t *testing.T) {
			log := &waE2E.CallLogMessage{CallOutcome: tc.outcome.Enum(), DurationSecs: proto.Int64(75), IsVideo: proto.Bool(true)}
			ev := callLogEvent(log, "LOG1", chat, at, database.SourceLive)
			// The ID is namespaced so a sender-chosen message ID can never
			// address a live call; direction is left to the live events.
			if ev.Outcome != tc.want || ev.Duration != 75*time.Second || !ev.IsVideo || ev.Kind != database.CallEventLog ||
				ev.CallID != "log:LOG1" || ev.Direction != "" || ev.CallerJID != "" || ev.ChatJID != chat {
				t.Errorf("got %+v", ev)
			}
		})
	}
}

func TestCallSummary(t *testing.T) {
	tests := []struct {
		call database.Call
		want string
	}{
		{database.Call{Direction: "incoming", Outcome: database.CallMissed}, "📞 Missed call from Max"},
		{database.Call{Direction: "outgoing", Outcome: database.CallMissed, IsVideo: true}, "📞 Unanswered video call to Max"},
		{database.Call{Direction: "incoming", Outcome: database.CallAnswered}, "📞 Call with Max (answered)"},
		{database.Call{Direction: "outgoing", Outcome: database.CallAnswered, DurationSeconds: sql.NullInt64{Int64: 83, Valid: true}}, "📞 Call with Max (1:23)"},
		{database.Call{Direction: "incoming", Outcome: database.CallRejected, IsGroup: true}, "📞 Declined group call from Max"},
		{database.Call{Direction: "incoming", Outcome: database.CallUnknown}, "📞 Incoming call from Max"},
	}
	for _, tc := range tests {
		if got := callSummary(tc.call, "Max"); got != tc.want {
			t.Errorf("callSummary(%+v) = %q, want %q", tc.call, got, tc.want)
		}
	}
}

type fakeSink struct {
	events []string
	infos  []bridgeTypes.WebhookMessageInfo
	calls  []*bridgeTypes.WebhookCallInfo
}

func (f *fakeSink) DeliverEvent(eventType string, info bridgeTypes.WebhookMessageInfo, call *bridgeTypes.WebhookCallInfo) {
	f.events = append(f.events, eventType)
	f.infos = append(f.infos, info)
	f.calls = append(f.calls, call)
}

func openTestStore(t *testing.T) *database.MessageStore {
	t.Helper()
	name := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	store, err := database.OpenMessageStore("file:" + name + "?mode=memory&cache=shared&_foreign_keys=on")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// TestHandleMessageEditAndRevoke drives HandleMessage the way the event loop
// does: before edits were handled there, both fell through to the text
// extraction, found nothing and were silently dropped.
func TestHandleMessageEditAndRevoke(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	sink := &fakeSink{}
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	info := func(id string, ts time.Time) types.MessageInfo {
		return types.MessageInfo{
			MessageSource: types.MessageSource{Chat: phoneJID, Sender: phoneJID},
			ID:            id, Timestamp: ts, PushName: "Max",
		}
	}

	// The original goes in directly: storing a new message resolves the chat
	// name through a connected client, which this test has none of. Edits and
	// revokes return before that point.
	if err := store.StoreChat(phoneJID.String(), "Max", sent); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("ORIG", phoneJID.String(), phoneJID.String(), "Max", "helo", sent, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	edit := &events.Message{Info: info("EDIT1", sent.Add(time.Minute)), Message: editMsg("ORIG", "hello", sent.Add(time.Minute).UnixMilli())}
	c.HandleMessage(store, sink, edit)
	c.HandleMessage(store, sink, edit) // redelivery must not fire a second webhook
	c.HandleMessage(store, sink, &events.Message{Info: info("REV1", sent.Add(2*time.Minute)), Message: revokeMsg("ORIG")})

	msgs, err := store.GetMessages(phoneJID.String(), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 1 {
		t.Fatalf("stored %d messages, want only the original: %+v", len(msgs), msgs)
	}
	edits, err := store.GetMessageEdits("ORIG", phoneJID.String())
	if err != nil || len(edits) != 1 || edits[0].NewContent != "hello" || edits[0].OldContent.String != "helo" {
		t.Fatalf("edits = %+v, %v", edits, err)
	}
	if strings.Join(sink.events, ",") != "message_edited,message_deleted" {
		t.Fatalf("webhook events = %v", sink.events)
	}
	if sink.infos[0].Content != "hello" || sink.infos[0].PreviousContent != "helo" || sink.infos[1].ID != "ORIG" {
		t.Errorf("webhook infos = %+v", sink.infos)
	}
}

func TestHandleCallEventStoresCallAndMessage(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	sink := &fakeSink{}
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	meta := types.BasicCallMeta{From: phoneJID, Timestamp: at, CallCreator: phoneJID, CallID: "CALL9"}
	c.HandleCallEvent(store, sink, &events.CallOffer{BasicCallMeta: meta, Data: &binary.Node{Tag: "offer"}})
	meta.Timestamp = at.Add(40 * time.Second)
	c.HandleCallEvent(store, sink, &events.CallTerminate{BasicCallMeta: meta, Reason: "timeout"})
	if c.HandleCallEvent(store, sink, &events.Message{}) {
		t.Error("a message is not a call event")
	}

	call, err := store.GetCall("CALL9")
	if err != nil {
		t.Fatal(err)
	}
	if call.Outcome != database.CallMissed || call.ChatJID != phoneJID.String() || !call.StartedAt.Equal(at) {
		t.Errorf("call = %+v", call)
	}
	msgs, err := store.GetMessages(phoneJID.String(), 10)
	if err != nil || len(msgs) != 1 || !strings.Contains(msgs[0].Content, "Missed call") {
		t.Errorf("call message = %+v, %v", msgs, err)
	}
	if len(sink.calls) != 2 || sink.calls[1].Outcome != database.CallMissed || sink.events[1] != "call" {
		t.Errorf("webhooks = %v %+v", sink.events, sink.calls)
	}
}

func TestParticipantIsAdmin(t *testing.T) {
	member := types.JID{User: "4915187654321", Server: types.DefaultUserServer}
	adminLID := types.JID{User: "987654321098765", Server: types.HiddenUserServer}
	info := &types.GroupInfo{Participants: []types.GroupParticipant{
		{JID: adminLID, PhoneNumber: phoneJID, LID: adminLID, IsAdmin: true},
		{JID: member, PhoneNumber: member},
		{JID: ownJID, PhoneNumber: ownJID, IsSuperAdmin: true},
	}}
	tests := []struct {
		name string
		info *types.GroupInfo
		jids []types.JID
		want bool
	}{
		{"admin by LID", info, []types.JID{adminLID}, true},
		{"admin by phone number", info, []types.JID{phoneJID}, true},
		{"super admin", info, []types.JID{ownJID}, true},
		{"plain member", info, []types.JID{member}, false},
		{"not a participant", info, []types.JID{lidJID}, false},
		{"empty JIDs never match", info, []types.JID{types.EmptyJID, {Server: types.DefaultUserServer}}, false},
		{"one of several forms", info, []types.JID{member, phoneJID}, true},
		{"no group info", nil, []types.JID{phoneJID}, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := participantIsAdmin(tc.info, tc.jids...); got != tc.want {
				t.Errorf("got %v, want %v", got, tc.want)
			}
		})
	}
}

// A group member can send a MESSAGE_EDIT or REVOKE naming anyone's message.
// Neither may change the store or reach a webhook.
func TestHandleMessageRefusesForgedChanges(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	sink := &fakeSink{}
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	stranger := types.JID{User: "4915187654321", Server: types.DefaultUserServer}
	if err := store.StoreChat(groupJID.String(), "Group", sent); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("ORIG", groupJID.String(), phoneJID.String(), "Max", "original", sent, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	from := func(id string, sender types.JID, fromMe bool, ts time.Time) types.MessageInfo {
		return types.MessageInfo{MessageSource: types.MessageSource{Chat: groupJID, Sender: sender, IsFromMe: fromMe, IsGroup: true},
			ID: id, Timestamp: ts}
	}
	at := sent.Add(time.Minute)
	c.HandleMessage(store, sink, &events.Message{Info: from("F1", stranger, false, at), Message: editMsg("ORIG", "forged", at.UnixMilli())})
	c.HandleMessage(store, sink, &events.Message{Info: from("F2", ownJID, true, at), Message: editMsg("ORIG", "mine now", at.UnixMilli())})
	// No connected client, so the admin check is unavailable: refused.
	c.HandleMessage(store, sink, &events.Message{Info: from("F3", stranger, false, at), Message: revokeMsg("ORIG")})

	msgs, err := store.GetMessages(groupJID.String(), 10)
	if err != nil || len(msgs) != 1 || msgs[0].Content != "original" {
		t.Fatalf("messages = %+v, %v", msgs, err)
	}
	if len(sink.events) != 0 {
		t.Errorf("webhooks fired for forged changes: %v", sink.events)
	}
	edits, err := store.GetMessageEdits("ORIG", groupJID.String())
	if err != nil || len(edits) != 2 || edits[0].Status != database.EditRejected || edits[1].Status != database.EditRejected {
		t.Errorf("edits = %+v, %v; want both recorded as rejected", edits, err)
	}

	// The author's own edit, with a sender clock far in the future, still
	// applies, but at the server's time: a later genuine edit must win.
	c.HandleMessage(store, sink, &events.Message{Info: from("G1", phoneJID, false, at), Message: editMsg("ORIG", "v1", at.Add(24*time.Hour).UnixMilli())})
	c.HandleMessage(store, sink, &events.Message{Info: from("G2", phoneJID, false, at.Add(time.Minute)), Message: editMsg("ORIG", "v2", at.Add(time.Minute).UnixMilli())})
	if msgs, _ = store.GetMessages(groupJID.String(), 10); len(msgs) != 1 || msgs[0].Content != "v2" {
		t.Errorf("after genuine edits: %+v", msgs)
	}
	if strings.Join(sink.events, ",") != "message_edited,message_edited" {
		t.Errorf("webhooks = %v", sink.events)
	}
}

// Only the user's own phone writes call-log messages; a forged one must not
// plant or rewrite a call.
func TestHandleMessageCallLogOnlyFromOwnDevice(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	logMsg := func() *waE2E.Message {
		return &waE2E.Message{CallLogMesssage: &waE2E.CallLogMessage{CallOutcome: waE2E.CallLogMessage_CONNECTED.Enum(), DurationSecs: proto.Int64(60)}}
	}
	info := func(id string, fromMe bool) types.MessageInfo {
		sender := phoneJID
		if fromMe {
			sender = ownJID
		}
		return types.MessageInfo{MessageSource: types.MessageSource{Chat: phoneJID, Sender: sender, IsFromMe: fromMe}, ID: id, Timestamp: at}
	}
	c.HandleMessage(store, nil, &events.Message{Info: info("FORGED", false), Message: logMsg()})
	c.HandleMessage(store, nil, &events.Message{Info: info("REAL", true), Message: logMsg()})

	if _, err := store.GetCall("log:FORGED"); !errors.Is(err, sql.ErrNoRows) {
		t.Errorf("forged call log was recorded (err %v)", err)
	}
	call, err := store.GetCall("log:REAL")
	if err != nil || call.Outcome != database.CallAnswered || call.ChatJID != phoneJID.String() {
		t.Errorf("own call log = %+v, %v", call, err)
	}
	if msgs, err := store.GetMessages(phoneJID.String(), 10); err != nil || len(msgs) != 0 {
		t.Errorf("call-log messages must not be stored as chat messages: %+v, %v", msgs, err)
	}

	// Same rule for history sync.
	wmi := func(id string, fromMe bool) *waWeb.WebMessageInfo {
		return &waWeb.WebMessageInfo{
			Key: &waCommon.MessageKey{ID: proto.String(id), FromMe: proto.Bool(fromMe), RemoteJID: proto.String(phoneJID.String())},
			// An hour on, so they don't fold into the live entries above.
			MessageTimestamp: proto.Uint64(uint64(at.Add(time.Hour).Unix())),
			Message:          logMsg(),
		}
	}
	if !c.handleHistoryNonMessage(store, phoneJID.String(), phoneJID, wmi("HFORGED", false)) {
		t.Error("a forged history call log must still be consumed, not stored as a message")
	}
	if _, err := store.GetCall("log:HFORGED"); !errors.Is(err, sql.ErrNoRows) {
		t.Errorf("forged history call log was recorded (err %v)", err)
	}
	c.handleHistoryNonMessage(store, phoneJID.String(), phoneJID, wmi("HREAL", true))
	if _, err := store.GetCall("log:HREAL"); err != nil {
		t.Errorf("own history call log missing: %v", err)
	}
}

// A history-sync edit is still only what someone sent; a revoke stub is the
// phone's record of a revoke the server accepted.
func TestHistoryChangesAuthorship(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	stranger := "4915187654321@s.whatsapp.net"
	if err := store.StoreChat(groupJID.String(), "Group", sent); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"A", "B"} {
		if err := store.StoreMessage(id, groupJID.String(), phoneJID.String(), "Max", "original "+id, sent, false, "", "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatal(err)
		}
	}
	forgedEdit := &waWeb.WebMessageInfo{
		Key:              &waCommon.MessageKey{ID: proto.String("HE1"), Participant: proto.String(stranger), RemoteJID: proto.String(groupJID.String())},
		MessageTimestamp: proto.Uint64(uint64(sent.Add(time.Minute).Unix())),
		Message:          &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: editMsg("A", "forged", sent.Add(time.Minute).UnixMilli())}},
	}
	adminStub := &waWeb.WebMessageInfo{
		Key:                    &waCommon.MessageKey{ID: proto.String("B"), Participant: proto.String(phoneJID.String()), RemoteJID: proto.String(groupJID.String())},
		MessageTimestamp:       proto.Uint64(uint64(sent.Unix())),
		MessageStubType:        waWeb.WebMessageInfo_ADMIN_REVOKE.Enum(),
		RevokeMessageTimestamp: proto.Uint64(uint64(sent.Add(time.Minute).Unix())),
	}
	// A revoke stub of a message the store never saw leaves a stand-in row;
	// its key names the author, who becomes the row's sender.
	unseenStub := &waWeb.WebMessageInfo{
		Key:              &waCommon.MessageKey{ID: proto.String("C"), Participant: proto.String(phoneJID.String()), RemoteJID: proto.String(groupJID.String())},
		MessageTimestamp: proto.Uint64(uint64(sent.Unix())),
		MessageStubType:  waWeb.WebMessageInfo_REVOKE.Enum(),
	}
	c.handleHistoryNonMessage(store, groupJID.String(), groupJID, forgedEdit)
	c.handleHistoryNonMessage(store, groupJID.String(), groupJID, adminStub)
	c.handleHistoryNonMessage(store, groupJID.String(), groupJID, unseenStub)

	msgs, err := store.GetMessages(groupJID.String(), 10)
	if err != nil || len(msgs) != 3 {
		t.Fatalf("messages = %+v, %v", msgs, err)
	}
	for _, m := range msgs {
		if m.ID == "A" && m.Content != "original A" {
			t.Errorf("forged history edit applied: %q", m.Content)
		}
		if m.ID == "C" && (m.Sender != phoneJID.String() || m.IsFromMe) {
			t.Errorf("revoke stand-in = %+v, want sender %s", m, phoneJID)
		}
	}
	res, err := store.MarkDeleted(database.Revoke{MessageID: "B", ChatJIDs: []string{groupJID.String()}, At: sent, Trusted: true})
	if err != nil || res.Newly {
		t.Errorf("admin-revoke stub did not mark B deleted (%+v, %v)", res, err)
	}
}

// A newsletter delivers an edited post as the whole post under its original
// ID. When the original was missed, that is the only copy, so it must be
// stored as a post rather than wait as a pending edit of nothing.
func TestNewsletterEditOfUnstoredPostIsAPost(t *testing.T) {
	store := openTestStore(t)
	newsletter := types.JID{User: "120363000000000002", Server: types.NewsletterServer}
	at := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	post := func(id string) *events.Message {
		return &events.Message{
			Info:           types.MessageInfo{MessageSource: types.MessageSource{Chat: newsletter, Sender: newsletter}, ID: types.MessageID(id), Timestamp: at},
			Message:        &waE2E.Message{Conversation: proto.String("edited post")},
			NewsletterMeta: &events.NewsletterMessageMeta{EditTS: at.Add(time.Minute)},
		}
	}
	chats := []string{newsletter.String()}
	if !newsletterEditOfUnstored(store, post("77"), classifyChange(post("77")), chats) {
		t.Error("edit of an unstored post is not stored as a post")
	}
	if err := store.StoreChat(newsletter.String(), "News", at); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("78", newsletter.String(), newsletter.String(), "News", "post", at, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	if newsletterEditOfUnstored(store, post("78"), classifyChange(post("78")), chats) {
		t.Error("edit of a stored post is not handled as an edit")
	}
	// Only newsletters: elsewhere an edit of an unstored message is pending.
	edit := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: phoneJID, Sender: phoneJID}, ID: "E", Timestamp: at},
		Message: editMsg("NOPE", "x", at.UnixMilli())}
	if newsletterEditOfUnstored(store, edit, classifyChange(edit), []string{phoneJID.String()}) {
		t.Error("a protocol-message edit was treated as a newsletter post")
	}
	c := &Client{logger: waLog.Noop}
	c.HandleMessage(store, nil, post("78"))
	if msgs, err := store.GetMessages(newsletter.String(), 5); err != nil || len(msgs) != 1 || msgs[0].Content != "edited post" {
		t.Errorf("stored post after its edit = %+v, %v", msgs, err)
	}
}

// A live revoke's key names the revoked message's author: the revoker when
// FromMe is set, else the group participant.
func TestRevokedAuthor(t *testing.T) {
	revoke := func(fromMe bool, participant string) *events.Message {
		key := &waCommon.MessageKey{ID: proto.String("ORIG"), FromMe: proto.Bool(fromMe)}
		if participant != "" {
			key.Participant = proto.String(participant)
		}
		return &events.Message{Message: &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
			Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: key}}}
	}
	revoker := database.Actor{JID: lidJID.String()}
	c := &Client{logger: waLog.Noop}
	tests := []struct {
		name string
		msg  *events.Message
		want database.Actor
	}{
		{"revoker's own message", revoke(true, ""), revoker},
		{"admin revoking a participant's", revoke(false, phoneJID.User+":3@"+phoneJID.Server), database.Actor{JID: phoneJID.String()}},
		{"1:1 key without FromMe is unknown", revoke(false, ""), database.Actor{}},
	}
	for _, tc := range tests {
		if got := c.revokedAuthor(tc.msg, revoker); got.JID != tc.want.JID || got.IsMe != tc.want.IsMe {
			t.Errorf("%s: got %+v, want %+v", tc.name, got, tc.want)
		}
	}
}
