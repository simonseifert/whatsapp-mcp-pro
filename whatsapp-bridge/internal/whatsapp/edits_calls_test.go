package whatsapp

import (
	"database/sql"
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
	editAt := at.Add(30 * time.Second)
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
			Message:          &waE2E.Message{EditedMessage: &waE2E.FutureProofMessage{Message: editMsg("ORIG", "v2", 1_800_000_050_000)}},
		}, changeEdit, 1_800_000_050, false, false, false},
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
			ev := callLogEvent(log, "LOG1", chat, at, false, database.SourceLive)
			if ev.Outcome != tc.want || ev.Duration != 75*time.Second || !ev.IsVideo || ev.Kind != database.CallEventLog ||
				ev.Direction != "incoming" || ev.CallerJID != chat || ev.ChatJID != chat {
				t.Errorf("got %+v", ev)
			}
		})
	}
	if ev := callLogEvent(&waE2E.CallLogMessage{}, "LOG2", chat, at, true, database.SourceLive); ev.Direction != "outgoing" || ev.CallerJID != "" {
		t.Errorf("outgoing log entry = %+v", ev)
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
