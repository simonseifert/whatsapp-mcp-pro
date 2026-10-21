package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
)

// ErrNoMedia is returned when the message exists but carries no media.
var ErrNoMedia = errors.New("message has no media")

// ErrMessageNotFound is returned when the message id+jid is not in the store.
var ErrMessageNotFound = errors.New("message not found")

// FetchMessageMedia downloads and decrypts a stored message's media through
// whatsmeow, which re-resolves direct_path against current media hosts. Unlike
// the URL stored with the message, that keeps working after the signed CDN
// link has expired, for as long as WhatsApp still holds the file.
func (c *Client) FetchMessageMedia(ctx context.Context, store *database.MessageStore, messageID, chatJID string) (data []byte, mediaType, filename string, err error) {
	media, err := loadStoredMedia(store, messageID, chatJID)
	if err != nil {
		return nil, "", "", err
	}

	data, err = c.Client.Download(ctx, media.msg)
	if isMediaGone(err) && MediaRetryEnabled() {
		// Persist the ask before trying it here, so a timeout, an offline
		// phone or a restart leaves it to the background queue.
		if _, qerr := store.EnqueueMediaRequest(messageID, chatJID, time.Now()); qerr != nil {
			c.logger.Warnf("[MEDIA] could not queue media retry for %s: %v", messageID, qerr)
		}
		data, err = c.retryMediaDownload(ctx, store, messageID, chatJID, media.msg, media.mediaKey)
	}
	if err != nil {
		return nil, "", "", fmt.Errorf("whatsmeow download: %w", err)
	}
	return data, media.mediaType, media.filename, nil
}

// storedMedia is a stored message's media, ready for whatsmeow to download.
type storedMedia struct {
	mediaType, filename string
	mediaKey            []byte
	msg                 whatsmeow.DownloadableMessage
}

// loadStoredMedia rebuilds a downloadable media message from the store.
func loadStoredMedia(store *database.MessageStore, messageID, chatJID string) (*storedMedia, error) {
	mediaType, filename, url, directPath, mediaKey, fileSHA256, fileEncSHA256, fileLength, err := store.GetMessageMedia(messageID, chatJID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrMessageNotFound
		}
		return nil, fmt.Errorf("lookup message: %w", err)
	}
	if mediaType == "" {
		return nil, ErrNoMedia
	}

	urlCopy := url
	directPathCopy := directPath
	lengthCopy := fileLength
	var msg whatsmeow.DownloadableMessage
	switch mediaType {
	case "image":
		msg = &waE2E.ImageMessage{
			URL: &urlCopy, DirectPath: &directPathCopy, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &lengthCopy,
		}
	case "video":
		msg = &waE2E.VideoMessage{
			URL: &urlCopy, DirectPath: &directPathCopy, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &lengthCopy,
		}
	case "audio":
		msg = &waE2E.AudioMessage{
			URL: &urlCopy, DirectPath: &directPathCopy, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &lengthCopy,
		}
	case "document":
		msg = &waE2E.DocumentMessage{
			URL: &urlCopy, DirectPath: &directPathCopy, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &lengthCopy,
		}
	default:
		return nil, fmt.Errorf("unsupported media type %q", mediaType)
	}
	return &storedMedia{mediaType: mediaType, filename: filename, mediaKey: mediaKey, msg: msg}, nil
}

// isMediaGone reports a download error that means WhatsApp's servers no longer
// hold the file. Only the sender's phone can bring it back.
func isMediaGone(err error) bool {
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410)
}

// mediaRetryWait bounds how long a download waits for the sender's phone to
// re-upload. The MCP server gives up on the bridge after 30 s.
var mediaRetryWait = 25 * time.Second

var (
	mediaRetryMu      sync.Mutex
	mediaRetryWaiters = map[types.MessageID][]chan *events.MediaRetry{}
)

// HandleMediaRetry hands a phone's answer to a media retry request to the
// download waiting for it. Answers nobody waits for (late ones, or ones to
// receipts the background queue sent) go to the persistent queue, which
// downloads the file into store/media; without a queue they are dropped.
func (c *Client) HandleMediaRetry(evt *events.MediaRetry) {
	mediaRetryMu.Lock()
	waiters := mediaRetryWaiters[evt.MessageID]
	delete(mediaRetryWaiters, evt.MessageID)
	mediaRetryMu.Unlock()
	if len(waiters) == 0 {
		// Off the event goroutine: resolving means a media download.
		go c.resolveQueuedMediaRetry(evt)
		return
	}
	for _, ch := range waiters {
		select {
		case ch <- evt:
		default:
		}
	}
}

// mediaRetryInfo rebuilds the message identity a media retry receipt needs.
func (c *Client) mediaRetryInfo(store *database.MessageStore, messageID, chatJID string) (*types.MessageInfo, error) {
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return nil, fmt.Errorf("media retry: bad chat JID: %w", err)
	}
	var sender string
	var fromMe bool
	if err := store.GetDB().QueryRow(
		"SELECT COALESCE(sender, ''), is_from_me FROM messages WHERE id = ? AND chat_jid = ?", messageID, chatJID,
	).Scan(&sender, &fromMe); err != nil {
		return nil, fmt.Errorf("media retry: lookup sender: %w", err)
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: fromMe, IsGroup: chat.Server == types.GroupServer},
		ID:            types.MessageID(messageID),
	}
	if fromMe && c.Store != nil && c.Store.ID != nil {
		info.Sender = c.Store.ID.ToNonAD()
	} else if s, err := types.ParseJID(c.historySenderJID(chatJID, sender)); err == nil {
		info.Sender = s
	}
	return info, nil
}

// sendMediaRetryReceipt is the background queue's send: a receipt only, the
// answer arrives later through HandleMediaRetry.
func (c *Client) sendMediaRetryReceipt(ctx context.Context, store *database.MessageStore, messageID, chatJID string) error {
	media, err := loadStoredMedia(store, messageID, chatJID)
	if err != nil {
		return err
	}
	info, err := c.mediaRetryInfo(store, messageID, chatJID)
	if err != nil {
		return err
	}
	if err := c.Client.SendMediaRetryReceipt(ctx, info, media.mediaKey); err != nil {
		return fmt.Errorf("media retry: send receipt: %w", err)
	}
	return nil
}

// retryMediaDownload asks the phone that sent a message to re-upload its media.
//
// Off unless MEDIA_RETRY_ENABLED=true: every retry makes the user's phone show
// a "Finished syncing" notification, and a bulk media download on 2026-10-01
// sent 172 of them in ten minutes.
//
// When on, it sends a media retry receipt, waits for the new direct path, downloads from it and
// remembers it. This only works while the sender still has the file, and
// their phone has to be online to answer.
//
// Every step is also recorded in media_requests. This path does not wait for
// the queue's hourly budget, so a caller is never worse off than before, but
// its receipt counts against that budget. If the wait times out the request
// stays "sent" and a late answer is still saved by the background queue.
func (c *Client) retryMediaDownload(ctx context.Context, store *database.MessageStore, messageID, chatJID string, msg whatsmeow.DownloadableMessage, mediaKey []byte) ([]byte, error) {
	info, err := c.mediaRetryInfo(store, messageID, chatJID)
	if err != nil {
		return nil, err
	}
	maxAttempts := c.mediaRetryMaxAttempts()

	ch := make(chan *events.MediaRetry, 1)
	mediaRetryMu.Lock()
	mediaRetryWaiters[info.ID] = append(mediaRetryWaiters[info.ID], ch)
	mediaRetryMu.Unlock()
	defer func() {
		mediaRetryMu.Lock()
		defer mediaRetryMu.Unlock()
		waiters := mediaRetryWaiters[info.ID]
		for i, w := range waiters {
			if w == ch {
				mediaRetryWaiters[info.ID] = append(waiters[:i], waiters[i+1:]...)
				break
			}
		}
		if len(mediaRetryWaiters[info.ID]) == 0 {
			delete(mediaRetryWaiters, info.ID)
		}
	}()

	if err := c.Client.SendMediaRetryReceipt(ctx, info, mediaKey); err != nil {
		c.markMediaRequest(store.MarkMediaRequestSendError(messageID, chatJID, err.Error(), maxAttempts, time.Now()))
		return nil, fmt.Errorf("media retry: send receipt: %w", err)
	}
	c.markMediaRequest(store.MarkMediaRequestSent(messageID, chatJID, time.Now()))
	c.logger.Infof("[MEDIA] asked the sender's phone to re-upload %s in %s", messageID, chatJID)

	var evt *events.MediaRetry
	select {
	case evt = <-ch:
	case <-time.After(mediaRetryWait):
		return nil, fmt.Errorf("media is gone from WhatsApp's servers and the sender's phone did not re-upload it within %s; the retry stays queued", mediaRetryWait)
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	notif, err := whatsmeow.DecryptMediaRetryNotification(evt, mediaKey)
	switch outcome, reason := classifyMediaRetryAnswer(notif, err); outcome {
	case mediaRetryFailed:
		c.markMediaRequest(store.MarkMediaRequestFailed(messageID, chatJID, reason, time.Now()))
		return nil, fmt.Errorf("media retry: %s", reason)
	case mediaRetryAgain:
		c.markMediaRequest(store.MarkMediaRequestRetry(messageID, chatJID, reason, maxAttempts, time.Now()))
		return nil, fmt.Errorf("media retry: %s", reason)
	}

	newPath := notif.GetDirectPath()
	if !setDirectPath(msg, newPath) {
		return nil, fmt.Errorf("media retry: unsupported message type")
	}
	data, err := c.Client.Download(ctx, msg)
	if err != nil {
		c.markMediaRequest(store.MarkMediaRequestRetry(messageID, chatJID, "download re-uploaded file: "+err.Error(), maxAttempts, time.Now()))
		return nil, fmt.Errorf("media retry: download re-uploaded file: %w", err)
	}
	c.refreshDirectPath(store, messageID, chatJID, newPath)
	c.markMediaRequest(store.MarkMediaRequestSucceeded(messageID, chatJID, time.Now()))
	return data, nil
}

// setDirectPath points a media message at a new direct path and drops its
// stale URL so whatsmeow builds the download URL from the path.
func setDirectPath(msg whatsmeow.DownloadableMessage, path string) bool {
	empty := ""
	switch m := msg.(type) {
	case *waE2E.ImageMessage:
		m.DirectPath, m.URL = &path, &empty
	case *waE2E.VideoMessage:
		m.DirectPath, m.URL = &path, &empty
	case *waE2E.AudioMessage:
		m.DirectPath, m.URL = &path, &empty
	case *waE2E.DocumentMessage:
		m.DirectPath, m.URL = &path, &empty
	default:
		return false
	}
	return true
}

// DownloadMessageMedia decrypts and saves media for a stored message.
func (c *Client) DownloadMessageMedia(ctx context.Context, store *database.MessageStore, mediaDir, messageID, chatJID string) (string, string, error) {
	data, mediaType, filename, err := c.FetchMessageMedia(ctx, store, messageID, chatJID)
	if err != nil {
		return "", "", err
	}

	// Strip path separators defensively before joining.
	safeJID := strings.ReplaceAll(chatJID, "/", "_")
	safeName := strings.ReplaceAll(filename, "/", "_")
	if safeName == "" {
		safeName = messageID
	}
	dir := filepath.Join(mediaDir, safeJID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", fmt.Errorf("mkdir %s: %w", dir, err)
	}
	out := filepath.Join(dir, safeName)
	if err := os.WriteFile(out, data, 0o644); err != nil {
		return "", "", fmt.Errorf("write %s: %w", out, err)
	}
	abs, err := filepath.Abs(out)
	if err != nil {
		return out, mediaType, nil
	}
	return abs, mediaType, nil
}
