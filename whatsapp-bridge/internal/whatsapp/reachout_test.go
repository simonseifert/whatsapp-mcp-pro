package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/config"
	"whatsapp-bridge/internal/database"
)

// The WHATSAPP_ALLOWLIST_JIDS gate now covers reactions, polls, edits and
// deletes too (checkAllowlist); exact matching only, as for SendMessage.
func TestCheckAllowlistCoversAllMessageTypes(t *testing.T) {
	tests := []struct {
		name      string
		allowlist []string
		chat      string
		refused   bool
	}{
		{"no allowlist", nil, "4915199999999@s.whatsapp.net", false},
		{"listed JID", []string{"4915100000001@s.whatsapp.net"}, "4915100000001@s.whatsapp.net", false},
		{"listed number", []string{"4915100000001"}, "4915100000001@s.whatsapp.net", false},
		{"listed group", []string{"120363000000000000@g.us"}, "120363000000000000@g.us", false},
		{"unlisted", []string{"4915100000001"}, "4915199999999@s.whatsapp.net", true},
		{"no substring match", []string{"net"}, "4915199999999@s.whatsapp.net", true},
		{"unresolvable LID", []string{"4915100000001"}, "111222333@lid", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := &Client{logger: waLog.Noop, cfg: &config.Config{AllowlistJIDs: tt.allowlist}}
			jid, err := types.ParseJID(tt.chat)
			if err != nil {
				t.Fatal(err)
			}
			se := c.checkAllowlist(jid)
			if tt.refused != (se != nil) {
				t.Fatalf("checkAllowlist(%s) = %v, refused want %v", tt.chat, se, tt.refused)
			}
			if se != nil && se.Kind != SendErrForbidden {
				t.Fatalf("kind = %s, want forbidden", se.Kind)
			}
		})
	}
}

func newReachoutTestStore(t *testing.T) *database.MessageStore {
	t.Helper()
	name := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	store, err := database.OpenMessageStore("file:" + name + "?mode=memory&cache=shared")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// newOfflineClient is a Client with no whatsmeow connection: enough for the
// reachout bookkeeping, which must not depend on being online.
func newOfflineClient(store *database.MessageStore) *Client {
	c := &Client{logger: waLog.Noop}
	c.EnableReachoutTracking(store)
	return c
}

func TestReachoutGate(t *testing.T) {
	store := newReachoutTestStore(t)
	existing := "4915100000001@s.whatsapp.net"
	if err := store.StoreChat(existing, "Known", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("m1", existing, existing, "Known", "hello", time.Now(), false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	lidChat := "111222333@lid"
	if err := store.StoreChat(lidChat, "LID filed", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("m2", lidChat, lidChat, "LID filed", "hey", time.Now(), false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name      string
		lockFor   time.Duration // 0 = no lock; negative = expired
		recipient string
		aliases   []string
		blocked   bool
	}{
		{"no lock, new contact", 0, "4915199999999@s.whatsapp.net", nil, false},
		{"lock, new contact", time.Hour, "4915199999999@s.whatsapp.net", nil, true},
		{"lock, new LID contact", time.Hour, "999@lid", nil, true},
		{"lock, existing chat", time.Hour, existing, nil, false},
		{"lock, existing chat via device JID", time.Hour, "4915100000001:3@s.whatsapp.net", nil, false},
		{"lock, chat filed under alias", time.Hour, "4915188888888@s.whatsapp.net", []string{lidChat}, false},
		{"lock, group is not a new contact", time.Hour, "120363000000000000@g.us", nil, false},
		{"expired lock", -time.Minute, "4915199999999@s.whatsapp.net", nil, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := newOfflineClient(store)
			if tt.lockFor != 0 {
				c.setReachoutTimelock(time.Now().Add(tt.lockFor))
			}
			jid, err := types.ParseJID(tt.recipient)
			if err != nil {
				t.Fatal(err)
			}
			se := c.checkReachoutGate(store, jid, tt.aliases...)
			if tt.blocked {
				if se == nil {
					t.Fatal("expected send to be refused")
				}
				if se.Kind != SendErrReachoutTimelock || se.Until.IsZero() {
					t.Fatalf("got %+v, want reachout timelock with expiry", se)
				}
				if ClassifySendError(se) != SendErrReachoutTimelock {
					t.Fatalf("classification lost: %v", ClassifySendError(se))
				}
			} else if se != nil {
				t.Fatalf("expected send to pass, got %v", se)
			}
		})
	}
}

func TestReachoutTimelockPersistsAcrossRestart(t *testing.T) {
	store := newReachoutTestStore(t)
	t.Setenv("REACHOUT_TIMELOCK_DEFAULT", "2h")

	c := newOfflineClient(store)
	before := time.Now()
	until := c.recordReachoutTimelock(types.NewJID("4915199999999", types.DefaultUserServer))
	if d := until.Sub(before); d < 2*time.Hour-time.Minute || d > 2*time.Hour+time.Minute {
		t.Fatalf("lock duration %v, want ~2h from REACHOUT_TIMELOCK_DEFAULT", d)
	}

	// A fresh client on the same store (a restart) restores the lock.
	restarted := newOfflineClient(store)
	if got := restarted.ReachoutTimelockUntil(); got.Unix() != until.Unix() {
		t.Fatalf("restored lock %v, want %v", got, until)
	}

	// A second 463 never shortens an existing, longer lock.
	longer := time.Now().Add(48 * time.Hour)
	restarted.setReachoutTimelock(longer)
	if got := restarted.recordReachoutTimelock(types.NewJID("1", types.DefaultUserServer)); got.Unix() != longer.Unix() {
		t.Fatalf("lock shortened to %v, want %v", got, longer)
	}
}

func TestReachoutNotification(t *testing.T) {
	store := newReachoutTestStore(t)
	c := newOfflineClient(store)

	ends := time.Now().Add(6 * time.Hour).Truncate(time.Second)
	active := &events.NotifyAccountReachoutTimelock{IsActive: true, EnforcementType: "REACHOUT_TIMELOCK"}
	active.TimeEnforcementEnds.Time = ends
	c.handleReachoutNotification(active)
	if got := c.ReachoutTimelockUntil(); !got.Equal(ends) {
		t.Fatalf("lock from notification %v, want %v", got, ends)
	}
	if persisted, _ := store.GetBridgeStateTime(reachoutStateKey); !persisted.Equal(ends) {
		t.Fatalf("persisted %v, want %v", persisted, ends)
	}

	c.handleReachoutNotification(&events.NotifyAccountReachoutTimelock{IsActive: false})
	if got := c.ReachoutTimelockUntil(); !got.IsZero() {
		t.Fatalf("lock not cleared: %v", got)
	}
	if persisted, _ := store.GetBridgeStateTime(reachoutStateKey); !persisted.IsZero() {
		t.Fatalf("persisted lock not cleared: %v", persisted)
	}
}

func TestClassifySendError(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want SendErrorKind
	}{
		{"463", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 463), SendErrReachoutTimelock},
		{"wrapped 463", fmt.Errorf("Error sending message: %w", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 463)), SendErrReachoutTimelock},
		{"other server error", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 479), SendErrUpstream},
		{"4630 is not 463", fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 4630), SendErrUpstream},
		{"deadline", context.DeadlineExceeded, SendErrTimeout},
		{"cancelled", context.Canceled, SendErrTimeout},
		{"ack timeout", whatsmeow.ErrMessageTimedOut, SendErrTimeout},
		{"socket down", whatsmeow.ErrNotConnected, SendErrNotConnected},
		{"logged out", whatsmeow.ErrNotLoggedIn, SendErrNotConnected},
		{"antiban paused", antiban.ErrHealthPaused, SendErrRateLimited},
		{"warm-up cap", antiban.ErrWarmUpLimitReached, SendErrRateLimited},
		{"typed wins", &SendError{Kind: SendErrForbidden, Err: errors.New("x")}, SendErrForbidden},
		{"unknown", errors.New("boom"), SendErrUpstream},
		{"nil", nil, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ClassifySendError(tt.err); got != tt.want {
				t.Fatalf("ClassifySendError(%v) = %q, want %q", tt.err, got, tt.want)
			}
		})
	}
}

// A 463 on a send addressed straight to a LID usually means the server can't
// route that LID (no phone mapping yet), not a reachout timelock. Arming the
// lock there would block every new contact for a day over one bad address.
func TestSendFailure463OnlyArmsLockForPhoneRecipients(t *testing.T) {
	err463 := fmt.Errorf("%w %d", whatsmeow.ErrServerReturnedError, 463)
	tests := []struct {
		name     string
		to       types.JID
		wantLock bool
		wantKind SendErrorKind
	}{
		{"phone recipient arms the lock", types.NewJID("4915199999999", types.DefaultUserServer), true, SendErrReachoutTimelock},
		{"LID recipient does not", types.NewJID("123456789012345", types.HiddenUserServer), false, SendErrUpstream},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := newOfflineClient(newReachoutTestStore(t))
			se := c.sendFailure(tt.to, antiban.Text, err463, "send: %w")
			if se == nil || se.Kind != tt.wantKind {
				t.Fatalf("kind = %v, want %v", se, tt.wantKind)
			}
			if locked := !c.ReachoutTimelockUntil().IsZero(); locked != tt.wantLock {
				t.Fatalf("lock armed = %v, want %v", locked, tt.wantLock)
			}
		})
	}
}
