package whatsapp

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"

	"whatsapp-bridge/internal/mediastore"
)

// The media size cap (review-bridge #3): one constant, from MEDIA_MAX_BYTES.
func TestMediaMaxBytes(t *testing.T) {
	tests := []struct {
		name string
		env  string
		want int64
	}{
		{"default is 100 MiB", "", 100 << 20},
		{"override", "1048576", 1 << 20},
		{"junk falls back", "lots", 100 << 20},
		{"zero falls back", "0", 100 << 20},
		{"negative falls back", "-5", 100 << 20},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("MEDIA_MAX_BYTES", tt.env)
			if got := mediastore.MaxBytes(); got != tt.want {
				t.Fatalf("MaxBytes = %d, want %d", got, tt.want)
			}
		})
	}
}

func TestAutoDownloadRefusal(t *testing.T) {
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	tests := []struct {
		name      string
		mediaType string
		declared  uint64
		refused   bool
	}{
		{"small image", "image", 999, false},
		{"exactly the cap", "document", 1000, false},
		{"one byte over", "document", 1001, true},
		{"huge video", "video", 2 << 30, true},
		{"document without a declared length", "document", 0, true},
		{"video without a declared length", "video", 0, true},
		{"voice note without a declared length", "audio", 0, false},
		{"image without a declared length", "image", 0, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := autoDownloadRefusal(tt.mediaType, tt.declared) != ""; got != tt.refused {
				t.Fatalf("autoDownloadRefusal(%s, %d) refused = %v, want %v", tt.mediaType, tt.declared, got, tt.refused)
			}
		})
	}
}

func TestCheckMediaSize(t *testing.T) {
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	if err := CheckMediaSize(1000); err != nil {
		t.Fatalf("at the cap: %v", err)
	}
	if err := CheckMediaSize(0); err != nil {
		t.Fatalf("undeclared: %v", err)
	}
	if err := CheckMediaSize(1001); !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("over the cap: %v, want ErrMediaTooLarge", err)
	}
}

// At most MEDIA_DOWNLOAD_CONCURRENCY downloads hold a slot at once.
func TestMediaSlotsBoundConcurrency(t *testing.T) {
	t.Setenv("MEDIA_DOWNLOAD_CONCURRENCY", "2")
	mediaSlotsOnce = sync.Once{}
	t.Cleanup(func() { mediaSlotsOnce = sync.Once{} })

	var running, peak int32
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			release, err := acquireMediaSlot(context.Background())
			if err != nil {
				t.Error(err)
				return
			}
			defer release()
			n := atomic.AddInt32(&running, 1)
			for {
				p := atomic.LoadInt32(&peak)
				if n <= p || atomic.CompareAndSwapInt32(&peak, p, n) {
					break
				}
			}
			time.Sleep(20 * time.Millisecond)
			atomic.AddInt32(&running, -1)
		}()
	}
	wg.Wait()
	if peak != 2 {
		t.Fatalf("peak concurrent downloads = %d, want 2", peak)
	}

	// A waiter gives up when its context ends instead of queuing forever.
	r1, _ := acquireMediaSlot(context.Background())
	r2, _ := acquireMediaSlot(context.Background())
	defer r1()
	defer r2()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := acquireMediaSlot(ctx); err == nil {
		t.Fatal("acquired a third slot with two held")
	}
}

// downloadMedia must call the underlying downloader exactly once, hold one
// slot while it runs and release it afterwards.
func TestDownloadMediaCallsDownloaderOnceAndReleasesSlot(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("MEDIA_DOWNLOAD_CONCURRENCY", "1")
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	mediaSlotsOnce = sync.Once{}
	t.Cleanup(func() { mediaSlotsOnce = sync.Once{} })

	var calls int32
	c := &Client{rawDownloadToFile: func(ctx context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		atomic.AddInt32(&calls, 1)
		// The only slot is ours while we run.
		short, cancel := context.WithTimeout(ctx, 10*time.Millisecond)
		defer cancel()
		if _, err := acquireMediaSlot(short); err == nil {
			t.Error("slot was free during the download")
		}
		_, err := f.Write([]byte("data"))
		return err
	}}
	size := uint64(10)
	data, err := c.downloadMedia(context.Background(), &waE2E.DocumentMessage{FileLength: &size})
	if err != nil || string(data) != "data" {
		t.Fatalf("download = %q, %v", data, err)
	}
	if n := atomic.LoadInt32(&calls); n != 1 {
		t.Fatalf("underlying downloads = %d, want 1", n)
	}
	release, err := acquireMediaSlot(context.Background())
	if err != nil {
		t.Fatalf("slot not released: %v", err)
	}
	release()

	// Over the declared cap: refused without calling the downloader.
	big := uint64(5000)
	if _, err := c.downloadMedia(context.Background(), &waE2E.DocumentMessage{FileLength: &big}); !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("oversize err = %v", err)
	}
	if n := atomic.LoadInt32(&calls); n != 1 {
		t.Fatalf("downloader called for an oversize file (%d calls)", n)
	}
}

// A sender can declare 10 bytes and serve far more. The cap is enforced on the
// bytes as they are written: the download aborts and no partial file is left.
func TestDownloadToTempEnforcesCapWhateverIsDeclared(t *testing.T) {
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	tests := []struct {
		name    string
		chunks  []int // sizes of successive writes
		wantErr bool
	}{
		{"within the cap", []int{400, 400}, false},
		{"exactly the cap", []int{1000}, false},
		{"one chunk over", []int{1001 + mediaWireOverhead}, true},
		{"many small writes cross the cap", []int{300, 300, 300, 300, 300}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			lies := uint64(10)
			wrote := 0
			c := &Client{rawDownloadToFile: func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
				for _, n := range tt.chunks {
					if _, err := f.Write(make([]byte, n)); err != nil {
						return err
					}
					wrote += n
				}
				return nil
			}}
			path, err := c.downloadToTemp(context.Background(), &waE2E.DocumentMessage{FileLength: &lies}, dir)
			if tt.wantErr {
				if !errors.Is(err, ErrMediaTooLarge) {
					t.Fatalf("err = %v, want ErrMediaTooLarge", err)
				}
				if entries, _ := os.ReadDir(dir); len(entries) != 0 {
					t.Fatalf("partial file left behind: %v", entries)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			info, err := os.Stat(path)
			if err != nil || info.Size() != int64(wrote) {
				t.Fatalf("temp file: %v %v", info, err)
			}
			// Windows has no POSIX mode bits; the file relies on the temp dir's ACL.
			if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
				t.Fatalf("temp file mode = %o, want 600", info.Mode().Perm())
			}
		})
	}
}

// io.Copy into the capped file must not bypass the limit through ReadFrom.
func TestCappedFileCopyCannotBypassLimit(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "x")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	capped := &cappedFile{File: f, limit: 100}
	if _, err := io.Copy(capped, strings.NewReader(strings.Repeat("a", 500))); !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("io.Copy err = %v, want ErrMediaTooLarge", err)
	}
	if info, _ := f.Stat(); info.Size() > 100 {
		t.Fatalf("file grew to %d bytes", info.Size())
	}
}

// Only the capped writer may stop an oversize stream: this fake undoes its own
// damage afterwards (truncates and returns nil), so the post-download size
// check sees a small file and cannot catch it. Remove the stream-time cap and
// this test fails.
func TestStreamTimeCapAloneStopsOversizeDownload(t *testing.T) {
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	dir := t.TempDir()
	lies := uint64(10)
	c := &Client{rawDownloadToFile: func(_ context.Context, _ whatsmeow.DownloadableMessage, f whatsmeow.File) error {
		if _, err := f.Write(make([]byte, 5000)); err != nil {
			return err // the capped writer stopped it
		}
		if err := f.Truncate(10); err != nil {
			return err
		}
		return nil // looks like a clean, small download to anything after the fact
	}}
	if _, err := c.downloadToTemp(context.Background(), &waE2E.DocumentMessage{FileLength: &lies}, dir); !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("err = %v, want ErrMediaTooLarge from the writer", err)
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatalf("partial file left behind: %v", entries)
	}
}

func TestCleanStaleDownloadTemps(t *testing.T) {
	root := t.TempDir()
	chat := filepath.Join(root, "chat")
	if err := os.MkdirAll(chat, 0o755); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-2 * time.Hour)
	mk := func(path string, mod time.Time) {
		t.Helper()
		if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(path, mod, mod); err != nil {
			t.Fatal(err)
		}
	}
	mk(filepath.Join(root, ".dl-old"), old)
	mk(filepath.Join(chat, ".autodl-old"), old)
	mk(filepath.Join(root, ".dl-fresh"), time.Now())
	mk(filepath.Join(chat, "report.pdf"), old) // real media is never touched
	mk(filepath.Join(chat, ".hidden-other"), old)
	if n := CleanStaleDownloadTemps(root, time.Hour); n != 2 {
		t.Fatalf("removed %d, want 2", n)
	}
	for _, keep := range []string{".dl-fresh"} {
		if _, err := os.Stat(filepath.Join(root, keep)); err != nil {
			t.Errorf("%s was removed", keep)
		}
	}
	for _, keep := range []string{"report.pdf", ".hidden-other"} {
		if _, err := os.Stat(filepath.Join(chat, keep)); err != nil {
			t.Errorf("%s was removed", keep)
		}
	}
}
