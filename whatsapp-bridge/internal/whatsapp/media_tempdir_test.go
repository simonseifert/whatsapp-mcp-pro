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
	if runtime.GOOS == "linux" {
		t.Skip("linux allows exactly /tmp, not os.TempDir(); see TestValidateMediaPath_LinuxIgnoresTMPDIR")
	}
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
	if runtime.GOOS == "linux" {
		t.Skip("linux allows exactly /tmp, not os.TempDir(); see TestValidateMediaPath_LinuxIgnoresTMPDIR")
	}
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

func envOf(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestPlatformTempDirs_LinuxIsExactlyTmp(t *testing.T) {
	got := platformTempDirs("linux", "/var/tmp/x", envOf(map[string]string{"TMP": "/elsewhere"}))
	if len(got) != 1 || got[0] != "/tmp" {
		t.Errorf("linux must allow exactly /tmp even when TMPDIR differs, got %v", got)
	}
}

func TestPlatformTempDirs_Darwin(t *testing.T) {
	got := platformTempDirs("darwin", "/var/folders/ab/T/", envOf(nil))
	want := []string{"/tmp", "/var/folders/ab/T/"}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("got %v want %v", got, want)
	}
	if got := platformTempDirs("darwin", "relative/T", envOf(nil)); len(got) != 1 || got[0] != "/tmp" {
		t.Errorf("a relative os.TempDir() must be ignored, got %v", got)
	}
}

func TestPlatformTempDirs_Windows(t *testing.T) {
	env := envOf(map[string]string{"TMP": `C:\Users\Me\AppData\Local\Temp`, "TEMP": `D:\scratch`})
	got := platformTempDirs("windows", `c:\users\me\appdata\local\temp`, env)
	if len(got) != 2 {
		t.Fatalf("want os.TempDir (deduped with TMP) plus TEMP, got %v", got)
	}
	for _, d := range got {
		if d == "/tmp" {
			t.Errorf("/tmp is not a Windows path: %v", got)
		}
	}
	if got := platformTempDirs("windows", "", envOf(map[string]string{"TMP": "rel"})); len(got) != 0 {
		t.Errorf("non-absolute values must be ignored, got %v", got)
	}
}

func TestPathInsideFor(t *testing.T) {
	tests := []struct {
		goos, p, root string
		want          bool
	}{
		{"windows", `C:\Users\ME\AppData\Local\Temp\a.ogg`, `c:\users\me\appdata\local\temp`, true},
		{"windows", `c:/users/me/appdata/local/temp/a.ogg`, `C:\Users\Me\AppData\Local\Temp\`, true},
		{"windows", `C:\Users\Me\AppData\Local\Temp`, `C:\Users\Me\AppData\Local\Temp`, true},
		{"windows", `C:\Users\Me\AppData\Local\Temp-evil\a.ogg`, `C:\Users\Me\AppData\Local\Temp`, false},
		{"windows", `D:\Users\Me\AppData\Local\Temp\a.ogg`, `C:\Users\Me\AppData\Local\Temp`, false},
		{"linux", "/tmp/a.ogg", "/tmp", true},
		{"linux", "/TMP/a.ogg", "/tmp", false}, // case matters off Windows
		{"linux", "/tmpfoo/a.ogg", "/tmp", false},
	}
	for _, tt := range tests {
		if got := pathInsideFor(tt.goos, tt.p, tt.root); got != tt.want {
			t.Errorf("pathInsideFor(%s, %q, %q) = %v, want %v", tt.goos, tt.p, tt.root, got, tt.want)
		}
	}
}

func TestValidateMediaPath_LinuxIgnoresTMPDIR(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("linux only")
	}
	_ = os.Unsetenv("DISABLE_PATH_CHECK")
	t.Setenv("MEDIA_ALLOWED_DIRS", "")
	base, err := os.MkdirTemp(".", "tmpdir_")
	if err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	base, _ = filepath.Abs(base)
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	f := filepath.Join(base, "a.ogg")
	if err := os.WriteFile(f, []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	setTempDir(t, base)
	if err := validateMediaPath(f); err == nil {
		t.Errorf("TMPDIR must not widen the linux allowlist, but %s was allowed", f)
	}
}
