package whatsapp

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func TestIsMediaGone(t *testing.T) {
	for _, err := range []error{whatsmeow.ErrMediaDownloadFailedWith403, whatsmeow.ErrMediaDownloadFailedWith404, whatsmeow.ErrMediaDownloadFailedWith410} {
		if !isMediaGone(fmt.Errorf("wrapped: %w", err)) {
			t.Errorf("isMediaGone(%v) = false, want true", err)
		}
	}
	if isMediaGone(errors.New("timeout")) || isMediaGone(nil) {
		t.Error("unrelated errors must not trigger a media retry")
	}
}

func TestHandleMediaRetryWakesWaiterOnce(t *testing.T) {
	id := types.MessageID("3EB0TEST")
	ch := make(chan *events.MediaRetry, 1)
	mediaRetryMu.Lock()
	mediaRetryWaiters[id] = append(mediaRetryWaiters[id], ch)
	mediaRetryMu.Unlock()

	c := &Client{}
	c.HandleMediaRetry(&events.MediaRetry{MessageID: id})
	c.HandleMediaRetry(&events.MediaRetry{MessageID: id}) // no waiter left: dropped

	if got := len(ch); got != 1 {
		t.Fatalf("waiter got %d events, want 1", got)
	}
	mediaRetryMu.Lock()
	_, left := mediaRetryWaiters[id]
	mediaRetryMu.Unlock()
	if left {
		t.Error("waiter entry not cleared after delivery")
	}
}

func TestSetDirectPathClearsStaleURL(t *testing.T) {
	url := "https://mmg.whatsapp.net/old"
	msg := &waE2E.ImageMessage{URL: &url}
	if !setDirectPath(msg, "/v/t62/new") {
		t.Fatal("setDirectPath refused an image")
	}
	if msg.GetDirectPath() != "/v/t62/new" || msg.GetURL() != "" {
		t.Errorf("got path=%q url=%q", msg.GetDirectPath(), msg.GetURL())
	}
}

func sum256(b []byte) []byte {
	s := sha256.Sum256(b)
	return s[:]
}

func TestFileMatchesSHA256(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "image_20261021_120000.jpg")
	if err := os.WriteFile(path, []byte("message A"), 0o644); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name    string
		path    string
		want    []byte
		match   bool
		wantErr bool
	}{
		{"same content", path, sum256([]byte("message A")), true, false},
		{"other message's hash", path, sum256([]byte("message B")), false, false},
		{"unknown hash trusts file", path, nil, true, false},
		{"missing file", filepath.Join(dir, "nope.jpg"), sum256([]byte("x")), false, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := FileMatchesSHA256(tt.path, tt.want)
			if (err != nil) != tt.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tt.wantErr)
			}
			if got != tt.match {
				t.Errorf("match = %v, want %v", got, tt.match)
			}
		})
	}
}

func TestAutoDownloadPathsKeepsPrimaryLayout(t *testing.T) {
	paths, err := AutoDownloadPaths("123@s.whatsapp.net", "3EB0ABC", "image_20261021_120000.jpg")
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join("store", "123_s.whatsapp.net", "image_20261021_120000.jpg"); paths[0] != want {
		t.Errorf("primary = %q, want %q", paths[0], want)
	}
	if want := filepath.Join("store", "123_s.whatsapp.net", "3EB0ABC_image_20261021_120000.jpg"); paths[1] != want {
		t.Errorf("alt = %q, want %q", paths[1], want)
	}
}

// Two messages that arrived in the same second share a filename. The second
// must land on its ID-prefixed name, and each must be found by its own hash.
func TestSaveAutoDownloadCollision(t *testing.T) {
	dir := t.TempDir()
	name := "image_20261021_120000.jpg"
	pathsA := []string{filepath.Join(dir, name), filepath.Join(dir, "A_"+name)}
	pathsB := []string{filepath.Join(dir, name), filepath.Join(dir, "B_"+name)}
	dataA, dataB := []byte("message A"), []byte("message B")

	saves := []struct {
		name     string
		paths    []string
		data     []byte
		wantPath string
	}{
		{"first message takes plain name", pathsA, dataA, pathsA[0]},
		{"duplicate event keeps plain name", pathsA, dataA, pathsA[0]},
		{"colliding message goes to id name", pathsB, dataB, pathsB[1]},
	}
	for _, tt := range saves {
		t.Run(tt.name, func(t *testing.T) {
			got, err := saveAutoDownload(tt.paths, tt.data, sum256(tt.data))
			if err != nil {
				t.Fatalf("saveAutoDownload: %v", err)
			}
			if got != tt.wantPath {
				t.Errorf("saved to %q, want %q", got, tt.wantPath)
			}
		})
	}

	lookups := []struct {
		name  string
		paths []string
		sha   []byte
		want  string
	}{
		{"A finds plain name", pathsA, sum256(dataA), pathsA[0]},
		{"B skips A's file", pathsB, sum256(dataB), pathsB[1]},
		{"unknown content finds nothing", pathsB, sum256([]byte("C")), ""},
	}
	for _, tt := range lookups {
		t.Run(tt.name, func(t *testing.T) {
			if got := FindAutoDownloaded(tt.paths, tt.sha); got != tt.want {
				t.Errorf("FindAutoDownloaded = %q, want %q", got, tt.want)
			}
		})
	}

	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".autodl-") {
			t.Errorf("temp file %s left behind", e.Name())
		}
	}
}

// A sender picks the document filename. ".." must not climb out of the chat
// directory and a dotfile name must not land as a hidden file.
func TestAutoDownloadPathsRejectsDotNames(t *testing.T) {
	chatDir := filepath.Join("store", "123_s.whatsapp.net")
	tests := []struct {
		name, msgID, filename string
		wantBase              string // "" = random fallback, only checked for safety
	}{
		{"dotdot", "MSG1", "..", "MSG1.bin"},
		{"dot", "MSG1", ".", "MSG1.bin"},
		{"empty", "MSG1", "", "MSG1.bin"},
		{"dotfile", "MSG1", ".env", "MSG1.bin"},
		// Sanitises to ".._.._etc_passwd": leading dot, so it falls back.
		{"traversal chars", "MSG1", "../../etc/passwd", "MSG1.bin"},
		{"inner dots kept", "MSG1", "report.v2.pdf", "report.v2.pdf"},
		{"dotdot id and name", "..", "..", ""},
		{"dot id empty name", ".", "", ""},
		{"dotfile id and name", ".x", ".bashrc", ""},
		{"bad id good name", "..", "a.jpg", "a.jpg"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths, err := AutoDownloadPaths("123@s.whatsapp.net", tt.msgID, tt.filename)
			if err != nil {
				t.Fatalf("AutoDownloadPaths: %v", err)
			}
			for _, p := range paths {
				b := filepath.Base(p)
				if filepath.Dir(p) != chatDir || strings.HasPrefix(b, ".") {
					t.Fatalf("%q is not a plain file in %q", p, chatDir)
				}
			}
			if tt.wantBase != "" && filepath.Base(paths[0]) != tt.wantBase {
				t.Errorf("primary base = %q, want %q", filepath.Base(paths[0]), tt.wantBase)
			}
		})
	}
}

// An unusable chat JID would put files directly in store/, next to the
// session DB; that must be an error, not a fallback.
func TestAutoDownloadPathsRejectsBadChat(t *testing.T) {
	for _, chat := range []string{"", ".", "..", ".hidden"} {
		if paths, err := AutoDownloadPaths(chat, "MSG1", "a.jpg"); err == nil {
			t.Errorf("chat %q: got %v, want error", chat, paths)
		}
	}
}

// A symlink or directory at the primary name is not another message's media,
// so it must not push the download onto the alt name.
func TestSaveAutoDownloadPrimaryNotRegular(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target.jpg")
	if err := os.WriteFile(target, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name  string
		plant func(primary string) error
	}{
		{"symlink", func(p string) error { return os.Symlink(target, p) }},
		{"directory", func(p string) error { return os.Mkdir(p, 0o755) }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []string{filepath.Join(dir, tt.name+".jpg"), filepath.Join(dir, "B_"+tt.name+".jpg")}
			if err := tt.plant(paths[0]); err != nil {
				t.Skipf("cannot plant %s: %v", tt.name, err)
			}
			if _, err := saveAutoDownload(paths, []byte("message B"), sum256([]byte("message B"))); err == nil {
				t.Fatal("expected an error")
			}
			if _, err := os.Lstat(paths[1]); !os.IsNotExist(err) {
				t.Errorf("alt %s was written", paths[1])
			}
		})
	}
}

// The ID-prefixed name must never overwrite a file that is already there.
func TestSaveAutoDownloadDoesNotClobberAlt(t *testing.T) {
	dir := t.TempDir()
	paths := []string{filepath.Join(dir, "a.jpg"), filepath.Join(dir, "B_a.jpg")}
	if err := os.WriteFile(paths[0], []byte("someone else"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths[1], []byte("also someone else"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := saveAutoDownload(paths, []byte("message B"), sum256([]byte("message B"))); err == nil {
		t.Fatal("expected an error rather than overwriting the existing alt file")
	}
	if got, _ := os.ReadFile(paths[1]); string(got) != "also someone else" {
		t.Errorf("alt file was overwritten: %q", got)
	}
}
