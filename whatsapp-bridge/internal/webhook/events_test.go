package webhook

import (
	"testing"

	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/types"
)

func TestEventTrigger(t *testing.T) {
	wm := &Manager{logger: waLog.Noop}
	trig := func(typ, value, match string, enabled bool) types.WebhookTrigger {
		return types.WebhookTrigger{TriggerType: typ, TriggerValue: value, MatchType: match, Enabled: enabled}
	}
	tests := []struct {
		name     string
		enabled  bool
		triggers []types.WebhookTrigger
		event    string
		want     bool
	}{
		{"exact event trigger", true, []types.WebhookTrigger{trig("event", "message_edited", "exact", true)}, "message_edited", true},
		{"other event", true, []types.WebhookTrigger{trig("event", "message_edited", "exact", true)}, "call", false},
		{"regex covers both changes", true, []types.WebhookTrigger{trig("event", "^message_(edited|deleted)$", "regex", true)}, "message_deleted", true},
		{"all does not opt in", true, []types.WebhookTrigger{trig("all", "", "exact", true)}, "message_edited", false},
		{"disabled trigger", true, []types.WebhookTrigger{trig("event", "call", "exact", false)}, "call", false},
		{"disabled webhook", false, []types.WebhookTrigger{trig("event", "call", "exact", true)}, "call", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &types.WebhookConfig{ID: 7, Name: "w", Enabled: tc.enabled, Triggers: tc.triggers}
			if got := eventTrigger(cfg, tc.event, wm.matchesString) != nil; got != tc.want {
				t.Errorf("matched = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestEventTriggerNeverMatchesMessages(t *testing.T) {
	wm := &Manager{logger: waLog.Noop}
	msg := textMessage(phoneJID, phoneJID)
	tr := types.WebhookTrigger{TriggerType: "event", TriggerValue: "hi", MatchType: "contains", Enabled: true}
	if wm.matchesTrigger(tr, msg, "hi", "", "Chat", nil) {
		t.Error("an event trigger must not fire on new messages")
	}
}

func TestBuildEventPayload(t *testing.T) {
	cfg := &types.WebhookConfig{ID: 7, Name: "w"}
	tr := &types.WebhookTrigger{TriggerType: "event", TriggerValue: "call", MatchType: "exact"}
	d := int64(42)
	p := buildEventPayload(cfg, tr, "call", types.WebhookMessageInfo{ID: "call-X", ChatJID: groupJID.String(), ChatName: "G"},
		&types.WebhookCallInfo{CallID: "X", Outcome: "answered", DurationSeconds: &d})
	if p.EventType != "call" || p.Call == nil || *p.Call.DurationSeconds != 42 || p.WebhookConfig.ID != 7 || p.Trigger.Value != "call" {
		t.Errorf("payload = %+v", p)
	}
	if p.Metadata.GroupInfo == nil || p.Metadata.GroupInfo.GroupName != "G" {
		t.Errorf("group info = %+v", p.Metadata.GroupInfo)
	}
}

func TestValidateAcceptsEventTrigger(t *testing.T) {
	t.Setenv("DISABLE_SSRF_CHECK", "true")
	wm := &Manager{logger: waLog.Noop}
	cfg := &types.WebhookConfig{Name: "w", WebhookURL: "https://example.com/hook", Enabled: true,
		Triggers: []types.WebhookTrigger{{TriggerType: "event", TriggerValue: "message_deleted", MatchType: "exact", Enabled: true}}}
	if err := wm.ValidateWebhookConfig(cfg); err != nil {
		t.Errorf("event trigger rejected: %v", err)
	}
	cfg.Triggers[0].TriggerType = "edits"
	if err := wm.ValidateWebhookConfig(cfg); err == nil {
		t.Error("unknown trigger type accepted")
	}
}
