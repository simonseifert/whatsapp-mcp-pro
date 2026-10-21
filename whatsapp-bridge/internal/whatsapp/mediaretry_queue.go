package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/mediastore"
)

// The persistent media retry queue.
//
// A media retry used to live only inside the /api/download request that hit
// purged media: send a receipt, wait 25 s, give up. A bridge restart, or a
// phone that was simply offline, lost the request for good. Requests are now
// rows in media_requests, worked by a paced loop, and the phone's answer is
// handled whenever it arrives, saving the file where /api/download serves it.
//
// The pacing is the point. Every receipt makes the user's phone show
// "Finished syncing", and an unpaced bulk download once sent 172 of them in
// ten minutes. The idea follows mautrix-whatsapp's media request loop.

// MediaRetryConfig tunes the background media retry loop.
type MediaRetryConfig struct {
	// MaxPerHour caps receipts sent in any rolling hour, counting the ones
	// /api/download sends synchronously.
	MaxPerHour int
	// MaxAttempts is how many receipts a request gets before it is failed.
	MaxAttempts int
	// BatchSize caps receipts sent per tick, so a backlog drains gradually.
	BatchSize int
	// Interval is the time between ticks.
	Interval time.Duration
	// AnswerTimeout is how long a sent receipt may go unanswered before it is
	// treated as lost (phone offline) and the request goes back to pending.
	AnswerTimeout time.Duration
}

// DefaultMediaRetryConfig returns the conservative defaults.
func DefaultMediaRetryConfig() MediaRetryConfig {
	return MediaRetryConfig{
		MaxPerHour:    20,
		MaxAttempts:   3,
		BatchSize:     3,
		Interval:      time.Minute,
		AnswerTimeout: time.Hour,
	}
}

// MediaRetryEnabled reports whether MEDIA_RETRY_ENABLED=true. Off by default
// because every retry pings the user's phone.
func MediaRetryEnabled() bool {
	return os.Getenv("MEDIA_RETRY_ENABLED") == "true"
}

// MediaRetryConfigFromEnv reads MEDIA_RETRY_MAX_PER_HOUR and
// MEDIA_RETRY_MAX_ATTEMPTS over the defaults. Unset, non-numeric or
// non-positive values keep the default rather than disabling the cap.
func MediaRetryConfigFromEnv() MediaRetryConfig {
	cfg := DefaultMediaRetryConfig()
	if n := positiveEnvInt("MEDIA_RETRY_MAX_PER_HOUR"); n > 0 {
		cfg.MaxPerHour = n
	}
	if n := positiveEnvInt("MEDIA_RETRY_MAX_ATTEMPTS"); n > 0 {
		cfg.MaxAttempts = n
	}
	return cfg
}

func positiveEnvInt(name string) int {
	n, err := strconv.Atoi(os.Getenv(name))
	if err != nil || n <= 0 {
		return 0
	}
	return n
}

// mediaRetryBudget is how many receipts the next tick may send: what is left
// of the hourly cap, never more than one batch.
func mediaRetryBudget(sentLastHour, maxPerHour, batchSize int) int {
	left := maxPerHour - sentLastHour
	if left <= 0 {
		return 0
	}
	if batchSize > 0 && left > batchSize {
		return batchSize
	}
	return left
}

// MediaRetryQueue works the media_requests table.
type MediaRetryQueue struct {
	store  *database.MessageStore
	cfg    MediaRetryConfig
	logger waLog.Logger
	ctx    context.Context

	// Seams for tests; production wires them to the whatsmeow client.
	now       func() time.Time
	connected func() bool
	send      func(ctx context.Context, req database.MediaRequest) error
}

func newMediaRetryQueue(store *database.MessageStore, cfg MediaRetryConfig, logger waLog.Logger,
	connected func() bool, send func(context.Context, database.MediaRequest) error) *MediaRetryQueue {
	if logger == nil {
		logger = waLog.Noop
	}
	return &MediaRetryQueue{
		store:     store,
		cfg:       cfg,
		logger:    logger,
		ctx:       context.Background(),
		now:       time.Now,
		connected: connected,
		send:      send,
	}
}

// StartMediaRetryQueue starts the background loop until ctx is cancelled and
// routes unclaimed media retry answers to it. Call once, before connecting.
func (c *Client) StartMediaRetryQueue(ctx context.Context, store *database.MessageStore, cfg MediaRetryConfig) *MediaRetryQueue {
	q := newMediaRetryQueue(store, cfg, c.logger, c.IsConnected, func(ctx context.Context, req database.MediaRequest) error {
		return c.sendMediaRetryReceipt(ctx, store, req.MessageID, req.ChatJID)
	})
	q.ctx = ctx
	c.mediaRetryQueue.Store(q)
	go q.run(ctx)
	c.logger.Infof("[MEDIA] retry queue started: max %d/hour, %d attempts each", cfg.MaxPerHour, cfg.MaxAttempts)
	return q
}

// mediaRetryMaxAttempts is the attempt cap the synchronous path records with.
func (c *Client) mediaRetryMaxAttempts() int {
	if q := c.mediaRetryQueue.Load(); q != nil {
		return q.cfg.MaxAttempts
	}
	return DefaultMediaRetryConfig().MaxAttempts
}

func (q *MediaRetryQueue) run(ctx context.Context) {
	// The first tick waits a full interval: at startup the client is still
	// connecting, and an immediate burst after every restart is exactly what
	// the hourly cap exists to prevent.
	ticker := time.NewTicker(q.cfg.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := q.Tick(ctx); err != nil {
				q.logger.Warnf("[MEDIA] retry queue tick: %v", err)
			}
		}
	}
}

// Tick runs one pass: expire unanswered receipts, then send up to the paced
// budget of pending ones. It does nothing while disconnected, so an outage
// neither burns attempts nor bunches sends. Returns the number of receipts sent.
func (q *MediaRetryQueue) Tick(ctx context.Context) (int, error) {
	if !q.connected() {
		return 0, nil
	}
	now := q.now()
	expired, err := q.store.ExpireSentMediaRequests(now.Add(-q.cfg.AnswerTimeout), q.cfg.MaxAttempts, now)
	if err != nil {
		return 0, err
	}
	if expired > 0 {
		q.logger.Infof("[MEDIA] %d media retries went unanswered for %s", expired, q.cfg.AnswerTimeout)
	}

	sentLastHour, err := q.store.CountMediaRequestsSentSince(now.Add(-time.Hour))
	if err != nil {
		return 0, err
	}
	budget := mediaRetryBudget(sentLastHour, q.cfg.MaxPerHour, q.cfg.BatchSize)
	if budget == 0 {
		return 0, nil
	}
	due, err := q.store.DueMediaRequests(budget)
	if err != nil {
		return 0, err
	}

	sent := 0
	for _, req := range due {
		// A drop mid-batch would fail every remaining send and burn an
		// attempt each; leave them pending for the next connected tick.
		if ctx.Err() != nil || !q.connected() {
			break
		}
		if err := q.send(ctx, req); err != nil {
			q.logger.Warnf("[MEDIA] retry receipt for %s in %s failed: %v", req.MessageID, req.ChatJID, err)
			if markErr := q.store.MarkMediaRequestSendError(req.MessageID, req.ChatJID, err.Error(), q.cfg.MaxAttempts, q.now()); markErr != nil {
				return sent, markErr
			}
			continue
		}
		if err := q.store.MarkMediaRequestSent(req.MessageID, req.ChatJID, q.now()); err != nil {
			return sent, err
		}
		sent++
		q.logger.Infof("[MEDIA] queued retry: asked the sender's phone to re-upload %s in %s (attempt %d)", req.MessageID, req.ChatJID, req.Attempts+1)
	}
	return sent, nil
}

// mediaRetryOutcome is what a phone's answer means for its request.
type mediaRetryOutcome int

const (
	mediaRetrySuccess mediaRetryOutcome = iota
	mediaRetryAgain                     // worth another receipt, attempts permitting
	mediaRetryFailed                    // final: the phone no longer has the file
)

// classifyMediaRetryAnswer interprets a decrypted media retry notification.
// NOT_FOUND and "not available on phone" are final: asking again only pings
// the phone. Anything else unexpected is retried within the attempt budget.
func classifyMediaRetryAnswer(notif *waMmsRetry.MediaRetryNotification, err error) (mediaRetryOutcome, string) {
	switch {
	case errors.Is(err, whatsmeow.ErrMediaNotAvailableOnPhone):
		return mediaRetryFailed, err.Error()
	case err != nil:
		return mediaRetryAgain, err.Error()
	}
	switch notif.GetResult() {
	case waMmsRetry.MediaRetryNotification_SUCCESS:
		if notif.GetDirectPath() == "" {
			return mediaRetryAgain, "phone answered SUCCESS without a direct path"
		}
		return mediaRetrySuccess, ""
	case waMmsRetry.MediaRetryNotification_NOT_FOUND:
		return mediaRetryFailed, "phone answered NOT_FOUND"
	default:
		return mediaRetryAgain, "phone answered " + notif.GetResult().String()
	}
}

// resolveQueuedMediaRetry handles a phone's answer no /api/download is
// waiting for: one that arrived after the request timed out, or one for a
// receipt the background loop sent.
func (c *Client) resolveQueuedMediaRetry(evt *events.MediaRetry) {
	q := c.mediaRetryQueue.Load()
	if q == nil {
		return
	}
	reqs, err := q.store.OpenMediaRequestsForMessage(string(evt.MessageID))
	if err != nil {
		c.logger.Warnf("[MEDIA] media retry answer for %s: %v", evt.MessageID, err)
		return
	}
	for _, req := range reqs {
		ctx, cancel := context.WithTimeout(q.ctx, 2*time.Minute)
		c.resolveMediaRequest(ctx, q, req, evt)
		cancel()
	}
}

// resolveMediaRequest applies one answer to one request and, on success,
// downloads the re-uploaded file into the /api/download layout.
func (c *Client) resolveMediaRequest(ctx context.Context, q *MediaRetryQueue, req database.MediaRequest, evt *events.MediaRetry) {
	now := q.now()
	media, err := loadStoredMedia(q.store, req.MessageID, req.ChatJID)
	if err != nil {
		c.markMediaRequest(q.store.MarkMediaRequestFailed(req.MessageID, req.ChatJID, err.Error(), now))
		return
	}

	notif, err := whatsmeow.DecryptMediaRetryNotification(evt, media.mediaKey)
	outcome, reason := classifyMediaRetryAnswer(notif, err)
	switch outcome {
	case mediaRetryFailed:
		c.logger.Infof("[MEDIA] %s in %s cannot be recovered: %s", req.MessageID, req.ChatJID, reason)
		c.markMediaRequest(q.store.MarkMediaRequestFailed(req.MessageID, req.ChatJID, reason, now))
		return
	case mediaRetryAgain:
		c.markMediaRequest(q.store.MarkMediaRequestRetry(req.MessageID, req.ChatJID, reason, q.cfg.MaxAttempts, now))
		return
	}

	newPath := notif.GetDirectPath()
	if !setDirectPath(media.msg, newPath) {
		c.markMediaRequest(q.store.MarkMediaRequestFailed(req.MessageID, req.ChatJID, "unsupported message type", now))
		return
	}
	data, err := c.Client.Download(ctx, media.msg)
	if err != nil {
		reason := fmt.Sprintf("download re-uploaded file: %v", err)
		c.markMediaRequest(q.store.MarkMediaRequestRetry(req.MessageID, req.ChatJID, reason, q.cfg.MaxAttempts, q.now()))
		return
	}
	c.refreshDirectPath(q.store, req.MessageID, req.ChatJID, newPath)

	// The direct path is fresh even if the save fails, so /api/download can
	// still fetch it; asking the phone again would only ping it for nothing.
	path, err := mediastore.Save(req.ChatJID, req.MessageID, media.mediaType, media.filename, data)
	if err != nil {
		c.logger.Warnf("[MEDIA] re-uploaded %s but could not save it: %v", req.MessageID, err)
	} else {
		c.logger.Infof("[MEDIA] recovered %s in %s via queued retry: %s", req.MessageID, req.ChatJID, path)
	}
	c.markMediaRequest(q.store.MarkMediaRequestSucceeded(req.MessageID, req.ChatJID, q.now()))
}

func (c *Client) markMediaRequest(err error) {
	if err != nil {
		c.logger.Warnf("[MEDIA] could not update media request: %v", err)
	}
}

// refreshDirectPath stores the path a phone re-uploaded to and drops the
// expired URL, so later downloads go straight to the new copy.
func (c *Client) refreshDirectPath(store *database.MessageStore, messageID, chatJID, newPath string) {
	if _, err := store.GetDB().Exec(
		"UPDATE messages SET direct_path = ?, url = '' WHERE id = ? AND chat_jid = ?", newPath, messageID, chatJID,
	); err != nil {
		c.logger.Warnf("[MEDIA] re-uploaded %s but could not store its new path: %v", messageID, err)
	}
}
