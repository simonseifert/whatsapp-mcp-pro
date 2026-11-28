package whatsapp

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// setTempDir points os.TempDir() at dir for the test, on any OS.
func setTempDir(t *testing.T, dir string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Setenv("TMP", dir)
		t.Setenv("TEMP", dir)
		return
	}
	t.Setenv("TMPDIR", dir)
}

func TestValidateMediaPath_PlatformTempDir(t *testing.T) {
	_ = os.Unsetenv("DISABLE_PATH_CHECK")
	t.Setenv("MEDIA_ALLOWED_DIRS", "")

	// A temp root outside /tmp, as on macOS (/var/folders/...) or Windows (%TEMP%).
	base, err := os.MkdirTemp(".", "tmproot_")
	if err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	base, _ = filepath.Abs(base)
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	root := filepath.Join(base, "T")
	sibling := filepath.Join(base, "T-evil")
	for _, d := range []string{root, sibling} {
		if err := os.Mkdir(d, 0o700); err != nil {
			t.Fatalf("mkdir %s: %v", d, err)
		}
	}
	setTempDir(t, root)

	inside := filepath.Join(root, "voice.ogg")
	outside := filepath.Join(sibling, "voice.ogg")
	for _, p := range []string{inside, outside} {
		if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
	if err := validateMediaPath(inside); err != nil {
		t.Errorf("file in os.TempDir() should be allowed, got: %v", err)
	}
	if err := validateMediaPath(outside); err == nil {
		t.Errorf("sibling %s of the temp dir should be refused", outside)
	}
	if err := validateMediaPath(filepath.Join(root, "not-yet.ogg")); err != nil {
		t.Errorf("a file not created yet in the temp dir should be allowed, got: %v", err)
	}
}

func TestValidateMediaPath_TempDirViaSymlink(t *testing.T) {
	_ = os.Unsetenv("DISABLE_PATH_CHECK")
	t.Setenv("MEDIA_ALLOWED_DIRS", "")

	base, err := os.MkdirTemp(".", "tmplink_")
	if err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	base, _ = filepath.Abs(base)
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	real := filepath.Join(base, "real")
	link := filepath.Join(base, "link")
	if err := os.Mkdir(real, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
	// Like macOS: TMPDIR names the link, files resolve to the real directory.
	setTempDir(t, link)
	resolvedFile := filepath.Join(real, "a.ogg")
	if err := os.WriteFile(resolvedFile, []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := validateMediaPath(resolvedFile); err != nil {
		t.Errorf("resolved form of the temp dir should be allowed, got: %v", err)
	}
	if err := validateMediaPath(filepath.Join(link, "a.ogg")); err != nil {
		t.Errorf("literal form of the temp dir should be allowed, got: %v", err)
	}
}

func TestPlatformTempDirs_KeepsTmpOnPosix(t *testing.T) {
	setTempDir(t, t.TempDir())
	dirs := platformTempDirs()
	has := func(want string) bool {
		for _, d := range dirs {
			if d == want {
				return true
			}
		}
		return false
	}
	if runtime.GOOS != "windows" && !has("/tmp") {
		t.Errorf("/tmp must stay allowed on POSIX even when TMPDIR differs, got %v", dirs)
	}
	if runtime.GOOS == "windows" && has("/tmp") {
		t.Errorf("/tmp is not a Windows path, got %v", dirs)
	}
	if !has(os.TempDir()) {
		t.Errorf("os.TempDir() %q missing from %v", os.TempDir(), dirs)
	}
}
