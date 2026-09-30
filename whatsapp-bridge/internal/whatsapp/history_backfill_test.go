package whatsapp

import "testing"

func TestHistorySenderJID(t *testing.T) {
	c := &Client{}
	tests := []struct {
		name, chat, sender, want string
	}{
		{"full jid kept", "120363@g.us", "38591@s.whatsapp.net", "38591@s.whatsapp.net"},
		{"bare phone gets user server", "120363@g.us", "38591", "38591@s.whatsapp.net"},
		{"empty sender in dm is the other side", "38591@s.whatsapp.net", "", "38591@s.whatsapp.net"},
		{"empty sender in group stays empty", "120363@g.us", "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := c.historySenderJID(tt.chat, tt.sender); got != tt.want {
				t.Errorf("historySenderJID(%q, %q) = %q, want %q", tt.chat, tt.sender, got, tt.want)
			}
		})
	}
}
