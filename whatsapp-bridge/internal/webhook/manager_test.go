package webhook

import (
	"encoding/json"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	waTypes "go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	"whatsapp-bridge/internal/types"
)

var (
	phoneJID = waTypes.JID{User: "4915112345678", Server: waTypes.DefaultUserServer}
	lidJID   = waTypes.JID{User: "123456789012345", Server: waTypes.HiddenUserServer}
	groupJID = waTypes.JID{User: "120363000000000001", Server: waTypes.GroupServer}
)

// fakeResolver maps a LID chat to its phone twin, as a live client does once
// the LID mapping is known.
type fakeResolver struct{ pairs map[waTypes.JID]waTypes.JID }

func (f fakeResolver) ChatJIDForms(chat waTypes.JID) (waTypes.JID, waTypes.JID) {
	other, ok := f.pairs[chat]
	if !ok {
		return chat, waTypes.EmptyJID
	}
	if chat.Server == waTypes.HiddenUserServer {
		return other, chat
	}
	return chat, other
}

func textMessage(chat, sender waTypes.JID) *events.Message {
	return &events.Message{
		Info: waTypes.MessageInfo{
			MessageSource: waTypes.MessageSource{Chat: chat, Sender: sender},
			ID:            "3EB0TEST",
			Timestamp:     time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC),
		},
		Message: &waE2E.Message{Conversation: proto.String("hi")},
	}
}

func TestBuildMessagePayloadChatJID(t *testing.T) {
	mapped := fakeResolver{pairs: map[waTypes.JID]waTypes.JID{lidJID: phoneJID, phoneJID: lidJID}}
	tests := []struct {
		name     string
		client   interface{}
		chat     waTypes.JID
		wantChat string
		wantAlt  string
		wantGrp  bool
	}{
		{"LID chat with mapping sends phone JID", mapped, lidJID, phoneJID.String(), lidJID.String(), false},
		{"phone chat with mapping carries LID alt", mapped, phoneJID, phoneJID.String(), lidJID.String(), false},
		{"LID chat without mapping stays LID", fakeResolver{}, lidJID, lidJID.String(), "", false},
		{"no resolver falls back to raw chat", nil, lidJID, lidJID.String(), "", false},
		{"group has no alt", mapped, groupJID, groupJID.String(), "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			msg := textMessage(tt.chat, tt.chat)
			chat, alt := chatForms(tt.client, msg.Info.Chat)
			p := buildMessagePayload(msg, "Chat", "Name", chat, alt, "http://localhost:8080/api/download")
			if p.Message.ChatJID != tt.wantChat || p.Message.ChatJIDAlt != tt.wantAlt {
				t.Errorf("chat_jid = %q alt = %q, want %q / %q", p.Message.ChatJID, p.Message.ChatJIDAlt, tt.wantChat, tt.wantAlt)
			}
			if (p.Metadata.GroupInfo != nil) != tt.wantGrp {
				t.Errorf("group info = %v, want group %v", p.Metadata.GroupInfo, tt.wantGrp)
			}
			raw, err := json.Marshal(p.Message)
			if err != nil {
				t.Fatal(err)
			}
			var m map[string]any
			_ = json.Unmarshal(raw, &m)
			if _, present := m["chat_jid_alt"]; present != (tt.wantAlt != "") {
				t.Errorf("chat_jid_alt present = %v in %s", present, raw)
			}
			if m["content"] != "hi" {
				t.Errorf("content = %v", m["content"])
			}
		})
	}
}

func TestChatJIDTriggerMatchesEitherIdentity(t *testing.T) {
	wm := &Manager{logger: waLog.Noop}
	msg := textMessage(lidJID, lidJID)
	forms := []string{phoneJID.String(), lidJID.String()}
	tests := []struct {
		name  string
		value string
		forms []string
		want  bool
	}{
		{"old trigger on LID", lidJID.String(), forms, true},
		{"trigger on phone JID", phoneJID.String(), forms, true},
		{"phone JID without known mapping", phoneJID.String(), nil, false},
		{"other chat", "4915100000000@s.whatsapp.net", forms, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			trig := types.WebhookTrigger{TriggerType: "chat_jid", TriggerValue: tt.value, MatchType: "exact", Enabled: true}
			if got := wm.matchesTrigger(trig, msg, "hi", "", "Chat", tt.forms); got != tt.want {
				t.Errorf("matchesTrigger = %v, want %v", got, tt.want)
			}
		})
	}
}
