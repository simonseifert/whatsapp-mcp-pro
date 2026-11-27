package whatsapp

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"

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

func TestQuoteUnknownMessageIsSkipped(t *testing.T) {
	store := newQuoteStore(t)
	var c *Client
	ci, err := c.buildQuoteContext(store, "NOPE", testGroup, types.JID{User: testPhone, Server: types.DefaultUserServer})
	if err != nil || ci != nil {
		t.Errorf("ctx=%v err=%v, want nil, nil (send proceeds unquoted as before)", ci, err)
	}
}
