package antiban

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// writeJSONAtomic writes v to path via a temp file and rename, so a crash
// mid-write leaves the previous copy rather than a truncated one.
func writeJSONAtomic(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}

	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}

	tmpFile, err := os.CreateTemp(dir, ".antiban_state_*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmpFile.Name()
	defer func() {
		// No-op after a successful rename; cleans up on every failure path.
		_ = os.Remove(tmpPath)
	}()

	if _, err := tmpFile.Write(data); err != nil {
		_ = tmpFile.Close()
		return err
	}
	if err := tmpFile.Close(); err != nil {
		return err
	}

	return os.Rename(tmpPath, path)
}

// quarantineStateFile moves an unreadable state file aside instead of letting
// the next save overwrite it, so whoever investigates the loud log line still
// has the bytes. Returns where it went, or "" if the move failed.
func quarantineStateFile(path string, now time.Time) string {
	dest := fmt.Sprintf("%s.corrupt-%s", path, now.UTC().Format("20060102T150405Z"))
	if err := os.Rename(path, dest); err != nil {
		return ""
	}
	return dest
}
