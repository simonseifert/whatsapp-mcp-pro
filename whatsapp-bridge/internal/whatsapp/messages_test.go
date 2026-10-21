package whatsapp

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
)

func TestValidateMediaPath(t *testing.T) {
	tests := []struct {
		name        string
		path        string
		wantErr     bool
		errContains string
	}{
		{"empty path", "", false, ""},
		{"allowed /app/media", "/app/media/file.jpg", false, ""},
		{"allowed /app/store media", "/app/store/media/photo.jpg", false, ""},
		// The bridge's own session DB lives under store/; sending it to a chat
		// would hand over the linked-device keys.
		{"store session db", "/app/store/whatsapp.db", true, "sensitive"},
		{"store messages db wal", "/app/store/messages.db-wal", true, "sensitive"},
		{"nested store db", "/tmp/x/store/messages.db", true, "sensitive"},
		{"db anywhere", "/tmp/export/report.db", true, "database"},
		{"db journal", "/tmp/app.db-journal", true, "database"},
		{"db shm", "/tmp/app.db-shm", true, "database"},
		{"sqlite", "/tmp/history.sqlite", true, "sensitive"},
		{"sqlite3", "/tmp/History.SQLITE3", true, "sensitive"},
		{"sqlite wal", "/tmp/cache.sqlite-wal", true, "sensitive"},
		// Only subfolders of store/ hold media; its top level is bridge state.
		{"file directly in store", "/app/store/pair-qr.txt", true, "store directory"},
		{"image directly in store", "/app/store/photo.jpg", true, "store directory"},
		{"nested store top level", "/tmp/data/store/pair-qr.txt", true, "store directory"},
		{"auto-download jid dir", "/app/store/123_s.whatsapp.net/image_20261021_120000.jpg", false, ""},
		{"downloaded media dir", "/tmp/data/store/media/123_s.whatsapp.net/3EB0.jpg", false, ""},
		{"dotfile .env", "/tmp/.env", true, "hidden"},
		{"dot dir .ssh", "/tmp/.ssh/known_hosts", true, "hidden"},
		{"dot dir .git", "/tmp/repo/.git/config", true, "hidden"},
		{"dot dir .config", "/tmp/.config/app/settings.yaml", true, "hidden"},
		{"prod.env", "/tmp/prod.env", true, "sensitive"},
		{"pem", "/tmp/cert.pem", true, "sensitive"},
		{"key upper case", "/tmp/Server.KEY", true, "sensitive"},
		{"p12", "/tmp/bundle.p12", true, "sensitive"},
		{"id_rsa", "/tmp/id_rsa", true, "sensitive"},
		{"id_ed25519.pub", "/tmp/id_ed25519.pub", true, "sensitive"},
		{"config.json", "/tmp/config.json", true, "sensitive"},
		{"credentials json", "/tmp/credentials-prod.json", true, "sensitive"},
		{"secrets yaml", "/tmp/secrets.yaml", true, "sensitive"},
		{"kdbx", "/tmp/vault.kdbx", true, "sensitive"},
		{"similar name ok", "/tmp/keynote.pdf", false, ""},
		{"env in middle ok", "/tmp/environment.png", false, ""},
		{"allowed /tmp", "/tmp/upload.png", false, ""},
		{"path traversal ../", "/app/media/../etc/passwd", true, "traversal"},
		{"path traversal multiple", "/app/media/../../etc/passwd", true, "traversal"},
		{"path traversal in middle", "/app/../../../etc/passwd", true, "traversal"},
		{"outside allowed /etc", "/etc/passwd", true, "outside allowed"},
		{"outside allowed /home", "/home/user/file.txt", true, "outside allowed"},
		{"outside allowed /var", "/var/log/syslog", true, "outside allowed"},
		// Sibling directory that merely shares a name prefix with /tmp must not
		// pass: a raw strings.HasPrefix(absPath, "/tmp") would wrongly allow it.
		{"sibling prefix of /tmp", "/tmpfoo/file.jpg", true, "outside allowed"},
	}

	_ = os.Unsetenv("DISABLE_PATH_CHECK")

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateMediaPath(tt.path)

			if tt.wantErr {
				if err == nil {
					t.Errorf("validateMediaPath(%s) = nil, want error containing %q", tt.path, tt.errContains)
					return
				}
				if tt.errContains != "" && !strings.Contains(strings.ToLower(err.Error()), strings.ToLower(tt.errContains)) {
					t.Errorf("validateMediaPath(%s) error = %v, want error containing %q", tt.path, err, tt.errContains)
				}
				return
			}

			if err != nil {
				t.Errorf("validateMediaPath(%s) = %v, want nil", tt.path, err)
			}
		})
	}
}

func TestValidateMediaPath_SymlinkEscape(t *testing.T) {
	_ = os.Unsetenv("DISABLE_PATH_CHECK")

	// A directory inside the allowlist (/tmp) containing a symlink that points
	// outside it. os.ReadFile would follow the link, so validation must resolve
	// it and reject — otherwise the link's own path (/tmp/...) passes the check
	// while the bytes read come from the target.
	dir, err := os.MkdirTemp("/tmp", "wamcp_symlink_")
	if err != nil {
		t.Skipf("cannot create temp dir under /tmp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })

	// A real file inside the allowed dir must still be accepted.
	realFile := filepath.Join(dir, "real.jpg")
	if err := os.WriteFile(realFile, []byte("x"), 0o600); err != nil {
		t.Fatalf("write real file: %v", err)
	}
	if err := validateMediaPath(realFile); err != nil {
		t.Errorf("real file %s under /tmp should be allowed, got: %v", realFile, err)
	}

	// A symlink to a file outside the allowlist must be rejected.
	link := filepath.Join(dir, "escape.jpg")
	if err := os.Symlink("/etc/passwd", link); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	if err := validateMediaPath(link); err == nil {
		t.Errorf("symlink %s -> /etc/passwd should be rejected, got nil", link)
	}
}

func TestValidateMediaPath_DisableCheck(t *testing.T) {
	original := os.Getenv("DISABLE_PATH_CHECK")
	defer func() { _ = os.Setenv("DISABLE_PATH_CHECK", original) }()

	_ = os.Setenv("DISABLE_PATH_CHECK", "true")

	if err := validateMediaPath("/home/user/file.txt"); err != nil {
		t.Errorf("With DISABLE_PATH_CHECK=true, validateMediaPath should allow external paths, got: %v", err)
	}
}

// DISABLE_PATH_CHECK relaxes the directory allowlist, not secret protection.
func TestValidateMediaPath_SensitiveUnderDisableCheck(t *testing.T) {
	t.Setenv("DISABLE_PATH_CHECK", "true")

	tests := []struct {
		name    string
		path    string
		wantErr bool
	}{
		{"plain file anywhere", "/home/user/photo.jpg", false},
		{"ssh key", "/home/user/.ssh/id_ed25519", true},
		{"dotenv", "/srv/app/.env", true},
		{"session db", "/srv/data/store/whatsapp.db", true},
		{"pem", "/etc/ssl/private/site.pem", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateMediaPath(tt.path)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateMediaPath(%s) = %v, wantErr %v", tt.path, err, tt.wantErr)
			}
		})
	}
}

// The dot rule applies below the allowed root only, so an operator can list a
// data dir such as ~/.local/share/... and still send the media in it.
func TestValidateMediaPath_DotInAllowedRoot(t *testing.T) {
	t.Setenv("DISABLE_PATH_CHECK", "")
	t.Setenv("MEDIA_ALLOWED_DIRS", "/srv/home, /srv/home/.local/share/wa")

	tests := []struct {
		name    string
		path    string
		wantErr bool
	}{
		{"media in dotted data dir", "/srv/home/.local/share/wa/media/a.jpg", false},
		{"dot dir under broad root", "/srv/home/.ssh/notes.txt", true},
		{"dotfile in data dir", "/srv/home/.local/share/wa/.env", true},
		{"session db in data dir", "/srv/home/.local/share/wa/store/whatsapp.db", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateMediaPath(tt.path)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateMediaPath(%s) = %v, wantErr %v", tt.path, err, tt.wantErr)
			}
		})
	}
}

// A symlink inside an allowed dir must be judged by its target too, and a
// sensitive link name must be refused even when the target is harmless.
func TestValidateMediaPath_SymlinkToSensitive(t *testing.T) {
	t.Setenv("DISABLE_PATH_CHECK", "")

	dir, err := os.MkdirTemp("/tmp", "wamcp_sensitive_")
	if err != nil {
		t.Skipf("cannot create temp dir under /tmp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })

	secret := filepath.Join(dir, "prod.env")
	photo := filepath.Join(dir, "photo.jpg")
	for _, f := range []string{secret, photo} {
		if err := os.WriteFile(f, []byte("x"), 0o600); err != nil {
			t.Fatalf("write %s: %v", f, err)
		}
	}
	innocentLink := filepath.Join(dir, "innocent.jpg")
	sensitiveLink := filepath.Join(dir, "id_rsa")
	if err := os.Symlink(secret, innocentLink); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	if err := os.Symlink(photo, sensitiveLink); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}

	tests := []struct {
		name    string
		path    string
		wantErr bool
	}{
		{"real photo", photo, false},
		{"innocent name to secret", innocentLink, true},
		{"secret name to photo", sensitiveLink, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateMediaPath(tt.path)
			if (err != nil) != tt.wantErr {
				t.Errorf("validateMediaPath(%s) = %v, wantErr %v", tt.path, err, tt.wantErr)
			}
		})
	}
}

func TestValidateMediaPath_TraversalAlwaysBlocked(t *testing.T) {
	original := os.Getenv("DISABLE_PATH_CHECK")
	defer func() { _ = os.Setenv("DISABLE_PATH_CHECK", original) }()

	_ = os.Setenv("DISABLE_PATH_CHECK", "true")

	if err := validateMediaPath("/app/media/../../../etc/passwd"); err == nil {
		t.Error("Path traversal should be blocked even with DISABLE_PATH_CHECK=true")
	}
}

func TestMediaTypeAndMimeType(t *testing.T) {
	tests := []struct {
		name          string
		mediaPath     string
		wantMediaType whatsmeow.MediaType
		wantMimeType  string
	}{
		{
			name:          "pdf document",
			mediaPath:     "/tmp/report.pdf",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/pdf",
		},
		{
			name:          "docx document",
			mediaPath:     "/tmp/spec.DOCX",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		},
		{
			name:          "xlsx document",
			mediaPath:     "/tmp/budget.xlsx",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		},
		{
			name:          "cbz document",
			mediaPath:     "/tmp/comic.cbz",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/x-cbz",
		},
		{
			name:          "jpg image",
			mediaPath:     "/tmp/photo.jpg",
			wantMediaType: whatsmeow.MediaImage,
			wantMimeType:  "image/jpeg",
		},
		{
			name:          "unknown extension fallback",
			mediaPath:     "/tmp/archive.weird",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/octet-stream",
		},
		{
			name:          "no extension fallback",
			mediaPath:     "/tmp/readme",
			wantMediaType: whatsmeow.MediaDocument,
			wantMimeType:  "application/octet-stream",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotMediaType, gotMimeType := mediaTypeAndMimeType(tt.mediaPath)
			if gotMediaType != tt.wantMediaType {
				t.Fatalf("mediaTypeAndMimeType() mediaType = %v, want %v", gotMediaType, tt.wantMediaType)
			}
			if gotMimeType != tt.wantMimeType {
				t.Fatalf("mediaTypeAndMimeType() mimeType = %q, want %q", gotMimeType, tt.wantMimeType)
			}
		})
	}
}

func TestDocumentFileName(t *testing.T) {
	tests := []struct {
		name      string
		mediaPath string
		want      string
	}{
		{
			name:      "unix path",
			mediaPath: "/tmp/Presidio-statement-May-2026.pdf",
			want:      "Presidio-statement-May-2026.pdf",
		},
		{
			name:      "windows path",
			mediaPath: `C:\tmp\Quarterly Report.xlsx`,
			want:      "Quarterly Report.xlsx",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := documentFileName(tt.mediaPath)
			if got != tt.want {
				t.Fatalf("documentFileName() = %q, want %q", got, tt.want)
			}
		})
	}
}
