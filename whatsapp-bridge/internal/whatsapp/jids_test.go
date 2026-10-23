package whatsapp

import (
	"errors"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

const (
	testPhone = "4915112345678"
	testLID   = "123456789012345"
	testGroup = "120363000000000001@g.us"
)

func knownLIDs(users ...string) func(string) bool {
	set := map[string]bool{}
	for _, u := range users {
		set[u] = true
	}
	return func(u string) bool { return set[u] }
}

func TestParticipantJID(t *testing.T) {
	tests := []struct {
		name    string
		sender  string
		chat    string
		known   func(string) bool
		want    string
		wantErr bool
	}{
		{"bare phone in group", testPhone, testGroup, knownLIDs(testLID), testPhone + "@s.whatsapp.net", false},
		{"bare known LID in group", testLID, testGroup, knownLIDs(testLID), testLID + "@lid", false},
		{"bare unknown digits default to phone", testLID, testGroup, knownLIDs(), testLID + "@s.whatsapp.net", false},
		{"bare with nil lookup", testPhone, testGroup, nil, testPhone + "@s.whatsapp.net", false},
		{"full phone JID kept", testPhone + "@s.whatsapp.net", testGroup, knownLIDs(testLID), testPhone + "@s.whatsapp.net", false},
		{"full LID kept as LID", testLID + "@lid", testGroup, knownLIDs(testLID), testLID + "@lid", false},
		{"device suffix stripped", testPhone + ":12@s.whatsapp.net", testGroup, nil, testPhone + "@s.whatsapp.net", false},
		{"bare equals 1:1 LID chat", testLID, testLID + "@lid", knownLIDs(), testLID + "@lid", false},
		{"bare equals newsletter chat", "120363000000000002", "120363000000000002@newsletter", nil, "120363000000000002@newsletter", false},
		{"bare equals group id is not the chat", "120363000000000001", testGroup, nil, "120363000000000001@s.whatsapp.net", false},
		{"empty", "", testGroup, nil, "", true},
		{"not digits", "alice", testGroup, nil, "", true},
		{"server without user", "@s.whatsapp.net", testGroup, nil, "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := participantJID(tt.sender, tt.chat, tt.known)
			if (err != nil) != tt.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tt.wantErr)
			}
			if err == nil && got.String() != tt.want {
				t.Errorf("participantJID(%q) = %q, want %q", tt.sender, got, tt.want)
			}
			if err == nil && (got.User == "" || got.Server == "") {
				t.Errorf("participantJID(%q) returned a JID missing user or server: %#v", tt.sender, got)
			}
		})
	}
}

func TestBareRecipientJID(t *testing.T) {
	tests := []struct {
		name      string
		recipient string
		known     func(string) bool
		want      string
		wantLID   bool
	}{
		{"phone number", testPhone, knownLIDs(testLID), testPhone + "@s.whatsapp.net", false},
		{"known LID refused", testLID, knownLIDs(testLID), "", true},
		{"unknown digits become phone", testLID, knownLIDs(), testLID + "@s.whatsapp.net", false},
		{"nil lookup", testPhone, nil, testPhone + "@s.whatsapp.net", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := bareRecipientJID(tt.recipient, tt.known)
			if tt.wantLID {
				if !errors.Is(err, ErrBareLIDRecipient) {
					t.Fatalf("err = %v, want ErrBareLIDRecipient", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected err: %v", err)
			}
			if got.String() != tt.want {
				t.Errorf("got %q, want %q", got, tt.want)
			}
		})
	}
}

func TestStoredSender(t *testing.T) {
	tests := []struct {
		in   types.JID
		want string
	}{
		{types.JID{User: testPhone, Server: types.DefaultUserServer, Device: 12}, testPhone + "@s.whatsapp.net"},
		{types.JID{User: testLID, Server: types.HiddenUserServer}, testLID + "@lid"},
		{types.EmptyJID, ""},
	}
	for _, tt := range tests {
		if got := StoredSender(tt.in); got != tt.want {
			t.Errorf("StoredSender(%#v) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestOfflineClientResolvers(t *testing.T) {
	// No whatsmeow client: lookups must degrade to the store/defaults, not panic.
	c := &Client{}
	known := c.knownLIDFunc(nil)
	if known(testLID) {
		t.Error("offline client with no store should know no LIDs")
	}
	chat := types.JID{User: testLID, Server: types.HiddenUserServer}
	canonical, alt := c.ChatJIDForms(chat)
	if canonical != chat || !alt.IsEmpty() {
		t.Errorf("ChatJIDForms offline = %v, %v; want %v, empty", canonical, alt, chat)
	}
	group := types.JID{User: "120363000000000001", Server: types.GroupServer}
	if canonical, alt := c.ChatJIDForms(group); canonical != group || !alt.IsEmpty() {
		t.Errorf("ChatJIDForms group = %v, %v", canonical, alt)
	}
	p, err := c.quotedParticipant(nil, testLID+"@lid", types.JID{User: testPhone, Server: types.DefaultUserServer}, false)
	if err != nil || p.String() != testLID+"@lid" {
		t.Errorf("quotedParticipant offline = %v, %v", p, err)
	}
}

func TestKnownLIDFuncUsesDirectory(t *testing.T) {
	store := newReachoutTestStore(t)
	if _, err := store.GetDB().Exec(`INSERT INTO identities (jid, lid) VALUES (?, ?)`, testPhone+"@s.whatsapp.net", testLID+"@lid"); err != nil {
		t.Fatal(err)
	}
	known := (&Client{}).knownLIDFunc(store)
	if !known(testLID) {
		t.Error("LID in identities should be known")
	}
	if known(testPhone) {
		t.Error("phone number must not count as a LID")
	}
	got, err := participantJID(testLID, testGroup, known)
	if err != nil || got.String() != testLID+"@lid" {
		t.Errorf("participantJID via directory = %v, %v", got, err)
	}
}
