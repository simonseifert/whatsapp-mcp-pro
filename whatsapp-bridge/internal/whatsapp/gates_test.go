package whatsapp

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/config"
)

// gateContacts is what group create, add members, rename, topic and the
// disappearing timer run before they touch WhatsApp (review-bridge #2).
func TestGateContacts(t *testing.T) {
	const known = "4915100000001@s.whatsapp.net"
	const stranger = "4915199999999@s.whatsapp.net"
	const otherStranger = "4915188888888@s.whatsapp.net"
	const group = "120363000000000001@g.us"

	tests := []struct {
		name      string
		allowlist []string
		timelock  bool
		newCap    int // NewRecipientsPerDay, 0 = off
		msgType   antiban.MessageType
		targets   []string
		want      SendErrorKind // "" = allowed
	}{
		{name: "no gates configured", msgType: antiban.Text, targets: []string{known, stranger}},
		{name: "allowlist refuses an unlisted participant", allowlist: []string{"4915100000001"},
			msgType: antiban.Text, targets: []string{known, stranger}, want: SendErrForbidden},
		{name: "allowlist allows when every participant is listed", allowlist: []string{"4915100000001", "4915199999999"},
			msgType: antiban.Text, targets: []string{known, stranger}},
		{name: "allowlist refuses an unlisted group", allowlist: []string{"4915100000001"},
			msgType: antiban.Edit, targets: []string{group}, want: SendErrForbidden},
		{name: "allowlist allows a listed group", allowlist: []string{group},
			msgType: antiban.Edit, targets: []string{group}},
		{name: "bare number does not allow a LID", allowlist: []string{"4915100000001"},
			msgType: antiban.Text, targets: []string{"4915100000001@lid"}, want: SendErrForbidden},
		{name: "reachout lock refuses a new contact", timelock: true,
			msgType: antiban.Text, targets: []string{stranger}, want: SendErrReachoutTimelock},
		{name: "reachout lock still allows an existing chat", timelock: true,
			msgType: antiban.Text, targets: []string{known}},
		{name: "reachout lock does not apply to a group", timelock: true,
			msgType: antiban.Edit, targets: []string{group}},
		{name: "one new contact among known ones trips the lock", timelock: true,
			msgType: antiban.Text, targets: []string{known, stranger}, want: SendErrReachoutTimelock},
		{name: "each new participant counts as a first contact", newCap: 1,
			msgType: antiban.Text, targets: []string{stranger, otherStranger}, want: SendErrCapReached},
		{name: "known participants are not first contacts", newCap: 1,
			msgType: antiban.Text, targets: []string{known, known}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newReachoutTestStore(t)
			if err := store.StoreChat(known, "Known", time.Now()); err != nil {
				t.Fatal(err)
			}
			if err := store.StoreMessage("M1", known, known, "Known", "hello", time.Now(), false, "", "", "", "", nil, nil, nil, 0); err != nil {
				t.Fatal(err)
			}
			c := newOfflineClient(store)
			c.cfg = &config.Config{AllowlistJIDs: tt.allowlist}
			ic, err := antiban.NewSendInterceptor(&antiban.Config{
				CapsEnabled:         true,
				NewRecipientsPerDay: tt.newCap,
				CapsStatePath:       filepath.Join(t.TempDir(), "caps.json"),
			})
			if err != nil {
				t.Fatal(err)
			}
			c.antiban = ic
			if tt.timelock {
				c.setReachoutTimelock(time.Now().Add(time.Hour))
			}
			targets, se := parseGateTargets(tt.targets, "test")
			if se != nil {
				t.Fatal(se)
			}
			got := c.gateContacts(context.Background(), store, tt.msgType, targets)
			switch {
			case tt.want == "" && got != nil:
				t.Fatalf("refused: %v", got)
			case tt.want != "" && got == nil:
				t.Fatalf("allowed, want %s", tt.want)
			case tt.want != "" && got.Kind != tt.want:
				t.Fatalf("kind %s, want %s (%v)", got.Kind, tt.want, got)
			}
		})
	}
}

func TestGateWithoutRecipientAndTyping(t *testing.T) {
	c := newOfflineClient(nil)
	if se := c.gateWithoutRecipient(context.Background(), antiban.Text); se != nil {
		t.Fatalf("no allowlist: refused %v", se)
	}
	c.cfg = &config.Config{AllowlistJIDs: []string{"4915100000001"}}
	if se := c.gateWithoutRecipient(context.Background(), antiban.Text); se == nil || se.Kind != SendErrForbidden {
		t.Fatalf("newsletter create with an allowlist = %v, want forbidden", se)
	}
	// Typing goes through the allowlist only.
	if err := c.SendTypingIndicator("4915199999999@s.whatsapp.net", "typing"); err == nil || ClassifySendError(err) != SendErrForbidden {
		t.Fatalf("typing to an unlisted chat = %v, want forbidden", err)
	}
	if got := disappearingTimerType(types.NewJID("1", types.DefaultUserServer)); got != antiban.Text {
		t.Fatalf("1:1 timer type = %v, want text", got)
	}
	if got := disappearingTimerType(types.NewJID("1", types.GroupServer)); got != antiban.Edit {
		t.Fatalf("group timer type = %v, want edit", got)
	}
}
