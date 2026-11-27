package whatsapp

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

var (
	starGroup  = types.JID{User: "120363000000000001", Server: types.GroupServer}
	starLID    = types.JID{User: "100000000000001", Server: types.HiddenUserServer}
	starLIDDev = types.JID{User: "100000000000001", Server: types.HiddenUserServer, Device: 7}
)

func TestExtractReplyContextMentions(t *testing.T) {
	msg := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
		Text: proto.String("hi @a @b"),
		ContextInfo: &waE2E.ContextInfo{MentionedJID: []string{
			"4915112345678:12@s.whatsapp.net", // device suffix stripped
			starLIDDev.String(),               // LID stays a LID
			"4915112345678@s.whatsapp.net",    // duplicate after stripping
			"not a jid",                       // dropped
		}},
	}}
	rc := ExtractReplyContext(msg)
	want := []string{"4915112345678@s.whatsapp.net", "100000000000001@lid"}
	if !reflect.DeepEqual(rc.MentionedJIDs, want) {
		t.Fatalf("mentions = %v, want %v", rc.MentionedJIDs, want)
	}
	if got := ExtractReplyContext(&waE2E.Message{Conversation: proto.String("x")}).MentionedJIDs; got != nil {
		t.Fatalf("no mentions = %v, want nil", got)
	}
}

func TestStoreReplyContextStoresMentions(t *testing.T) {
	store := openTestStore(t)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	if err := store.StoreChat(starGroup.String(), "G", sent); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("M1", starGroup.String(), starLID.String(), "Max", "hi", sent, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	rc := ExtractReplyContext(&waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
		Text:        proto.String("hi"),
		ContextInfo: &waE2E.ContextInfo{MentionedJID: []string{starLIDDev.String()}},
	}})
	c := &Client{logger: waLog.Noop}
	c.storeReplyContext(store, "M1", starGroup.String(), rc)
	got, err := store.GetMentionedJIDs("M1", starGroup.String())
	if err != nil || !reflect.DeepEqual(got, []string{"100000000000001@lid"}) {
		t.Fatalf("stored mentions = %v, %v", got, err)
	}
}

func TestHandleStarEvent(t *testing.T) {
	store := openTestStore(t)
	c := &Client{logger: waLog.Noop}
	at := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	star := func(starred bool) *events.Star {
		return &events.Star{ChatJID: starGroup, SenderJID: starLID, MessageID: "M1", Timestamp: at,
			Action: &waSyncAction.StarAction{Starred: proto.Bool(starred)}}
	}
	c.HandleChatStateEvent(store, star(true))
	if ok, _ := store.IsMessageStarred(starGroup.String(), "M1"); !ok {
		t.Fatal("star event did not store the row")
	}
	c.HandleChatStateEvent(store, star(false))
	if ok, _ := store.IsMessageStarred(starGroup.String(), "M1"); ok {
		t.Fatal("unstar event did not delete the row")
	}
	c.HandleChatStateEvent(store, &events.Star{ChatJID: starGroup, MessageID: "M1"}) // nil Action: ignored
}

func TestApplyForwarded(t *testing.T) {
	kinds := map[string]*waE2E.Message{
		"text":     {Conversation: proto.String("hi")},
		"extended": {ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("hi")}},
		"image":    {ImageMessage: &waE2E.ImageMessage{}},
		"video":    {VideoMessage: &waE2E.VideoMessage{}},
		"audio":    {AudioMessage: &waE2E.AudioMessage{}},
		"document": {DocumentMessage: &waE2E.DocumentMessage{}},
	}
	for name, msg := range kinds {
		t.Run(name, func(t *testing.T) {
			applyForwarded(msg)
			ci := messageContextInfo(msg)
			if ci == nil || !ci.GetIsForwarded() || ci.GetForwardingScore() != 1 {
				t.Fatalf("context = %+v", ci)
			}
		})
	}
	// An existing quote and mentions survive.
	msg := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
		Text:        proto.String("hi"),
		ContextInfo: &waE2E.ContextInfo{StanzaID: proto.String("Q"), MentionedJID: []string{"1@s.whatsapp.net"}},
	}}
	applyForwarded(msg)
	ci := messageContextInfo(msg)
	if ci.GetStanzaID() != "Q" || len(ci.GetMentionedJID()) != 1 || !ci.GetIsForwarded() {
		t.Fatalf("context = %+v", ci)
	}
	// Plain text becomes an extended message, since Conversation has no ContextInfo.
	plain := &waE2E.Message{Conversation: proto.String("hi")}
	applyForwarded(plain)
	if plain.Conversation != nil || plain.GetExtendedTextMessage().GetText() != "hi" {
		t.Fatalf("plain text not converted: %+v", plain)
	}
}

// starClient records the app state patch instead of sending it.
func starClient(sendErr error, onSend func()) (*Client, *appstate.PatchInfo) {
	var got appstate.PatchInfo
	c := &Client{logger: waLog.Noop}
	c.appStateSender = func(_ context.Context, p appstate.PatchInfo) error {
		got = p
		if onSend != nil {
			onSend()
		}
		return sendErr
	}
	return c, &got
}

func TestStarMessageLIDSenderInGroup(t *testing.T) {
	store := openTestStore(t)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	_ = store.StoreChat(starGroup.String(), "G", sent)
	if err := store.StoreMessage("M1", starGroup.String(), starLID.String(), "Max", "hi", sent, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	var rowBeforeSend bool
	c, patch := starClient(nil, nil)
	c.appStateSender = func(_ context.Context, p appstate.PatchInfo) error {
		*patch = p
		rowBeforeSend, _ = store.IsMessageStarred(starGroup.String(), "M1")
		return nil
	}
	if err := c.StarMessage(store, starGroup.String(), "M1", "", false, true); err != nil {
		t.Fatal(err)
	}
	if !rowBeforeSend {
		t.Error("row was not written before the app state patch went out")
	}
	idx := patch.Mutations[0].Index
	want := []string{appstate.IndexStar, starGroup.String(), "M1", "0", starLID.String()}
	if !reflect.DeepEqual(idx, want) {
		t.Fatalf("index = %v, want %v", idx, want)
	}
	if ok, _ := store.IsMessageStarred(starGroup.String(), "M1"); !ok {
		t.Fatal("row missing after star")
	}

	// Unstar deletes the row.
	if err := c.StarMessage(store, starGroup.String(), "M1", "", false, false); err != nil {
		t.Fatal(err)
	}
	if ok, _ := store.IsMessageStarred(starGroup.String(), "M1"); ok {
		t.Fatal("row still there after unstar")
	}
}

func TestStarMessageFromMeAndDirectUseZeroSender(t *testing.T) {
	store := openTestStore(t)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	for _, chat := range []types.JID{starGroup, phoneJID} {
		_ = store.StoreChat(chat.String(), "C", sent)
	}
	_ = store.StoreMessage("OWN", starGroup.String(), "", "Me", "hi", sent, true, "", "", "", "", nil, nil, nil, 0)
	_ = store.StoreMessage("THEIRS", phoneJID.String(), "4915112345678", "Max", "hi", sent, false, "", "", "", "", nil, nil, nil, 0)

	c, patch := starClient(nil, nil)
	if err := c.StarMessage(store, starGroup.String(), "OWN", "", true, true); err != nil {
		t.Fatal(err)
	}
	if got := patch.Mutations[0].Index; got[3] != "1" || got[4] != "0" {
		t.Errorf("own group message index = %v", got)
	}
	if err := c.StarMessage(store, phoneJID.String(), "THEIRS", "", false, true); err != nil {
		t.Fatal(err)
	}
	if got := patch.Mutations[0].Index; got[3] != "0" || got[4] != "0" {
		t.Errorf("direct message index = %v", got)
	}
}

func TestStarMessageUsesRequestSenderWhenUnstored(t *testing.T) {
	store := openTestStore(t)
	c, patch := starClient(nil, nil)
	if err := c.StarMessage(store, starGroup.String(), "GONE", starLIDDev.String(), false, true); err != nil {
		t.Fatal(err)
	}
	if got := patch.Mutations[0].Index; got[4] != starLID.String() {
		t.Errorf("sender = %q, want the device-free LID", got[4])
	}
	// No stored row and no sender in a group: refuse rather than guess.
	if err := c.StarMessage(store, starGroup.String(), "GONE2", "", false, true); err == nil {
		t.Fatal("want an error when the group sender is unknown")
	}
}

func TestStarMessageSendFailureRollsBack(t *testing.T) {
	store := openTestStore(t)
	c, _ := starClient(errors.New("boom"), nil)
	if err := c.StarMessage(store, phoneJID.String(), "M1", "", false, true); err == nil {
		t.Fatal("want the send error")
	}
	if ok, _ := store.IsMessageStarred(phoneJID.String(), "M1"); ok {
		t.Fatal("row kept after failed send")
	}
}

func TestMarkChatUnread(t *testing.T) {
	store := openTestStore(t)
	t1 := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	_ = store.StoreChat(phoneJID.String(), "Max", t1)
	_ = store.StoreMessage("OLD", phoneJID.String(), "4915112345678", "Max", "a", t1, false, "", "", "", "", nil, nil, nil, 0)
	_ = store.StoreMessage("NEW", phoneJID.String(), "4915112345678", "Max", "b", t1.Add(time.Hour), false, "", "", "", "", nil, nil, nil, 0)

	c, patch := starClient(nil, nil)
	if err := c.MarkChatUnread(store, phoneJID.String(), true); err != nil {
		t.Fatal(err)
	}
	m := patch.Mutations[0]
	if m.Index[0] != appstate.IndexMarkChatAsRead || m.Index[1] != phoneJID.String() {
		t.Fatalf("index = %v", m.Index)
	}
	act := m.Value.GetMarkChatAsReadAction()
	if act.GetRead() {
		t.Error("unread=true must send read=false")
	}
	if keys := act.GetMessageRange().GetMessages(); len(keys) != 1 || keys[0].GetKey().GetID() != "NEW" {
		t.Errorf("range = %+v", act.GetMessageRange())
	}
	if st, _ := store.GetChatState(phoneJID.String()); !st.MarkedUnread {
		t.Error("local state not marked unread")
	}

	if err := c.MarkChatUnread(store, phoneJID.String(), false); err != nil {
		t.Fatal(err)
	}
	if !patch.Mutations[0].Value.GetMarkChatAsReadAction().GetRead() {
		t.Error("unread=false must send read=true")
	}
	if st, _ := store.GetChatState(phoneJID.String()); st.MarkedUnread {
		t.Error("local state still marked unread")
	}
}
