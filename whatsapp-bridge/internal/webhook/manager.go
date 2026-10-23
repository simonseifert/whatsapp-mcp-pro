package webhook

import (
	"fmt"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/types"
	"whatsapp-bridge/internal/whatsapp"

	waTypes "go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Manager handles webhook processing and delivery
type Manager struct {
	messageStore    *database.MessageStore
	logger          waLog.Logger
	configs         []*types.WebhookConfig
	mutex           sync.RWMutex
	delivery        *DeliveryService
	downloadBaseURL string
}

// NewManager creates a new webhook manager
func NewManager(messageStore *database.MessageStore, logger waLog.Logger) *Manager {
	port := os.Getenv("API_PORT")
	if port == "" {
		port = "8080"
	}

	return &Manager{
		messageStore:    messageStore,
		logger:          logger,
		configs:         make([]*types.WebhookConfig, 0),
		delivery:        NewDeliveryService(messageStore, logger),
		downloadBaseURL: fmt.Sprintf("http://localhost:%s/api/download", port),
	}
}

// LoadWebhookConfigs loads webhook configurations from database
func (wm *Manager) LoadWebhookConfigs() error {
	wm.mutex.Lock()
	defer wm.mutex.Unlock()

	configs, err := wm.messageStore.GetAllWebhookConfigs()
	if err != nil {
		return fmt.Errorf("failed to load webhook configs: %v", err)
	}

	wm.configs = configs
	wm.logger.Infof("Loaded %d webhook configurations", len(configs))

	// Debug logging
	for i, config := range configs {
		wm.logger.Infof("Webhook %d: ID=%d, Name=%s, Triggers=%d", i, config.ID, config.Name, len(config.Triggers))
		for j, trigger := range config.Triggers {
			wm.logger.Infof("  Trigger %d: type=%s, value=%s, match=%s, enabled=%t",
				j, trigger.TriggerType, trigger.TriggerValue, trigger.MatchType, trigger.Enabled)
		}
	}

	return nil
}

// GetWebhookConfigs returns a copy of current webhook configurations
func (wm *Manager) GetWebhookConfigs() []*types.WebhookConfig {
	wm.mutex.RLock()
	defer wm.mutex.RUnlock()

	// Return a copy to avoid race conditions
	configs := make([]*types.WebhookConfig, len(wm.configs))
	copy(configs, wm.configs)
	return configs
}

// MatchesTriggers checks if a message matches any webhook triggers. chatJIDs
// are extra identities of the chat (canonical and alternate) that a chat_jid
// trigger may also match; see chatForms.
func (wm *Manager) MatchesTriggers(msg *events.Message, chatName string, chatJIDs ...string) []*types.WebhookConfig {
	wm.mutex.RLock()
	defer wm.mutex.RUnlock()

	var matchedConfigs []*types.WebhookConfig

	// Extract message content
	content := whatsapp.ExtractTextContent(msg.Message)
	mediaType, _, _, _, _, _, _, _ := whatsapp.ExtractMediaInfo(msg.Message)

	for _, config := range wm.configs {
		if !config.Enabled {
			continue
		}

		matched := false
		for _, trigger := range config.Triggers {
			if !trigger.Enabled {
				continue
			}

			if wm.matchesTrigger(trigger, msg, content, mediaType, chatName, chatJIDs) {
				matched = true
				break
			}
		}

		if matched {
			matchedConfigs = append(matchedConfigs, config)
		}
	}

	return matchedConfigs
}

// matchesTrigger checks if a single trigger matches the message
func (wm *Manager) matchesTrigger(trigger types.WebhookTrigger, msg *events.Message, content, mediaType, chatName string, chatJIDs []string) bool {
	switch trigger.TriggerType {
	case "all":
		return true

	case "chat_jid":
		// A trigger written against either identity keeps matching: the raw
		// JID is what was sent before chat_jid was normalised, the others are
		// what the payload and the store use now.
		for _, jid := range append([]string{msg.Info.Chat.String()}, chatJIDs...) {
			if jid != "" && wm.matchesString(jid, trigger.TriggerValue, trigger.MatchType) {
				return true
			}
		}
		return false

	case "sender":
		senderJID := msg.Info.Sender.String()
		senderUser := msg.Info.Sender.User
		return wm.matchesString(senderJID, trigger.TriggerValue, trigger.MatchType) ||
			wm.matchesString(senderUser, trigger.TriggerValue, trigger.MatchType)

	case "keyword":
		return wm.matchesString(content, trigger.TriggerValue, trigger.MatchType)

	case "media_type":
		return wm.matchesString(mediaType, trigger.TriggerValue, trigger.MatchType)

	case "event":
		// Matches change events only (DeliverEvent), never a new message.
		return false

	default:
		wm.logger.Warnf("Unknown trigger type: %s", trigger.TriggerType)
		return false
	}
}

// matchesString performs string matching based on match type
func (wm *Manager) matchesString(text, pattern, matchType string) bool {
	switch matchType {
	case "exact":
		return text == pattern

	case "contains":
		return strings.Contains(strings.ToLower(text), strings.ToLower(pattern))

	case "regex":
		matched, err := regexp.MatchString(pattern, text)
		if err != nil {
			wm.logger.Warnf("Invalid regex pattern '%s': %v", pattern, err)
			return false
		}
		return matched

	default:
		wm.logger.Warnf("Unknown match type: %s", matchType)
		return false
	}
}

// DeliverConnectionEvent broadcasts a connection state change to all enabled webhooks.
// Unlike message webhooks, these are not trigger-filtered — any webhook that is enabled
// receives connection events so operators can monitor bridge health out-of-band.
func (wm *Manager) DeliverConnectionEvent(payload *types.ConnectionEventPayload) {
	wm.mutex.RLock()
	configs := make([]*types.WebhookConfig, len(wm.configs))
	copy(configs, wm.configs)
	wm.mutex.RUnlock()

	if len(configs) == 0 {
		return
	}

	// Build a synthetic WebhookPayload wrapping the connection event.
	systemTrigger := types.WebhookTrigger{TriggerType: "system", TriggerValue: payload.EventType, MatchType: "exact", Enabled: true}
	for _, config := range configs {
		if !config.Enabled {
			continue
		}
		wp := &types.WebhookPayload{
			EventType: payload.EventType,
			Timestamp: payload.Timestamp,
			WebhookConfig: types.WebhookConfigInfo{
				ID:   config.ID,
				Name: config.Name,
			},
			Trigger: types.WebhookTriggerInfo{
				Type:      "system",
				Value:     payload.EventType,
				MatchType: "exact",
			},
			Metadata: types.WebhookMetadata{DeliveryAttempt: 1},
		}
		// Embed connection details in the message field so existing payload schema is preserved.
		wp.Message = types.WebhookMessageInfo{
			Content: payload.EventType,
			Sender:  payload.JID,
		}
		localConfig := config
		localTrigger := systemTrigger
		go wm.delivery.DeliverWebhook(localConfig, wp, "system-"+payload.EventType, "", &localTrigger)
	}
}

// DeliverEvent sends a message_edited, message_deleted or call event to the
// webhooks that asked for it with an "event" trigger whose value matches the
// event type (e.g. trigger_type "event", trigger_value "message_edited").
//
// Opt-in on purpose: these payloads carry a chat, a sender and text just like
// message_received, and a consumer subscribed with "all" that never looks at
// event_type would take an edit for a new message and act on it twice.
func (wm *Manager) DeliverEvent(eventType string, info types.WebhookMessageInfo, call *types.WebhookCallInfo) {
	if wm == nil {
		return
	}
	wm.mutex.RLock()
	configs := make([]*types.WebhookConfig, len(wm.configs))
	copy(configs, wm.configs)
	wm.mutex.RUnlock()

	if info.ChatName == "" && wm.messageStore != nil && info.ChatJID != "" {
		info.ChatName = wm.messageStore.IdentityDisplayName(info.ChatJID)
	}
	if info.SenderName == "" && wm.messageStore != nil && info.Sender != "" {
		info.SenderName = wm.messageStore.IdentityDisplayName(info.Sender)
	}
	for _, config := range configs {
		matched := eventTrigger(config, eventType, wm.matchesString)
		if matched == nil {
			continue
		}
		payload := buildEventPayload(config, matched, eventType, info, call)
		go wm.delivery.DeliverWebhook(config, payload, eventType+"-"+info.ID, info.ChatJID, matched)
	}
}

// eventTrigger returns the config's enabled "event" trigger for eventType, or
// nil. Only an explicit event trigger opts in (see DeliverEvent).
func eventTrigger(config *types.WebhookConfig, eventType string, match func(text, pattern, matchType string) bool) *types.WebhookTrigger {
	if config == nil || !config.Enabled {
		return nil
	}
	for i := range config.Triggers {
		t := config.Triggers[i]
		if t.Enabled && t.TriggerType == "event" && match(eventType, t.TriggerValue, t.MatchType) {
			return &t
		}
	}
	return nil
}

func buildEventPayload(config *types.WebhookConfig, trig *types.WebhookTrigger, eventType string,
	info types.WebhookMessageInfo, call *types.WebhookCallInfo) *types.WebhookPayload {
	payload := &types.WebhookPayload{
		EventType:     eventType,
		Timestamp:     time.Now().UTC().Format(time.RFC3339),
		WebhookConfig: types.WebhookConfigInfo{ID: config.ID, Name: config.Name},
		Trigger:       types.WebhookTriggerInfo{Type: trig.TriggerType, Value: trig.TriggerValue, MatchType: trig.MatchType},
		Message:       info,
		Metadata:      types.WebhookMetadata{DeliveryAttempt: 1},
		Call:          call,
	}
	if strings.HasSuffix(info.ChatJID, "@g.us") {
		payload.Metadata.GroupInfo = &types.GroupInfo{IsGroup: true, GroupName: info.ChatName}
	}
	return payload
}

// senderName resolves a message's author the way the MCP tools do: the
// identity directory first (address-book name or nickname), then the push
// name, then the bare number. Senders arrive as LIDs more and more, so the
// alternate JID is tried too.
func (wm *Manager) senderName(msg *events.Message) string {
	if wm.messageStore != nil {
		for _, jid := range []string{msg.Info.Sender.ToNonAD().String(), msg.Info.SenderAlt.ToNonAD().String()} {
			if jid == "" {
				continue
			}
			if name := wm.messageStore.IdentityDisplayName(jid); name != "" {
				return name
			}
		}
	}
	if msg.Info.PushName != "" {
		return msg.Info.PushName
	}
	return msg.Info.Sender.User
}

// ProcessMessage processes a message and sends webhooks if triggers match
func (wm *Manager) ProcessMessage(client interface{}, msg *events.Message, chatName string) {
	startTime := time.Now()

	chatJID, chatJIDAlt := chatForms(client, msg.Info.Chat)

	// Find matching webhook configurations
	matchedConfigs := wm.MatchesTriggers(msg, chatName, chatJID, chatJIDAlt)
	if len(matchedConfigs) == 0 {
		return
	}

	wm.logger.Infof("Found %d matching webhook configs for message %s", len(matchedConfigs), msg.Info.ID)

	basePayload := buildMessagePayload(msg, chatName, wm.senderName(msg), chatJID, chatJIDAlt, wm.downloadBaseURL)
	basePayload.Metadata.ProcessingTimeMs = time.Since(startTime).Milliseconds()

	// Send webhooks for each matched configuration
	for _, config := range matchedConfigs {
		// Find the specific trigger that matched
		var matchedTrigger *types.WebhookTrigger
		content := whatsapp.ExtractTextContent(msg.Message)
		mediaType, _, _, _, _, _, _, _ := whatsapp.ExtractMediaInfo(msg.Message)

		for _, trigger := range config.Triggers {
			if trigger.Enabled && wm.matchesTrigger(trigger, msg, content, mediaType, chatName, []string{chatJID, chatJIDAlt}) {
				matchedTrigger = &trigger
				break
			}
		}

		if matchedTrigger == nil {
			continue
		}

		// Customize payload for this webhook
		payload := basePayload
		payload.WebhookConfig = types.WebhookConfigInfo{
			ID:   config.ID,
			Name: config.Name,
		}
		payload.Trigger = types.WebhookTriggerInfo{
			Type:      matchedTrigger.TriggerType,
			Value:     matchedTrigger.TriggerValue,
			MatchType: matchedTrigger.MatchType,
		}
		payload.Metadata.DeliveryAttempt = 1

		// Send webhook asynchronously
		go wm.delivery.DeliverWebhook(config, &payload, msg.Info.ID, chatJID, matchedTrigger)
	}
}

// chatJIDResolver is the part of *whatsapp.Client the webhook needs; an
// interface so tests can stand in for a live client.
type chatJIDResolver interface {
	ChatJIDForms(chat waTypes.JID) (canonical, alt waTypes.JID)
}

// chatForms returns the chat JID the store files the message under and the
// other identity of the chat when known. Sending the raw msg.Info.Chat made
// a consumer see "<lid>@lid" for a chat the store and the MCP call
// "<phone>@s.whatsapp.net", so matching webhook events against stored chats
// silently failed for LID-addressed contacts.
func chatForms(client interface{}, chat waTypes.JID) (canonical, alt string) {
	if r, ok := client.(chatJIDResolver); ok && r != nil {
		c, a := r.ChatJIDForms(chat)
		if !c.IsEmpty() {
			canonical = c.String()
			if !a.IsEmpty() && a != c {
				alt = a.String()
			}
			return canonical, alt
		}
	}
	return chat.ToNonAD().String(), ""
}

// buildMessagePayload assembles the message_received payload shared by every
// matched webhook config.
func buildMessagePayload(msg *events.Message, chatName, senderName, chatJID, chatJIDAlt, downloadBaseURL string) types.WebhookPayload {
	content := whatsapp.ExtractTextContent(msg.Message)
	mediaType, filename, _, _, _, _, _, _ := whatsapp.ExtractMediaInfo(msg.Message)
	quotedMsgID, quotedSender := whatsapp.ExtractQuotedContext(msg.Message)

	payload := types.WebhookPayload{
		EventType: "message_received",
		Timestamp: msg.Info.Timestamp.Format(time.RFC3339),
		Message: types.WebhookMessageInfo{
			ID:              msg.Info.ID,
			ChatJID:         chatJID,
			ChatJIDAlt:      chatJIDAlt,
			ChatName:        chatName,
			Sender:          msg.Info.Sender.String(),
			SenderName:      senderName,
			Content:         content,
			Timestamp:       msg.Info.Timestamp.Format(time.RFC3339),
			PushName:        msg.Info.PushName,
			IsFromMe:        msg.Info.IsFromMe,
			MediaType:       mediaType,
			Filename:        filename,
			QuotedMessageID: quotedMsgID,
			QuotedSender:    quotedSender,
		},
	}
	if mediaType != "" {
		payload.Message.MediaDownloadURL = downloadBaseURL
	}
	if msg.Info.Chat.Server == waTypes.GroupServer {
		payload.Metadata.GroupInfo = &types.GroupInfo{
			IsGroup:   true,
			GroupName: chatName,
			// ParticipantCount would require additional API call
		}
	}
	return payload
}
