package whatsapp

import (
	"context"
	"os"
	"strconv"
	"time"

	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/mediastore"
)

// The media sweep saves recent media that has no copy on disk yet.
//
// Only live incoming messages are auto-downloaded. Media this bridge sends and
// media delivered by history sync is stored as a row with keys and no file, and
// WhatsApp's servers purge the file after a few weeks. Once that happens only a
// phone that still holds it can bring it back, and since September 2026 that
// mostly fails too (see DECISIONS D14). So the bytes have to be fetched while
// they exist. A failed auto-download is picked up here as well.
//
// It never asks the phone: a sweep is exactly the kind of bulk job that must not
// produce "Finished syncing" notifications.

// MediaSweepConfig tunes the media sweep.
type MediaSweepConfig struct {
	// MaxAge is how far back to look. Older media is rarely still on the servers.
	MaxAge time.Duration
	// Interval is the time between runs, and StartDelay the wait before the
	// first, so a restart doesn't compete with reconnecting.
	Interval, StartDelay time.Duration
	// MaxPerRun caps downloads per run, Pace is the wait between them.
	MaxPerRun int
	Pace      time.Duration
}

// DefaultMediaSweepConfig returns the defaults.
func DefaultMediaSweepConfig() MediaSweepConfig {
	return MediaSweepConfig{
		MaxAge:     14 * 24 * time.Hour,
		Interval:   24 * time.Hour,
		StartDelay: 10 * time.Minute,
		MaxPerRun:  300,
		Pace:       2 * time.Second,
	}
}

// MediaSweepConfigFromEnv reads MEDIA_SWEEP_DAYS over the defaults. Invalid
// values keep the default. The second result is false when it is "0", which
// turns the sweep off.
func MediaSweepConfigFromEnv() (MediaSweepConfig, bool) {
	cfg := DefaultMediaSweepConfig()
	raw := os.Getenv("MEDIA_SWEEP_DAYS")
	if n, err := strconv.Atoi(raw); err == nil {
		if n == 0 {
			return cfg, false
		}
		if n > 0 {
			cfg.MaxAge = time.Duration(n) * 24 * time.Hour
		}
	}
	return cfg, true
}

// mediaSweep is one bridge's sweep.
type mediaSweep struct {
	store  *database.MessageStore
	cfg    MediaSweepConfig
	logger waLog.Logger

	// Seams for tests; production wires them to the whatsmeow client.
	now       func() time.Time
	connected func() bool
	fetch     func(ctx context.Context, messageID, chatJID string) (data []byte, mediaType, filename string, err error)
}

// StartMediaSweep runs the sweep until ctx is cancelled.
func (c *Client) StartMediaSweep(ctx context.Context, store *database.MessageStore, cfg MediaSweepConfig) {
	s := &mediaSweep{
		store: store, cfg: cfg, logger: c.logger, now: time.Now, connected: c.IsConnected,
		fetch: func(ctx context.Context, messageID, chatJID string) ([]byte, string, string, error) {
			return c.FetchMessageMedia(ctx, store, messageID, chatJID, false)
		},
	}
	go s.loop(ctx)
	c.logger.Infof("[MEDIA] sweep started: media up to %s old, every %s", cfg.MaxAge, cfg.Interval)
}

func (s *mediaSweep) loop(ctx context.Context) {
	timer := time.NewTimer(s.cfg.StartDelay)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			saved, failed, err := s.run(ctx)
			switch {
			case err != nil:
				s.logger.Warnf("[MEDIA] sweep: %v", err)
			case saved+failed > 0:
				s.logger.Infof("[MEDIA] sweep: saved %d, could not fetch %d", saved, failed)
			}
			timer.Reset(s.cfg.Interval)
		}
	}
}

// run saves what it can of the recent media with no copy on disk. A file that
// can't be fetched is simply tried again next run until it ages out.
func (s *mediaSweep) run(ctx context.Context) (saved, failed int, err error) {
	if !s.connected() {
		return 0, 0, nil
	}
	rows, err := s.store.RecentMediaMessages(s.now().Add(-s.cfg.MaxAge))
	if err != nil {
		return 0, 0, err
	}
	for _, m := range rows {
		if saved+failed >= s.cfg.MaxPerRun || ctx.Err() != nil || !s.connected() {
			break
		}
		if hasLocalMediaCopy(m) {
			continue
		}
		select {
		case <-ctx.Done():
			return saved, failed, nil
		case <-time.After(s.cfg.Pace):
		}
		data, mediaType, filename, err := s.fetch(ctx, m.ID, m.ChatJID)
		if err != nil {
			failed++
			continue
		}
		if _, err := mediastore.Save(m.ChatJID, m.ID, mediaType, filename, data); err != nil {
			s.logger.Warnf("[MEDIA] sweep: could not save %s: %v", m.ID, err)
			failed++
			continue
		}
		saved++
	}
	return saved, failed, nil
}

// hasLocalMediaCopy reports whether either media layout holds this message's
// file, hash-checked as /api/download does before serving one.
func hasLocalMediaCopy(m database.MediaMessage) bool {
	if paths, err := AutoDownloadPaths(m.ChatJID, m.ID, m.Filename); err == nil && FindAutoDownloaded(paths, m.FileSHA256) != "" {
		return true
	}
	p, err := mediastore.Path(m.ChatJID, m.ID, m.MediaType, m.Filename)
	return err == nil && FindAutoDownloaded([]string{p}, m.FileSHA256) != ""
}
