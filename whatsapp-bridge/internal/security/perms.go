package security

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
)

// Modes for what holds session keys and message history. Owner only: the
// session database carries the Signal keys of the linked device.
const (
	PrivateDirMode  fs.FileMode = 0o700
	PrivateFileMode fs.FileMode = 0o600
)

// MkdirPrivate creates dir (and parents) with mode 0700. An existing directory
// is left exactly as it is, so a deployment that chose other permissions keeps
// them.
func MkdirPrivate(dir string) error {
	return os.MkdirAll(dir, PrivateDirMode)
}

// CreatePrivateFile creates path empty with mode 0600 if it does not exist.
// An existing file is not touched. SQLite gives its -wal and -shm files the
// mode of the database, so creating the database first keeps all three
// private; opening it with the driver alone would create it 0644 under the
// usual umask.
func CreatePrivateFile(path string) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, PrivateFileMode)
	if err != nil {
		if errors.Is(err, fs.ErrExist) {
			return nil
		}
		return fmt.Errorf("create %s: %w", path, err)
	}
	return f.Close()
}
