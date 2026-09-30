package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"

	"whatsapp-bridge/internal/database"
)

// ErrNoStoredMessages means a chat has nothing stored to anchor a history
// request on. The phone only answers "messages older than X".
var ErrNoStoredMessages = errors.New("no messages stored for this chat")

// HistoryAnchor is the oldest stored message of a chat, in the form
// RequestChatHistory takes.
type HistoryAnchor struct {
	MsgID     string
	FromMe    bool
	Sender    string
	Timestamp time.Time
}

// OldestHistoryAnchor looks up the oldest stored message of a chat so callers
// don't have to supply it by hand.
func (c *Client) OldestHistoryAnchor(store *database.MessageStore, chatJID string) (HistoryAnchor, error) {
	id, sender, fromMe, ts, err := store.OldestMessage(chatJID)
	if errors.Is(err, sql.ErrNoRows) {
		return HistoryAnchor{}, ErrNoStoredMessages
	}
	if err != nil {
		return HistoryAnchor{}, fmt.Errorf("lookup oldest message: %w", err)
	}
	return HistoryAnchor{MsgID: id, FromMe: fromMe, Sender: c.historySenderJID(chatJID, sender), Timestamp: ts}, nil
}

// historySenderJID turns a stored sender into a full JID. Senders are stored
// bare (a phone number, or a LID number since WhatsApp's LID migration), but
// the history request needs the full JID of the anchor message's author.
func (c *Client) historySenderJID(chatJID, sender string) string {
	if sender == "" {
		// A 1:1 incoming message was written by the other side of the chat.
		if !strings.HasSuffix(chatJID, "@g.us") {
			return chatJID
		}
		return ""
	}
	if strings.Contains(sender, "@") {
		return sender
	}
	lid := types.JID{User: sender, Server: types.HiddenUserServer}
	if c.Client != nil && c.Store != nil && c.Store.LIDs != nil {
		if pn, err := c.Store.LIDs.GetPNForLID(context.Background(), lid); err == nil && !pn.IsEmpty() {
			return pn.String()
		}
	}
	return sender + "@" + types.DefaultUserServer
}

// BackfillStatus reports a background history backfill for one chat.
type BackfillStatus struct {
	ChatJID    string    `json:"chat_jid"`
	Until      string    `json:"until,omitempty"`
	Running    bool      `json:"running"`
	Batches    int       `json:"batches"`
	MaxBatches int       `json:"max_batches"`
	Oldest     string    `json:"oldest,omitempty"`
	StoppedBy  string    `json:"stopped_by,omitempty"`
	Error      string    `json:"error,omitempty"`
	StartedAt  time.Time `json:"started_at"`
	FinishedAt time.Time `json:"finished_at,omitempty"`
}

var (
	backfillMu   sync.Mutex
	backfillJobs = map[string]*BackfillStatus{}
)

// backfillWait is how long one batch may take to arrive before it counts as a
// miss. The phone usually answers within seconds.
var backfillWait = 60 * time.Second

// StartBackfill pulls older history for a chat in the background, 50 messages
// per request, until the phone has nothing older (three misses in a row), the
// oldest message predates until, or maxBatches requests have been made.
// Every request makes the phone show a "Finished syncing" notification.
func (c *Client) StartBackfill(store *database.MessageStore, chatJID string, until time.Time, maxBatches int) (BackfillStatus, error) {
	if maxBatches <= 0 || maxBatches > 200 {
		maxBatches = 40
	}
	backfillMu.Lock()
	if job, ok := backfillJobs[chatJID]; ok && job.Running {
		snapshot := *job
		backfillMu.Unlock()
		return snapshot, fmt.Errorf("a backfill is already running for %s", chatJID)
	}
	job := &BackfillStatus{ChatJID: chatJID, Running: true, MaxBatches: maxBatches, StartedAt: time.Now()}
	if !until.IsZero() {
		job.Until = until.Format("2006-01-02")
	}
	backfillJobs[chatJID] = job
	snapshot := *job
	backfillMu.Unlock()

	go c.runBackfill(store, job, until)
	return snapshot, nil
}

// BackfillStatuses returns every backfill started since the bridge came up.
func BackfillStatuses() []BackfillStatus {
	backfillMu.Lock()
	defer backfillMu.Unlock()
	out := make([]BackfillStatus, 0, len(backfillJobs))
	for _, job := range backfillJobs {
		out = append(out, *job)
	}
	return out
}

func (c *Client) runBackfill(store *database.MessageStore, job *BackfillStatus, until time.Time) {
	finish := func(reason string, err error) {
		backfillMu.Lock()
		defer backfillMu.Unlock()
		job.Running = false
		job.StoppedBy = reason
		if err != nil {
			job.Error = err.Error()
		}
		job.FinishedAt = time.Now()
		c.logger.Infof("[BACKFILL] %s stopped: %s after %d batches (oldest %s)", job.ChatJID, reason, job.Batches, job.Oldest)
	}

	misses := 0
	for {
		anchor, err := c.OldestHistoryAnchor(store, job.ChatJID)
		if err != nil {
			finish("error", err)
			return
		}
		backfillMu.Lock()
		job.Oldest = anchor.Timestamp.Format(time.RFC3339)
		batches := job.Batches
		backfillMu.Unlock()

		switch {
		case !until.IsZero() && anchor.Timestamp.Before(until):
			finish("reached until", nil)
			return
		case misses >= 3:
			finish("phone has nothing older", nil)
			return
		case batches >= job.MaxBatches:
			finish("max batches", nil)
			return
		}

		if err := c.RequestChatHistory(job.ChatJID, anchor.MsgID, anchor.FromMe, anchor.Sender, anchor.Timestamp.UnixMilli(), 50); err != nil {
			finish("error", err)
			return
		}
		backfillMu.Lock()
		job.Batches++
		backfillMu.Unlock()

		if c.waitForOlder(store, job.ChatJID, anchor.MsgID) {
			misses = 0
		} else {
			misses++
		}
	}
}

// waitForOlder polls until the chat's oldest message is no longer anchorID.
func (c *Client) waitForOlder(store *database.MessageStore, chatJID, anchorID string) bool {
	deadline := time.Now().Add(backfillWait)
	for time.Now().Before(deadline) {
		time.Sleep(5 * time.Second)
		if id, _, _, _, err := store.OldestMessage(chatJID); err == nil && id != anchorID {
			return true
		}
	}
	return false
}
