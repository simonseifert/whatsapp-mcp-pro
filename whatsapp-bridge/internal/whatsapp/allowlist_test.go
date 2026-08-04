package whatsapp

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

// The allowlist is the backstop for autonomous senders (wa-dispatch,
// wa-assistant) that send without a human approving each message. These cases
// exist because an earlier implementation also accepted
// `strings.Contains(fullJID, entry)`, which made several plausible-looking
// entries allow everything.
func TestIsRecipientAllowed(t *testing.T) {
	pn := func(user string) types.JID {
		return types.JID{User: user, Server: "s.whatsapp.net"}
	}
	group := func(id string) types.JID {
		return types.JID{User: id, Server: "g.us"}
	}

	tests := []struct {
		name      string
		allowlist []string
		recipient types.JID
		want      bool
	}{
		// Documented syntax: bare number, full JID, group JID.
		{"bare number matches", []string{"4915112345678"}, pn("4915112345678"), true},
		{"full jid matches", []string{"4915112345678@s.whatsapp.net"}, pn("4915112345678"), true},
		{"group jid matches", []string{"1203630123456789@g.us"}, group("1203630123456789"), true},
		{"case insensitive", []string{"1203630123456789@G.US"}, group("1203630123456789"), true},
		{"one of several", []string{"111", "4915112345678", "222"}, pn("4915112345678"), true},
		{"surrounding whitespace tolerated", []string{" 4915112345678 "}, pn("4915112345678"), true},

		// Plain denials.
		{"unlisted number denied", []string{"4915112345678"}, pn("4915199999999"), false},
		{"empty allowlist denies", []string{}, pn("4915112345678"), false},
		{"blank entries ignored", []string{"", "   "}, pn("4915112345678"), false},

		// The bypasses. Each of these was ALLOWED by the substring version.
		{"server suffix must not allow everyone",
			[]string{"s.whatsapp.net"}, pn("4915199999999"), false},
		{"bare tld must not allow everyone",
			[]string{"net"}, pn("4915199999999"), false},
		{"at sign must not allow everyone",
			[]string{"@"}, pn("4915199999999"), false},
		{"country-code prefix must not allow the whole country",
			[]string{"49"}, pn("4915199999999"), false},
		{"allowed number as a substring of another must not match",
			[]string{"4915112345678"}, pn("14915112345678"), false},
		{"digits appearing mid-number must not match",
			[]string{"12345"}, pn("9991234599999"), false},
		{"group suffix must not allow every group",
			[]string{"g.us"}, group("1203630999999999"), false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsRecipientAllowed(tc.allowlist, tc.recipient); got != tc.want {
				t.Errorf("IsRecipientAllowed(%q, %s) = %v, want %v",
					tc.allowlist, tc.recipient.String(), got, tc.want)
			}
		})
	}
}
