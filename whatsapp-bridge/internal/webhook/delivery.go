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
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/types"

	waLog "go.mau.fi/whatsmeow/util/log"
)

// ssrfSafeDialContext resolves the host itself, checks every address it got,
// and then connects to one of those checked addresses. Checking a lookup and
// then letting the dialer do its own is a DNS-rebinding window: a short-TTL
// answer can be public for the check and loopback for the connect. Dialing the
// IP we checked closes it; the Control hook re-checks the concrete address as
// a backstop for redirects and literal IPs.
func ssrfSafeDialContext() func(ctx context.Context, network, addr string) (net.Conn, error) {
	dialer := &net.Dialer{
		Timeout: 30 * time.Second,
		Control: func(network, address string, _ syscall.RawConn) error {
			if os.Getenv("DISABLE_SSRF_CHECK") == "true" {
				return nil
			}
			host, port, err := net.SplitHostPort(address)
			if err != nil {
				return fmt.Errorf("bad dial address %q: %w", address, err)
			}
			if ip := net.ParseIP(host); ip != nil && isPrivateIP(ip) && !allowedPrivateTarget(ip, port) {
				return fmt.Errorf("refusing to dial private address %s", host)
			}
			return nil
		},
	}
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		if os.Getenv("DISABLE_SSRF_CHECK") == "true" {
			return dialer.DialContext(ctx, network, addr)
		}
		host, port, err := net.SplitHostPort(addr)
		if err != nil {
			return nil, fmt.Errorf("bad dial address %q: %w", addr, err)
		}
		var ips []net.IP
		if ip := net.ParseIP(host); ip != nil {
			if isPrivateIP(ip) && !allowedPrivateTarget(ip, port) {
				return nil, fmt.Errorf("refusing to dial private address %s", host)
			}
			ips = []net.IP{ip}
		} else if ips, err = resolveChecked(ctx, host, port); err != nil {
			return nil, err
		}
		var lastErr error
		for _, ip := range ips {
			conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
			if err == nil {
				return conn, nil
			}
			lastErr = err
		}
		return nil, lastErr
	}
}

// Delivery pool defaults. A delivery can live for minutes (five attempts, 30s
// timeout each, backoff between), so one goroutine per message let a flood of
// messages against a slow endpoint grow without bound.
const (
	defaultDeliveryWorkers = 8
	defaultDeliveryQueue   = 1000
)

func envPositiveInt(name string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(name)); err == nil && v > 0 {
		return v
	}
	return def
}

// deliveryJob is one queued webhook delivery.
type deliveryJob struct {
	config    *types.WebhookConfig
	payload   *types.WebhookPayload
	messageID string
	chatJID   string
	trigger   *types.WebhookTrigger
}

// DeliveryService handles webhook delivery with retry logic through a fixed
// worker pool and a bounded queue.
type DeliveryService struct {
	messageStore *database.MessageStore
	logger       waLog.Logger
	httpClient   *http.Client

	queue   chan deliveryJob
	dropped atomic.Int64
	// deliver runs one job; DeliverWebhook in production, a stand-in in tests.
	deliver func(deliveryJob)
}

// NewDeliveryService creates a new delivery service with WEBHOOK_WORKERS
// workers (default 8) and a queue of WEBHOOK_QUEUE_SIZE jobs (default 1000).
func NewDeliveryService(messageStore *database.MessageStore, logger waLog.Logger) *DeliveryService {
	ds := newDeliveryService(messageStore, logger)
	ds.startWorkers(envPositiveInt("WEBHOOK_WORKERS", defaultDeliveryWorkers), envPositiveInt("WEBHOOK_QUEUE_SIZE", defaultDeliveryQueue))
	return ds
}

// startWorkers allocates the queue and starts the workers.
func (ds *DeliveryService) startWorkers(workers, queueSize int) {
	ds.queue = make(chan deliveryJob, queueSize)
	if ds.deliver == nil {
		ds.deliver = func(j deliveryJob) {
			ds.DeliverWebhook(j.config, j.payload, j.messageID, j.chatJID, j.trigger)
		}
	}
	for i := 0; i < workers; i++ {
		go func() {
			for job := range ds.queue {
				ds.deliver(job)
			}
		}()
	}
}

// Submit queues a delivery. When the queue is full the delivery is dropped,
// counted and logged, and false is returned: dropping under overload is
// better than letting the backlog take the bridge down.
func (ds *DeliveryService) Submit(config *types.WebhookConfig, payload *types.WebhookPayload, messageID, chatJID string, trigger *types.WebhookTrigger) bool {
	select {
	case ds.queue <- deliveryJob{config, payload, messageID, chatJID, trigger}:
		return true
	default:
		n := ds.dropped.Add(1)
		ds.logger.Errorf("Webhook delivery queue full: dropped delivery of %s to webhook %q (%d dropped so far)", messageID, config.Name, n)
		return false
	}
}

// Dropped is how many deliveries were dropped because the queue was full.
func (ds *DeliveryService) Dropped() int64 { return ds.dropped.Load() }

func newDeliveryService(messageStore *database.MessageStore, logger waLog.Logger) *DeliveryService {
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

	// The timestamp is sent whether or not there is a secret, so a receiver can
	// reject a stale delivery; with a secret it is also signed, which is what
	// makes a captured delivery useless to replay after the window.
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	req.Header.Set("X-Webhook-Timestamp", ts)
	if config.SecretToken != "" {
		// Legacy: HMAC of the body alone, kept so existing receivers keep
		// working. It does not cover the timestamp and is replayable.
		req.Header.Set("X-Webhook-Signature", ds.generateHMACSignature(payload, config.SecretToken))
		// Current: HMAC of "<timestamp>.<body>".
		req.Header.Set("X-Webhook-Signature-V2", ds.generateTimestampedSignature(ts, payload, config.SecretToken))
	}

	// Send request
	resp, err := ds.httpClient.Do(req)
	if err != nil {
		ds.logger.Errorf("HTTP request failed: %v", err)
		return false, 0, err.Error()
	}
	defer func() { _ = resp.Body.Close() }()

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

// generateTimestampedSignature signs timestamp + "." + body, so the timestamp
// header cannot be swapped for a fresh one on a captured delivery.
func (ds *DeliveryService) generateTimestampedSignature(timestamp string, payload []byte, secret string) string {
	h := hmac.New(sha256.New, []byte(secret))
	h.Write([]byte(timestamp))
	h.Write([]byte("."))
	h.Write(payload)
	return "sha256=" + hex.EncodeToString(h.Sum(nil))
}
