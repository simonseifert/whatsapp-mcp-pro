package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/mediastore"
)

// ErrNoMedia is returned when the message exists but carries no media.
var ErrNoMedia = errors.New("message has no media")

// ErrMediaTooLarge is returned when a media file is above MEDIA_MAX_BYTES
// (mediastore.MaxBytes), by its declared length or by the bytes received.
var ErrMediaTooLarge = errors.New("media is larger than MEDIA_MAX_BYTES")

// CheckMediaSize refuses a declared file length above the shared media cap.
// A length of 0 means "not declared" and passes; callers that must not trust
// a missing length (auto-download) decide that themselves.
func CheckMediaSize(declared int64) error {
	if max := mediastore.MaxBytes(); declared > max {
		return fmt.Errorf("%w: declared %d bytes, limit %d", ErrMediaTooLarge, declared, max)
	}
	return nil
}

// mediaSlots bounds concurrent media downloads. whatsmeow buffers each file
// whole (and decrypts into a second buffer), and every incoming media message
// used to start its own download, so a burst of forwarded large files could
// exhaust memory.
var (
	mediaSlotsOnce sync.Once
	mediaSlots     chan struct{}
)

// DefaultMediaDownloadConcurrency is MEDIA_DOWNLOAD_CONCURRENCY's default.
const DefaultMediaDownloadConcurrency = 3

func mediaConcurrency() int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv("MEDIA_DOWNLOAD_CONCURRENCY"))); err == nil && v > 0 {
		return v
	}
	return DefaultMediaDownloadConcurrency
}

// acquireMediaSlot waits for a free download slot, or ctx ends. The returned
// function releases it.
func acquireMediaSlot(ctx context.Context) (release func(), err error) {
	mediaSlotsOnce.Do(func() { mediaSlots = make(chan struct{}, mediaConcurrency()) })
	select {
	case mediaSlots <- struct{}{}:
		return func() { <-mediaSlots }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// AcquireMediaSlot takes one of the shared MEDIA_DOWNLOAD_CONCURRENCY download
// slots, for download paths outside this package (the /api/download CDN
// branch). It waits until one is free or ctx ends; call the returned function
// to release it.
func AcquireMediaSlot(ctx context.Context) (release func(), err error) {
	return acquireMediaSlot(ctx)
}

// ResetMediaSlots re-reads MEDIA_DOWNLOAD_CONCURRENCY. Tests only.
func ResetMediaSlots() { mediaSlotsOnce = sync.Once{} }

// cappedFile is a temp file that refuses to grow past limit bytes. whatsmeow's
// DownloadToFile copies the response body into it, so the cap holds against
// the real size of the download, whatever the sender's file_length claims.
// ReadFrom and WriteString are shadowed on purpose: the embedded *os.File's
// would let io.Copy bypass Write, and also lets whatsmeow preallocate a size the server names.
type cappedFile struct {
	*os.File
	limit int64
}

func (f *cappedFile) Write(p []byte) (int, error) {
	pos, err := f.File.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0, err
	}
	if pos+int64(len(p)) > f.limit {
		return 0, fmt.Errorf("%w: download is larger than %d bytes", ErrMediaTooLarge, f.limit)
	}
	return f.File.Write(p)
}

func (f *cappedFile) WriteAt(p []byte, off int64) (int, error) {
	if off+int64(len(p)) > f.limit {
		return 0, fmt.Errorf("%w: download is larger than %d bytes", ErrMediaTooLarge, f.limit)
	}
	return f.File.WriteAt(p, off)
}

func (f *cappedFile) WriteString(str string) (int, error) { return f.Write([]byte(str)) }

func (f *cappedFile) ReadFrom(r io.Reader) (int64, error) {
	return io.Copy(struct{ io.Writer }{f}, r)
}

// mediaWireOverhead is what the encrypted body adds to the plaintext (padding
// up to a block, plus the 10-byte MAC), so a file at exactly the cap still fits.
const mediaWireOverhead = 64

// downloadToTemp streams msg into a new 0600 temp file in dir and returns its
// path. The cap is enforced while bytes arrive: the download is aborted and the
// partial file deleted as soon as it passes MEDIA_MAX_BYTES, and a finished
// file is re-checked. The caller removes the file. It does not take a download
// slot; the caller does.
func (c *Client) downloadToTemp(ctx context.Context, msg whatsmeow.DownloadableMessage, dir string) (string, error) {
	if l, ok := msg.(interface{ GetFileLength() uint64 }); ok {
		if err := CheckMediaSize(int64(l.GetFileLength())); err != nil {
			return "", err
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("media temp dir: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".dl-*")
	if err != nil {
		return "", fmt.Errorf("create media temp file: %w", err)
	}
	path := tmp.Name()
	max := mediastore.MaxBytes()
	capped := &cappedFile{File: tmp, limit: max + mediaWireOverhead}
	download := c.rawDownloadToFile
	if download == nil {
		download = c.Client.DownloadToFile
	}
	err = download(ctx, msg, capped)
	if err == nil {
		var info os.FileInfo
		if info, err = tmp.Stat(); err == nil && info.Size() > max {
			err = fmt.Errorf("%w: download is %d bytes, limit %d", ErrMediaTooLarge, info.Size(), max)
		}
	}
	if cerr := tmp.Close(); err == nil && cerr != nil {
		err = cerr
	}
	if err != nil {
		_ = os.Remove(path)
		return "", err
	}
	return path, nil
}

// CleanStaleDownloadTemps removes download temp files (.dl-*, .autodl-*) older
// than maxAge from root and its immediate subdirectories, which a crash or a
// kill mid-download leaves behind. Returns how many it removed.
func CleanStaleDownloadTemps(root string, maxAge time.Duration) int {
	removed := 0
	var scan func(dir string, depth int)
	scan = func(dir string, depth int) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return
		}
		for _, e := range entries {
			path := filepath.Join(dir, e.Name())
			if e.IsDir() {
				if depth < 1 {
					scan(path, depth+1)
				}
				continue
			}
			if !strings.HasPrefix(e.Name(), ".dl-") && !strings.HasPrefix(e.Name(), ".autodl-") {
				continue
			}
			if info, err := e.Info(); err == nil && info.Mode().IsRegular() && time.Since(info.ModTime()) > maxAge {
				if os.Remove(path) == nil {
					removed++
				}
			}
		}
	}
	scan(root, 0)
	return removed
}

// downloadMedia downloads msg behind the concurrency bound and returns the
// bytes. It streams to a temp file under the size cap first, so memory holds at
// most one capped copy per slot instead of whatever the sender serves.
func (c *Client) downloadMedia(ctx context.Context, msg whatsmeow.DownloadableMessage) ([]byte, error) {
	release, err := acquireMediaSlot(ctx)
	if err != nil {
		return nil, fmt.Errorf("waiting for a media download slot: %w", err)
	}
	defer release()
	path, err := c.downloadToTemp(ctx, msg, filepath.Join("store", "tmp"))
	if err != nil {
		return nil, err
	}
	defer func() { _ = os.Remove(path) }()
	return os.ReadFile(path)
}

// ErrMessageNotFound is returned when the message id+jid is not in the store.
var ErrMessageNotFound = errors.New("message not found")

// FetchMessageMedia downloads and decrypts a stored message's media through
// whatsmeow, which re-resolves direct_path against current media hosts. Unlike
// the URL stored with the message, that keeps working after the signed CDN
// link has expired, for as long as WhatsApp still holds the file.
//
// askPhone says whether purged media may be requested from the sender's phone
// when media retry is on. Bulk jobs pass false: each ask notifies the user, and
// only about one in nine has brought a file back.
func (c *Client) FetchMessageMedia(ctx context.Context, store *database.MessageStore, messageID, chatJID string, askPhone bool) (data []byte, mediaType, filename string, err error) {
	media, err := loadStoredMedia(store, messageID, chatJID)
	if err != nil {
		return nil, "", "", err
	}

	data, err = c.downloadMedia(ctx, media.msg)
	if askPhone && isMediaGone(err) && MediaRetryEnabled() {
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
	case "sticker":
		msg = &waE2E.StickerMessage{
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

// downloadMediaType maps a stored media_type to the whatsmeow media class that
// selects the decryption keys. Stickers are encrypted with the image keys.
func downloadMediaType(mediaType string) (whatsmeow.MediaType, error) {
	switch strings.ToLower(mediaType) {
	case "image", "sticker":
		return whatsmeow.MediaImage, nil
	case "video":
		return whatsmeow.MediaVideo, nil
	case "audio":
		return whatsmeow.MediaAudio, nil
	case "document":
		return whatsmeow.MediaDocument, nil
	}
	return "", fmt.Errorf("unsupported media type %q", mediaType)
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
// Every step is also recorded in media_requests. It shares the queue's hourly
// budget and refuses when the phone has already said the file is gone, see
// askPhoneForMedia. If the wait times out the request stays "sent" and a late
// answer is still saved by the background queue.
func (c *Client) retryMediaDownload(ctx context.Context, store *database.MessageStore, messageID, chatJID string, msg whatsmeow.DownloadableMessage, mediaKey []byte) ([]byte, error) {
	info, err := c.mediaRetryInfo(store, messageID, chatJID)
	if err != nil {
		return nil, err
	}
	cfg := c.mediaRetryConfig()
	maxAttempts := cfg.MaxAttempts

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

	if err := c.askPhoneForMedia(ctx, store, messageID, chatJID, cfg, func(ctx context.Context) error {
		return c.Client.SendMediaRetryReceipt(ctx, info, mediaKey)
	}); err != nil {
		return nil, err
	}
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
	data, err := c.downloadMedia(ctx, msg)
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
	case *waE2E.StickerMessage:
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
	data, mediaType, filename, err := c.FetchMessageMedia(ctx, store, messageID, chatJID, true)
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
