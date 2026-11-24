//go:build !windows

package database

import (
	"os"
	"syscall"
	"testing"
)

// A fresh store directory and database are owner-only; running again leaves
// them alone.
func TestNewMessageStoreCreatesPrivateStore(t *testing.T) {
	old := syscall.Umask(0o022)
	defer syscall.Umask(old)
	t.Chdir(t.TempDir())
	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore: %v", err)
	}
	defer func() { _ = store.Close() }()
	if fi, err := os.Stat("store"); err != nil || fi.Mode().Perm() != 0o700 {
		t.Fatalf("store dir: %v %v", fi, err)
	}
	for _, f := range []string{"store/messages.db", "store/messages.db-wal", "store/messages.db-shm"} {
		fi, err := os.Stat(f)
		if err != nil {
			continue // sidecars exist only while the WAL is open
		}
		if fi.Mode().Perm() != 0o600 {
			t.Errorf("%s mode = %o, want 600", f, fi.Mode().Perm())
		}
	}
}
