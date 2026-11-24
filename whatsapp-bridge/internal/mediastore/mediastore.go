// Package mediastore owns the store/media/<chat_jid>/<message_id><ext> layout
// that /api/download writes and serves. It is separate from the api package so
// the background media retry loop in internal/whatsapp can save into the very
// same place a later /api/download looks.
//
// This is not the auto-download layout (store/<jid>/<filename>, see
// whatsapp.AutoDownloadPaths), which wa-dispatch and wa-assistant read
// directly and which must not move.
package mediastore

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// DefaultMaxBytes is the largest media file the bridge downloads: 100 MiB.
// WhatsApp documents can be up to 2 GB and whatsmeow buffers a whole download
// in memory (twice, with decryption), so an uncapped download is a way for one
// forwarded file to take the bridge down.
const DefaultMaxBytes int64 = 100 << 20

// MaxBytes is the single size cap shared by auto-download, the media retry
// queue, the sweep and /api/download: MEDIA_MAX_BYTES, or DefaultMaxBytes when
// unset or not a positive integer.
func MaxBytes() int64 {
	if v, err := strconv.ParseInt(strings.TrimSpace(os.Getenv("MEDIA_MAX_BYTES")), 10, 64); err == nil && v > 0 {
		return v
	}
	return DefaultMaxBytes
}

var mediaExt = map[string]string{
	"image":    ".jpg",
	"video":    ".mp4",
	"audio":    ".ogg",
	"sticker":  ".webp",
	"document": ".bin",
}

// Path returns the absolute path media for a message is saved at. Callers
// such as the MCP server run in another working directory, so a relative path
// is useless to them.
func Path(chatJID, messageID, mediaType, filename string) (string, error) {
	dir, err := filepath.Abs(filepath.Join("store", "media", SanitizePath(chatJID)))
	if err != nil {
		return "", fmt.Errorf("resolve media dir: %w", err)
	}
	return filepath.Join(dir, SanitizePath(messageID)+Extension(mediaType, filename)), nil
}

// Save writes decrypted media to Path under the bridge's working directory and
// returns the absolute path.
func Save(chatJID, messageID, mediaType, filename string, data []byte) (string, error) {
	outPath, err := Path(chatJID, messageID, mediaType, filename)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(outPath), 0o755); err != nil {
		return "", fmt.Errorf("mkdir failed: %w", err)
	}
	// Atomic write: write to a sibling tmp file, then rename into place so partial
	// writes never become visible and concurrent requests for the same message
	// can't tear each other's output.
	tmpPath := outPath + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0o644); err != nil {
		return "", fmt.Errorf("write failed: %w", err)
	}
	if err := os.Rename(tmpPath, outPath); err != nil {
		_ = os.Remove(tmpPath)
		return "", fmt.Errorf("rename failed: %w", err)
	}
	return outPath, nil
}

// Extension keeps a document's own extension, because ".bin" makes a PDF
// useless to whoever opens it. The filename is sender-supplied, so the
// extension is sanitised like every other path component.
func Extension(mediaType, filename string) string {
	ext := mediaExt[mediaType]
	if ext == "" {
		ext = ".bin"
	}
	if mediaType == "document" && filename != "" {
		if e := strings.TrimPrefix(filepath.Ext(filename), "."); e != "" && len(e) <= 10 {
			ext = "." + SanitizePath(e)
		}
	}
	return ext
}

// SanitizePath replaces every byte outside a strict allowlist with '_', so untrusted
// IDs can't escape the store dir, hide files via leading dots, or smuggle control bytes.
// WhatsApp message IDs and JIDs are constrained to a subset of these characters.
func SanitizePath(s string) string {
	if s == "" {
		return "_"
	}
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'A' && c <= 'Z',
			c >= 'a' && c <= 'z',
			c >= '0' && c <= '9',
			c == '_', c == '-', c == '@':
			b[i] = c
		default:
			b[i] = '_'
		}
	}
	if b[0] == '.' || b[0] == '-' {
		b[0] = '_'
	}
	return string(b)
}
