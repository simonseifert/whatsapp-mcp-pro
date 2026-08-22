package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"syscall"
	"time"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/types"

	waLog "go.mau.fi/whatsmeow/util/log"
)

// ssrfSafeDialContext rejects connections to private/reserved IPs at the moment
// of dialing, after DNS has resolved. ValidateWebhookURL checks the hostname's
// IPs earlier, but that resolution and the actual connect are separate lookups —
// a short-TTL DNS-rebinding flip between them would otherwise slip a private
// address through. This closes that window by validating the concrete dialed IP.
func ssrfSafeDialContext() func(ctx context.Context, network, addr string) (net.Conn, error) {
	dialer := &net.Dialer{
		Timeout: 30 * time.Second,
		Control: func(network, address string, _ syscall.RawConn) error {
			if os.Getenv("DISABLE_SSRF_CHECK") == "true" {
				return nil
			}
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return fmt.Errorf("bad dial address %q: %w", address, err)
			}
			if ip := net.ParseIP(host); ip != nil && isPrivateIP(ip) {
				return fmt.Errorf("refusing to dial private address %s", host)
			}
			return nil
		},
	}
	return dialer.DialContext
}

// DeliveryService handles webhook delivery with retry logic
type DeliveryService struct {
	messageStore *database.MessageStore
	logger       waLog.Logger
	httpClient   *http.Client
}

// NewDeliveryService creates a new delivery service
func NewDeliveryService(messageStore *database.MessageStore, logger waLog.Logger) *DeliveryService {
	return &DeliveryService{
		messageStore: messageStore,
		logger:       logger,
		httpClient: &http.Client{
			Timeout:   30 * time.Second,
			Transport: &http.Transport{DialContext: ssrfSafeDialContext()},
			// The SSRF check in ValidateWebhookURL runs when a webhook is
			// *stored*. Without re-checking each redirect hop, a stored-safe
			// endpoint can 302 the delivery into a private address (cloud
			// metadata, localhost services) and the response body lands in
			// webhook_logs, readable over the API. Same check, later moment.
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				if len(via) >= 5 {
					return fmt.Errorf("too many redirects")
				}
				if err := ValidateWebhookURL(req.URL.String()); err != nil {
					return fmt.Errorf("redirect blocked: %w", err)
				}
				return nil
			},
		},
	}
}

// DeliverWebhook delivers a webhook with retry logic
func (ds *DeliveryService) DeliverWebhook(config *types.WebhookConfig, payload *types.WebhookPayload, messageID, chatJID string, trigger *types.WebhookTrigger) {
	maxRetries := 5
	backoffIntervals := []time.Duration{1 * time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second}

	if _, err := json.Marshal(payload); err != nil {
		ds.logger.Errorf("Failed to marshal webhook payload: %v", err)
		return
	}

	// Re-validate at delivery time: the URL was checked when stored, but DNS
	// can be repointed at a private address between then and now (rebinding).
	if err := ValidateWebhookURL(config.WebhookURL); err != nil {
		ds.logger.Warnf("Webhook %s delivery blocked: %v", config.Name, err)
		return
	}

	for attempt := 1; attempt <= maxRetries; attempt++ {
		payload.Metadata.DeliveryAttempt = attempt

		// Update payload with current attempt
		payloadBytes, _ := json.Marshal(payload)

		success, statusCode, responseBody := ds.sendHTTPRequest(config, payloadBytes)

		// Log the delivery attempt
		log := &types.WebhookLog{
			WebhookConfigID: config.ID,
			MessageID:       messageID,
			ChatJID:         chatJID,
			TriggerType:     trigger.TriggerType,
			TriggerValue:    trigger.TriggerValue,
			Payload:         string(payloadBytes),
			ResponseStatus:  statusCode,
			ResponseBody:    responseBody,
			AttemptCount:    attempt,
		}

		if success {
			now := time.Now()
			log.DeliveredAt = &now
			ds.logger.Infof("Webhook delivered successfully to %s (attempt %d)", config.WebhookURL, attempt)
		} else {
			ds.logger.Warnf("Webhook delivery failed to %s (attempt %d): status %d", config.WebhookURL, attempt, statusCode)
		}

		// Store log
		if err := ds.messageStore.StoreWebhookLog(log); err != nil {
			ds.logger.Errorf("Failed to store webhook log: %v", err)
		}

		if success {
			return // Success, no need to retry
		}

		// Wait before retry (except for last attempt)
		if attempt < maxRetries {
			time.Sleep(backoffIntervals[attempt-1])
		}
	}

	ds.logger.Errorf("Webhook delivery failed permanently to %s after %d attempts", config.WebhookURL, maxRetries)
}

// sendHTTPRequest sends the actual HTTP request
func (ds *DeliveryService) sendHTTPRequest(config *types.WebhookConfig, payload []byte) (success bool, statusCode int, responseBody string) {
	req, err := http.NewRequest("POST", config.WebhookURL, bytes.NewBuffer(payload))
	if err != nil {
		ds.logger.Errorf("Failed to create HTTP request: %v", err)
		return false, 0, err.Error()
	}

	// Set headers
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "WhatsApp-Bridge-Webhook/1.0")

	// Add HMAC signature if secret token is provided
	if config.SecretToken != "" {
		signature := ds.generateHMACSignature(payload, config.SecretToken)
		req.Header.Set("X-Webhook-Signature", signature)
	}

	// Send request
	resp, err := ds.httpClient.Do(req)
	if err != nil {
		ds.logger.Errorf("HTTP request failed: %v", err)
		return false, 0, err.Error()
	}
	defer resp.Body.Close()

	// Read response body
	responseBytes := make([]byte, 1024) // Limit response size
	n, _ := resp.Body.Read(responseBytes)
	responseBody = string(responseBytes[:n])

	// Consider 2xx status codes as successful
	success = resp.StatusCode >= 200 && resp.StatusCode < 300

	return success, resp.StatusCode, responseBody
}

// generateHMACSignature generates HMAC-SHA256 signature for webhook authentication
func (ds *DeliveryService) generateHMACSignature(payload []byte, secret string) string {
	h := hmac.New(sha256.New, []byte(secret))
	h.Write(payload)
	signature := hex.EncodeToString(h.Sum(nil))
	return "sha256=" + signature
}
