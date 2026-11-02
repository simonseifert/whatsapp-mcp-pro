package whatsapp

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bridge/internal/mediastore"
)

const sweepChat = "123@s.whatsapp.net"

var sweepNow = time.Date(2026, 11, 2, 12, 0, 0, 0, time.UTC)

// sweepFixture stores media messages and counts what the sweep fetches. It
// works in a temp dir, because both media layouts are relative to the store.
type sweepFixture struct {
	*mediaSweep
	fetched []string
	fetchFn func(id string) ([]byte, error)
}

func newSweepFixture(t *testing.T) *sweepFixture {
	t.Helper()
	t.Chdir(t.TempDir())
	cfg := DefaultMediaSweepConfig()
	cfg.Pace = 0
	f := &sweepFixture{fetchFn: func(string) ([]byte, error) { return []byte("bytes"), nil }}
	f.mediaSweep = &mediaSweep{
		store: newQueueTestStore(t), cfg: cfg, logger: waLog.Noop,
		now:       func() time.Time { return sweepNow },
		connected: func() bool { return true },
		fetch: func(_ context.Context, id, _ string) ([]byte, string, string, error) {
			f.fetched = append(f.fetched, id)
			data, err := f.fetchFn(id)
			return data, "image", "", err
		},
	}
	if err := f.store.StoreChat(sweepChat, "", sweepNow); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f *sweepFixture) media(t *testing.T, id, mediaType string, age time.Duration, key []byte) {
	t.Helper()
	if err := f.store.StoreMessage(id, sweepChat, sweepChat, "", "", sweepNow.Add(-age), false,
		mediaType, "", "", "/v/path", key, sum256([]byte("bytes")), []byte("enc"), 5); err != nil {
		t.Fatal(err)
	}
}

func TestSweepSavesOnlyRecentUnsavedMedia(t *testing.T) {
	const day = 24 * time.Hour
	key := []byte("key")
	tests := []struct {
		name      string
		id, kind  string
		age       time.Duration
		key       []byte
		wantFetch bool
	}{
		{"recent media", "RECENT", "image", day, key, true},
		{"older than the window", "OLD", "image", 15 * day, key, false},
		{"no media key", "NOKEY", "image", day, nil, false},
		{"a call is not a file", "CALL", "call", day, key, false},
		{"imported message", "import-abc", "image", day, key, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := newSweepFixture(t)
			f.media(t, tt.id, tt.kind, tt.age, tt.key)
			saved, failed, err := f.run(context.Background())
			if err != nil || failed != 0 {
				t.Fatalf("saved=%d failed=%d err=%v", saved, failed, err)
			}
			if got := len(f.fetched) == 1; got != tt.wantFetch {
				t.Errorf("fetched %v, want fetch=%v", f.fetched, tt.wantFetch)
			}
		})
	}
}

func TestSweepSkipsWhatIsAlreadySaved(t *testing.T) {
	f := newSweepFixture(t)
	f.media(t, "A", "image", time.Hour, []byte("key"))
	if saved, _, _ := f.run(context.Background()); saved != 1 {
		t.Fatalf("first run saved %d, want 1", saved)
	}
	f.fetched = nil
	if saved, _, _ := f.run(context.Background()); saved != 0 || len(f.fetched) != 0 {
		t.Errorf("second run saved %d and fetched %v, want nothing", saved, f.fetched)
	}
}

// A stale file at the expected name must not count as this message's media.
func TestSweepReplacesAFileWithTheWrongHash(t *testing.T) {
	f := newSweepFixture(t)
	f.media(t, "A", "image", time.Hour, []byte("key"))
	p, err := mediastore.Path(sweepChat, "A", "image", "")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("something else"), 0o644); err != nil {
		t.Fatal(err)
	}
	if saved, _, _ := f.run(context.Background()); saved != 1 {
		t.Fatalf("saved %d, want 1", saved)
	}
	if got, _ := os.ReadFile(p); string(got) != "bytes" {
		t.Errorf("file holds %q", got)
	}
}

func TestSweepCarriesOnAfterAFailure(t *testing.T) {
	f := newSweepFixture(t)
	f.fetchFn = func(id string) ([]byte, error) {
		if id == "GONE" {
			return nil, errors.New("403")
		}
		return []byte("bytes"), nil
	}
	f.media(t, "GONE", "image", time.Hour, []byte("key"))
	f.media(t, "OK", "image", 2*time.Hour, []byte("key"))
	saved, failed, err := f.run(context.Background())
	if err != nil || saved != 1 || failed != 1 {
		t.Errorf("saved=%d failed=%d err=%v, want 1, 1, nil", saved, failed, err)
	}
}

func TestSweepRespectsLimitAndConnection(t *testing.T) {
	f := newSweepFixture(t)
	for _, id := range ids(5) {
		f.media(t, id, "image", time.Hour, []byte("key"))
	}
	f.cfg.MaxPerRun = 2
	if saved, _, _ := f.run(context.Background()); saved != 2 {
		t.Errorf("saved %d, want the limit of 2", saved)
	}
	f.fetched = nil
	f.connected = func() bool { return false }
	if _, _, _ = f.run(context.Background()); len(f.fetched) != 0 {
		t.Errorf("fetched %v while disconnected", f.fetched)
	}
}

func TestMediaSweepConfigFromEnv(t *testing.T) {
	tests := []struct {
		name, env string
		wantDays  int
		wantOn    bool
	}{
		{"default", "", 14, true},
		{"custom", "30", 30, true},
		{"zero turns it off", "0", 14, false},
		{"garbage keeps the default", "lots", 14, true},
		{"negative keeps the default", "-1", 14, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("MEDIA_SWEEP_DAYS", tt.env)
			cfg, on := MediaSweepConfigFromEnv()
			if on != tt.wantOn || cfg.MaxAge != time.Duration(tt.wantDays)*24*time.Hour {
				t.Errorf("got %s on=%v", cfg.MaxAge, on)
			}
		})
	}
}
