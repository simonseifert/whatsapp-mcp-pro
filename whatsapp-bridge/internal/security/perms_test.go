//go:build !windows

package security

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestMkdirPrivateAndCreatePrivateFile(t *testing.T) {
	old := syscall.Umask(0o022) // the common default, which would give 0755 / 0644
	defer syscall.Umask(old)
	root := t.TempDir()

	dir := filepath.Join(root, "store")
	if err := MkdirPrivate(dir); err != nil {
		t.Fatal(err)
	}
	if fi, _ := os.Stat(dir); fi.Mode().Perm() != 0o700 {
		t.Fatalf("new dir mode = %o, want 700", fi.Mode().Perm())
	}

	db := filepath.Join(dir, "whatsapp.db")
	if err := CreatePrivateFile(db); err != nil {
		t.Fatal(err)
	}
	if fi, _ := os.Stat(db); fi.Mode().Perm() != 0o600 {
		t.Fatalf("new file mode = %o, want 600", fi.Mode().Perm())
	}
}

// Existing files and directories are never touched.
func TestPrivateHelpersLeaveExistingAlone(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "store")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(dir, "messages.db")
	if err := os.WriteFile(file, []byte("data"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(file, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := MkdirPrivate(dir); err != nil {
		t.Fatal(err)
	}
	if err := CreatePrivateFile(file); err != nil {
		t.Fatal(err)
	}
	if fi, _ := os.Stat(dir); fi.Mode().Perm() != 0o755 {
		t.Fatalf("existing dir mode changed to %o", fi.Mode().Perm())
	}
	fi, _ := os.Stat(file)
	if fi.Mode().Perm() != 0o644 || fi.Size() != 4 {
		t.Fatalf("existing file changed: mode %o size %d", fi.Mode().Perm(), fi.Size())
	}
}
