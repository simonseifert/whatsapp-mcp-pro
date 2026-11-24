package api

import (
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestValidateCDNURL(t *testing.T) {
	t.Setenv("DISABLE_SSRF_CHECK", "")
	tests := []struct {
		url string
		ok  bool
	}{
		{"https://mmg.whatsapp.net/v/t62.7118-24/abc.enc", true},
		{"https://media-fra3-2.cdn.whatsapp.net/x.enc", true},
		{"https://scontent.xx.fbcdn.net/m.enc", true},
		{"http://mmg.whatsapp.net/x.enc", false},             // https only
		{"https://169.254.169.254/latest/meta-data/", false}, // metadata IP
		{"https://internal.service.local/x", false},          // arbitrary host
		{"https://evil-whatsapp.net.attacker.com/x", false},  // suffix spoof
		{"://bad", false},
	}
	for _, tt := range tests {
		err := validateCDNURL(tt.url)
		if tt.ok && err != nil {
			t.Errorf("validateCDNURL(%s) = %v, want nil", tt.url, err)
		}
		if !tt.ok && err == nil {
			t.Errorf("validateCDNURL(%s) = nil, want error", tt.url)
		}
	}

	t.Setenv("DISABLE_SSRF_CHECK", "true")
	if err := validateCDNURL("https://internal.service.local/x"); err != nil {
		t.Errorf("escape hatch DISABLE_SSRF_CHECK=true not honored: %v", err)
	}
}

func TestMediaExtension(t *testing.T) {
	tests := []struct {
		name, mediaType, filename, want string
	}{
		{"image default", "image", "", ".jpg"},
		{"document keeps extension", "document", "Syllabus.pdf", ".pdf"},
		{"document without extension", "document", "README", ".bin"},
		{"hostile extension is sanitised", "document", "x.p/../df", ".bin"},
		{"control bytes in extension", "document", "x.pd\x00f", ".pd_f"},
		{"overlong extension ignored", "document", "x.aaaaaaaaaaaaaaaa", ".bin"},
		{"sticker is webp", "sticker", "", ".webp"},
		{"unknown media type", "gif", "", ".bin"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := mediaExtension(tt.mediaType, tt.filename); got != tt.want {
				t.Errorf("mediaExtension(%q, %q) = %q, want %q", tt.mediaType, tt.filename, got, tt.want)
			}
		})
	}
}

func TestSaveMediaReturnsAbsolutePathInsideStore(t *testing.T) {
	t.Chdir(t.TempDir())
	path, err := saveMedia("120363@g.us", "../../ABC", "document", "notes.pdf", []byte("hello"))
	if err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(path) {
		t.Errorf("path %q is not absolute", path)
	}
	root, _ := filepath.Abs(filepath.Join("store", "media"))
	if !strings.HasPrefix(path, root+string(filepath.Separator)) {
		t.Errorf("path %q escaped %q", path, root)
	}
	if !strings.HasSuffix(path, ".pdf") {
		t.Errorf("path %q lost the document extension", path)
	}
}

// /api/download refuses media whose message declares more than MEDIA_MAX_BYTES
// before it fetches anything (review-bridge #3); the cap is the same constant
// auto-download uses.
func TestDownloadRefusesDeclaredOversize(t *testing.T) {
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	s := newSendTestServer(t, &fakeSender{})
	const chat = "4915100000001@s.whatsapp.net"
	if err := s.messageStore.StoreChat(chat, "c", time.Now()); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		id     string
		length uint64
	}{{"BIG", 5000}} {
		if err := s.messageStore.StoreMessage(tc.id, chat, chat, "c", "", time.Now(), false,
			"document", "x.pdf", "https://mmg.whatsapp.net/x", "/x", []byte("key"), []byte("sha"), []byte("enc"), tc.length); err != nil {
			t.Fatal(err)
		}
	}
	rec := post(t, s.handleDownload, map[string]any{"message_id": "BIG", "chat_jid": chat}, nil)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status %d, want 413: %s", rec.Code, rec.Body)
	}
}
