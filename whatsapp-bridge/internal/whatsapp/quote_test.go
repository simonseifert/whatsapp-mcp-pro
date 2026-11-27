package whatsapp

import (
	"context"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/database"
)

func newQuoteStore(t *testing.T) *database.MessageStore {
	t.Helper()
	t.Chdir(t.TempDir())
	store, err := database.NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore: %v", err)
	}
	return store
}

func mustChat(t *testing.T, store *database.MessageStore, jids ...string) {
	t.Helper()
	for _, j := range jids {
		if err := store.StoreChat(j, "chat", time.Now()); err != nil {
			t.Fatal(err)
		}
	}
}

func TestQuoteAcrossChatsSetsRemoteJID(t *testing.T) {
	store := newQuoteStore(t)
	sender := testPhone + "@s.whatsapp.net"
	mustChat(t, store, testGroup)
	if err := store.StoreMessage("QID1", testGroup, sender, "Sam", "see you at six", time.Now(), false,
		"", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	dm := types.JID{User: testPhone, Server: types.DefaultUserServer}

	var c *Client
	ci, err := c.buildQuoteContext(store, "QID1", testGroup, dm)
	if err != nil {
		t.Fatalf("buildQuoteContext: %v", err)
	}
	if ci == nil {
		t.Fatal("no context built")
	}
	if ci.GetRemoteJID() != testGroup {
		t.Errorf("RemoteJID = %q, want %q", ci.GetRemoteJID(), testGroup)
	}
	if ci.GetParticipant() != sender {
		t.Errorf("Participant = %q, want %q", ci.GetParticipant(), sender)
	}
	if ci.GetStanzaID() != "QID1" {
		t.Errorf("StanzaID = %q", ci.GetStanzaID())
	}
	if ci.GetQuotedMessage().GetConversation() != "see you at six" {
		t.Errorf("quoted content = %q", ci.GetQuotedMessage().GetConversation())
	}
}

func TestQuoteAcrossChatsLooksUpByChat(t *testing.T) {
	store := newQuoteStore(t)
	other := "120363000000000002@g.us"
	mustChat(t, store, other, testGroup)
	now := time.Now()
	// Same id in two chats (ids are only unique per chat).
	_ = store.StoreMessage("DUP", other, testPhone+"@s.whatsapp.net", "A", "wrong chat", now.Add(time.Minute), false, "", "", "", "", nil, nil, nil, 0)
	_ = store.StoreMessage("DUP", testGroup, testLID+"@lid", "B", "right chat", now, false, "", "", "", "", nil, nil, nil, 0)
	dm := types.JID{User: testPhone, Server: types.DefaultUserServer}

	var c *Client
	ci, err := c.buildQuoteContext(store, "DUP", testGroup, dm)
	if err != nil || ci == nil {
		t.Fatalf("ctx=%v err=%v", ci, err)
	}
	if ci.GetQuotedMessage().GetConversation() != "right chat" {
		t.Errorf("quoted %q, want the message from the named chat", ci.GetQuotedMessage().GetConversation())
	}
	// A LID author in a group is kept as stored.
	if ci.GetParticipant() != testLID+"@lid" {
		t.Errorf("Participant = %q, want stored LID", ci.GetParticipant())
	}
}

func TestQuoteSameChatHasNoRemoteJID(t *testing.T) {
	store := newQuoteStore(t)
	chat := testPhone + "@s.whatsapp.net"
	mustChat(t, store, chat)
	_ = store.StoreMessage("QID2", chat, chat, "Sam", "hello", time.Now(), false, "", "", "", "", nil, nil, nil, 0)
	dm := types.JID{User: testPhone, Server: types.DefaultUserServer}

	var c *Client
	for _, quotedChat := range []string{"", chat} {
		ci, err := c.buildQuoteContext(store, "QID2", quotedChat, dm)
		if err != nil || ci == nil {
			t.Fatalf("quotedChat=%q ctx=%v err=%v", quotedChat, ci, err)
		}
		if ci.RemoteJID != nil {
			t.Errorf("quotedChat=%q: RemoteJID = %q, want unset", quotedChat, ci.GetRemoteJID())
		}
		if ci.GetParticipant() != chat || ci.GetStanzaID() != "QID2" {
			t.Errorf("quotedChat=%q: participant=%q stanza=%q", quotedChat, ci.GetParticipant(), ci.GetStanzaID())
		}
	}
}

func TestQuoteUnknownMessageSameChatIsSkipped(t *testing.T) {
	store := newQuoteStore(t)
	var c *Client
	for _, quotedChat := range []string{"", testPhone + "@s.whatsapp.net"} {
		ci, err := c.buildQuoteContext(store, "NOPE", quotedChat, types.JID{User: testPhone, Server: types.DefaultUserServer})
		if err != nil || ci != nil {
			t.Errorf("quotedChat=%q: ctx=%v err=%v, want nil, nil (sent unquoted as before)", quotedChat, ci, err)
		}
	}
}

func TestQuoteAcrossChatsNotFoundIsAnError(t *testing.T) {
	store := newQuoteStore(t)
	mustChat(t, store, testGroup)
	var c *Client
	ci, err := c.buildQuoteContext(store, "NOPE", testGroup, types.JID{User: testPhone, Server: types.DefaultUserServer})
	if err == nil || ci != nil {
		t.Fatalf("ctx=%v err=%v, want an error so nothing is sent unquoted", ci, err)
	}
	if !strings.Contains(err.Error(), testGroup) {
		t.Errorf("error %q should name the chat", err)
	}
}

func TestQuoteDBErrorIsAnErrorOnBothPaths(t *testing.T) {
	store := newQuoteStore(t)
	_ = store.Close()
	var c *Client
	dm := types.JID{User: testPhone, Server: types.DefaultUserServer}
	for _, quotedChat := range []string{"", testGroup} {
		ci, err := c.buildQuoteContext(store, "QID", quotedChat, dm)
		if err == nil || ci != nil {
			t.Errorf("quotedChat=%q: ctx=%v err=%v, want a DB error", quotedChat, ci, err)
		}
	}
}

// pairedClient knows testLID and testPhone as the same person, both ways.
func pairedClient() *Client {
	lid := types.JID{User: testLID, Server: types.HiddenUserServer}
	phone := types.JID{User: testPhone, Server: types.DefaultUserServer}
	return &Client{altJIDLookup: func(_ context.Context, j types.JID) (types.JID, error) {
		switch j {
		case lid:
			return phone, nil
		case phone:
			return lid, nil
		}
		return types.EmptyJID, nil
	}}
}

func TestQuoteDMChatGivenInEitherForm(t *testing.T) {
	store := newQuoteStore(t)
	phoneChat := testPhone + "@s.whatsapp.net"
	mustChat(t, store, phoneChat)
	_ = store.StoreMessage("DM1", phoneChat, phoneChat, "Sam", "from the dm", time.Now(), false, "", "", "", "", nil, nil, nil, 0)
	other := types.JID{User: "4915100000001", Server: types.DefaultUserServer}
	c := pairedClient()

	for _, form := range []string{testLID + "@lid", phoneChat} {
		ci, err := c.buildQuoteContext(store, "DM1", form, other)
		if err != nil || ci == nil {
			t.Fatalf("form %q: ctx=%v err=%v", form, ci, err)
		}
		if ci.GetRemoteJID() != phoneChat {
			t.Errorf("form %q: RemoteJID = %q, want the stored chat %q", form, ci.GetRemoteJID(), phoneChat)
		}
		if ci.GetQuotedMessage().GetConversation() != "from the dm" {
			t.Errorf("form %q: quoted %q", form, ci.GetQuotedMessage().GetConversation())
		}
	}
}

func TestQuoteChatThatIsRecipientInOtherFormIsSameChat(t *testing.T) {
	store := newQuoteStore(t)
	phoneChat := testPhone + "@s.whatsapp.net"
	mustChat(t, store, phoneChat)
	_ = store.StoreMessage("DM2", phoneChat, phoneChat, "Sam", "hello", time.Now(), false, "", "", "", "", nil, nil, nil, 0)
	c := pairedClient()

	for _, tc := range []struct {
		recipient types.JID
		quoted    string
	}{
		{types.JID{User: testPhone, Server: types.DefaultUserServer}, testLID + "@lid"},
		{types.JID{User: testLID, Server: types.HiddenUserServer}, phoneChat},
	} {
		ci, err := c.buildQuoteContext(store, "DM2", tc.quoted, tc.recipient)
		if err != nil || ci == nil {
			t.Fatalf("%v/%s: ctx=%v err=%v", tc.recipient, tc.quoted, ci, err)
		}
		if ci.RemoteJID != nil {
			t.Errorf("%v/%s: RemoteJID = %q, want unset (same chat)", tc.recipient, tc.quoted, ci.GetRemoteJID())
		}
	}
}

func TestRefusedQuoteNeverUploads(t *testing.T) {
	store := newQuoteStore(t)
	mustChat(t, store, testGroup)
	uploads := 0
	c := &Client{logger: waLog.Noop, uploadHook: func(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
		uploads++
		return whatsmeow.UploadResponse{}, nil
	}}
	res := c.sendWithOptions(context.Background(), store, SendOptions{
		Recipient:       testPhone + "@s.whatsapp.net",
		MediaPath:       "voice-note.ogg",
		QuotedMessageID: "NOPE",
		QuotedChatJID:   testGroup,
	})
	if res.Success || res.Err == nil {
		t.Fatalf("result = %+v, want a refused send", res)
	}
	if !strings.Contains(res.Err.Error(), "cannot quote message NOPE") {
		t.Errorf("error %q should be the quote refusal, not a later step", res.Err)
	}
	if uploads != 0 {
		t.Errorf("uploaded %d times, want 0: a refused quote must leave no trace", uploads)
	}
}
