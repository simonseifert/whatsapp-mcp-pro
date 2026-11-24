package whatsapp

import (
	"context"
	"errors"
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
	t.Setenv("MEDIA_DOWNLOAD_CONCURRENCY", "1")
	t.Setenv("MEDIA_MAX_BYTES", "1000")
	mediaSlotsOnce = sync.Once{}
	t.Cleanup(func() { mediaSlotsOnce = sync.Once{} })

	var calls int32
	inFlight := make(chan struct{})
	c := &Client{rawDownload: func(ctx context.Context, _ whatsmeow.DownloadableMessage) ([]byte, error) {
		atomic.AddInt32(&calls, 1)
		// The only slot is ours while we run.
		short, cancel := context.WithTimeout(ctx, 10*time.Millisecond)
		defer cancel()
		if _, err := acquireMediaSlot(short); err == nil {
			t.Error("slot was free during the download")
		}
		close(inFlight)
		return []byte("data"), nil
	}}
	size := uint64(10)
	msg := &waE2E.DocumentMessage{FileLength: &size}
	data, err := c.downloadMedia(context.Background(), msg)
	if err != nil || string(data) != "data" {
		t.Fatalf("download = %q, %v", data, err)
	}
	<-inFlight
	if n := atomic.LoadInt32(&calls); n != 1 {
		t.Fatalf("underlying downloads = %d, want 1", n)
	}
	release, err := acquireMediaSlot(context.Background())
	if err != nil {
		t.Fatalf("slot not released: %v", err)
	}
	release()

	// Over the declared cap: refused without calling the downloader or taking a slot.
	big := uint64(5000)
	if _, err := c.downloadMedia(context.Background(), &waE2E.DocumentMessage{FileLength: &big}); !errors.Is(err, ErrMediaTooLarge) {
		t.Fatalf("oversize err = %v", err)
	}
	if n := atomic.LoadInt32(&calls); n != 1 {
		t.Fatalf("downloader called for an oversize file (%d calls)", n)
	}
}
