package whatsapp

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/antiban"
)

const (
	testPhoneJID    = "4915112345678@s.whatsapp.net"
	testLID         = "123456789012345@lid"
	testUnmappedLID = "123456789012399@lid"
)

func clientWithLIDMap(t *testing.T) *Client {
	t.Helper()
	c := newOfflineClient(newReachoutTestStore(t))
	lid := types.NewJID("123456789012345", types.HiddenUserServer)
	phone := types.NewJID("4915112345678", types.DefaultUserServer)
	c.altJIDLookup = func(_ context.Context, jid types.JID) (types.JID, error) {
		if jid == lid {
			return phone, nil
		}
		return types.EmptyJID, nil
	}
	return c
}

func TestCapKeyCanonicalisesLID(t *testing.T) {
	c := clientWithLIDMap(t)
	tests := []struct {
		name string
		chat string
		want string
	}{
		{"phone JID as is", testPhoneJID, testPhoneJID},
		{"device suffix stripped", "4915112345678:3@s.whatsapp.net", testPhoneJID},
		{"mapped LID resolves to the phone JID", testLID, testPhoneJID},
		{"unmapped LID stays as given", testUnmappedLID, testUnmappedLID},
		{"group untouched", "120363000000000001@g.us", "120363000000000001@g.us"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			jid, err := types.ParseJID(tt.chat)
			if err != nil {
				t.Fatal(err)
			}
			if got := c.capKey(jid); got != tt.want {
				t.Fatalf("capKey(%s) = %s, want %s", tt.chat, got, tt.want)
			}
		})
	}
}

// TestPhoneAndLIDShareOneCapBucket: alternating between a person's phone JID
// and their LID must not double the per-chat budget.
func TestPhoneAndLIDShareOneCapBucket(t *testing.T) {
	c := clientWithLIDMap(t)
	cfg := &antiban.Config{
		CapsEnabled:           true,
		RecipientMaxPerMinute: 2,
		FeedbackMaxPerMinute:  2,
		CapsStatePath:         filepath.Join(t.TempDir(), "caps.json"),
	}
	tests := []struct {
		name    string
		msgType antiban.MessageType
	}{
		{"messages", antiban.Text},
		{"reactions", antiban.Reaction},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ic, err := antiban.NewSendInterceptor(cfg)
			if err != nil {
				t.Fatal(err)
			}
			phone, _ := types.ParseJID(testPhoneJID)
			lid, _ := types.ParseJID(testLID)
			sequence := []types.JID{phone, lid, lid}
			for i, chat := range sequence {
				err := ic.BeforeSend(context.Background(), c.antibanRequest(nil, tt.msgType, chat, 0))
				last := i == len(sequence)-1
				if last && !errors.Is(err, antiban.ErrSendCapReached) {
					t.Fatalf("send %d via %s: %v, want cap (phone and LID are one chat)", i, chat, err)
				}
				if !last && err != nil {
					t.Fatalf("send %d via %s: %v", i, chat, err)
				}
			}
		})
	}
}
