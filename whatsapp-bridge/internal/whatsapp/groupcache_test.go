package whatsapp

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/antiban"
)

func TestGroupCacheJoinedGroups(t *testing.T) {
	errDown := errors.New("iq failed")
	tests := []struct {
		name       string
		ttl        time.Duration
		fetchErr   error
		advance    time.Duration // between the two calls
		invalidate bool
		wantCalls  int
	}{
		{"second call inside the cooldown is served from cache", 5 * time.Minute, nil, time.Minute, false, 1},
		{"after the cooldown it fetches again", 5 * time.Minute, nil, 6 * time.Minute, false, 2},
		{"errors are cached too, so a failing query is not hammered", 5 * time.Minute, errDown, time.Minute, false, 1},
		{"a group event drops the cache", 5 * time.Minute, nil, time.Second, true, 2},
		{"ttl 0 turns the cache off", 0, nil, 0, false, 2},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			now := time.Date(2026, 10, 23, 9, 0, 0, 0, time.UTC)
			g := newGroupCache(tt.ttl)
			g.now = func() time.Time { return now }
			calls := 0
			fetch := func(context.Context) ([]*types.GroupInfo, error) {
				calls++
				if tt.fetchErr != nil {
					return nil, tt.fetchErr
				}
				return []*types.GroupInfo{{JID: types.NewJID("120363000000000001", types.GroupServer)}}, nil
			}

			_, err1 := g.joinedGroups(context.Background(), fetch)
			now = now.Add(tt.advance)
			if tt.invalidate {
				g.handleEvent(&events.GroupInfo{JID: types.NewJID("120363000000000001", types.GroupServer)})
			}
			groups, err2 := g.joinedGroups(context.Background(), fetch)

			if calls != tt.wantCalls {
				t.Fatalf("fetches = %d, want %d", calls, tt.wantCalls)
			}
			if !errors.Is(err1, tt.fetchErr) || !errors.Is(err2, tt.fetchErr) {
				t.Fatalf("errors = %v, %v; want %v", err1, err2, tt.fetchErr)
			}
			if tt.fetchErr == nil && len(groups) != 1 {
				t.Fatalf("groups = %v", groups)
			}
		})
	}
}

func TestGroupCacheGroupInfoPerJID(t *testing.T) {
	now := time.Date(2026, 10, 23, 9, 0, 0, 0, time.UTC)
	g := newGroupCache(5 * time.Minute)
	g.now = func() time.Time { return now }
	calls := map[types.JID]int{}
	fetch := func(_ context.Context, jid types.JID) (*types.GroupInfo, error) {
		calls[jid]++
		return &types.GroupInfo{JID: jid}, nil
	}
	a := types.NewJID("120363000000000001", types.GroupServer)
	b := types.NewJID("120363000000000002", types.GroupServer)

	for range 3 {
		if _, err := g.groupInfo(context.Background(), a, fetch); err != nil {
			t.Fatal(err)
		}
	}
	_, _ = g.groupInfo(context.Background(), b, fetch)
	if calls[a] != 1 || calls[b] != 1 {
		t.Fatalf("calls = %v, want one per group", calls)
	}

	g.handleEvent(&events.JoinedGroup{GroupInfo: types.GroupInfo{JID: a}})
	_, _ = g.groupInfo(context.Background(), a, fetch)
	_, _ = g.groupInfo(context.Background(), b, fetch)
	if calls[a] != 2 || calls[b] != 1 {
		t.Fatalf("after event for a: calls = %v, want only a refetched", calls)
	}
}

// A caller that gave up must not leave a cached error behind for everyone else.
func TestGroupCacheDoesNotCacheCallerCancellation(t *testing.T) {
	g := newGroupCache(5 * time.Minute)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	calls := 0
	fetch := func(ctx context.Context) ([]*types.GroupInfo, error) {
		calls++
		return nil, ctx.Err()
	}
	_, _ = g.joinedGroups(ctx, fetch)
	_, _ = g.joinedGroups(context.Background(), fetch)
	if calls != 2 {
		t.Fatalf("fetches = %d, want 2", calls)
	}
}

func TestAntibanRequestFlagsFirstContact(t *testing.T) {
	store := newReachoutTestStore(t)
	known := "4915112345678@s.whatsapp.net"
	if err := store.StoreChat(known, "Known", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("m1", known, known, "Known", "hi", time.Now(), false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	c := newOfflineClient(store)

	tests := []struct {
		name    string
		msgType antiban.MessageType
		chat    string
		wantNew bool
	}{
		{"text to existing chat", antiban.Text, known, false},
		{"text to unknown number", antiban.Text, "4915112345679@s.whatsapp.net", true},
		{"poll to unknown LID", antiban.Poll, "123456789012345@lid", true},
		{"group is never a first contact", antiban.Text, "120363000000000001@g.us", false},
		{"reaction never counts as first contact", antiban.Reaction, "4915112345679@s.whatsapp.net", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			jid, err := types.ParseJID(tt.chat)
			if err != nil {
				t.Fatal(err)
			}
			req := c.antibanRequest(store, tt.msgType, jid, 0)
			if req.NewRecipient != tt.wantNew || req.Recipient != tt.chat {
				t.Fatalf("req = %+v, want NewRecipient=%v Recipient=%s", req, tt.wantNew, tt.chat)
			}
		})
	}
}

func TestClassifyCapErrorCarriesRetryTime(t *testing.T) {
	se := classify(&antiban.CapError{Cap: antiban.CapNewRecipientsDay, RetryAfter: time.Hour}, "antiban blocked send: %w")
	if se.Kind != SendErrCapReached {
		t.Fatalf("kind = %s", se.Kind)
	}
	if d := time.Until(se.Until); d < 59*time.Minute || d > time.Hour {
		t.Fatalf("Until is %s away, want ~1h", d)
	}
}
