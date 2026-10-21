package mediastore

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSanitizePath(t *testing.T) {
	tests := []struct{ in, want string }{
		{"", "_"},
		{"120363@g.us", "120363@g_us"},
		{"../../etc", "______etc"},
		{".hidden", "_hidden"},
		{"-flag", "_flag"},
		{"3EB0ABC_def-1", "3EB0ABC_def-1"},
	}
	for _, tt := range tests {
		if got := SanitizePath(tt.in); got != tt.want {
			t.Errorf("SanitizePath(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestSaveWritesWherePathPoints(t *testing.T) {
	t.Chdir(t.TempDir())
	want, err := Path("123@s.whatsapp.net", "3EB0X", "image", "")
	if err != nil {
		t.Fatal(err)
	}
	got, err := Save("123@s.whatsapp.net", "3EB0X", "image", "", []byte("jpeg"))
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Errorf("Save wrote %q, Path says %q", got, want)
	}
	if !filepath.IsAbs(got) || !strings.HasSuffix(got, filepath.Join("store", "media", "123@s_whatsapp_net", "3EB0X.jpg")) {
		t.Errorf("unexpected layout: %q", got)
	}
	if b, err := os.ReadFile(got); err != nil || string(b) != "jpeg" {
		t.Errorf("read back %q, %v", b, err)
	}
	if _, err := os.Stat(got + ".tmp"); !os.IsNotExist(err) {
		t.Errorf("temp file left behind: %v", err)
	}
}
