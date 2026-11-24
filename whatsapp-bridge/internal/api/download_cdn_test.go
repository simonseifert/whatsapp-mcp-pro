package api

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"golang.org/x/crypto/hkdf"

	"whatsapp-bridge/internal/whatsapp"
)

// encryptCDN builds what the WhatsApp CDN serves: AES-CBC with PKCS#7 padding
// plus a 10-byte HMAC, keyed from mediaKey the way decryptCDNMedia derives it.
func encryptCDN(t *testing.T, plain, mediaKey, info []byte) []byte {
	t.Helper()
	expanded := make([]byte, 112)
	if _, err := io.ReadFull(hkdf.New(sha256.New, mediaKey, make([]byte, 32), info), expanded); err != nil {
		t.Fatal(err)
	}
	iv, cipherKey, macKey := expanded[:16], expanded[16:48], expanded[48:80]
	pad := aes.BlockSize - len(plain)%aes.BlockSize
	padded := append(append([]byte{}, plain...), make([]byte, pad)...)
	for i := len(plain); i < len(padded); i++ {
		padded[i] = byte(pad)
	}
	block, err := aes.NewCipher(cipherKey)
	if err != nil {
		t.Fatal(err)
	}
	body := make([]byte, len(padded))
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(body, padded)
	h := hmac.New(sha256.New, macKey)
	h.Write(iv)
	h.Write(body)
	return append(body, h.Sum(nil)[:10]...)
}

// cdnDownloadServer stores a document whose URL points at an httptest "CDN"
// serving payload, and returns the server plus the request to send.
func cdnDownloadServer(t *testing.T, payload func(mediaKey, info []byte) []byte) (*Server, map[string]any) {
	t.Helper()
	t.Chdir(t.TempDir())
	t.Setenv("DISABLE_SSRF_CHECK", "true") // the test CDN is http on loopback
	mediaKey := []byte("0123456789abcdef0123456789abcdef")
	body := payload(mediaKey, hkdfInfo["document"])
	cdn := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(body) }))
	t.Cleanup(cdn.Close)

	s := newSendTestServer(t, &fakeSender{})
	const chat = "4915100000001@s.whatsapp.net"
	if err := s.messageStore.StoreChat(chat, "c", time.Now()); err != nil {
		t.Fatal(err)
	}
	// file_length 0: only the stream-time checks are under test.
	if err := s.messageStore.StoreMessage("CDN1", chat, chat, "c", "", time.Now(), false,
		"document", "x.bin", cdn.URL+"/f", "/f", mediaKey, []byte("sha"), []byte("enc"), 0); err != nil {
		t.Fatal(err)
	}
	return s, map[string]any{"message_id": "CDN1", "chat_jid": chat}
}

// MEDIA_MAX_BYTES caps the file, not the wire bytes: a file exactly at the
// limit (with its padding and MAC on top) is accepted, one byte more is not.
func TestCDNPathCapsPlaintextNotCiphertext(t *testing.T) {
	const limit = 1000
	tests := []struct {
		name string
		size int
		want int
	}{
		{"well under the limit", 100, http.StatusOK},
		{"exactly at the limit", limit, http.StatusOK},
		{"at the limit, block-aligned (a full padding block)", 992, http.StatusOK},
		{"one byte over", limit + 1, http.StatusRequestEntityTooLarge},
		{"far over", limit * 5, http.StatusRequestEntityTooLarge},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("MEDIA_MAX_BYTES", "1000")
			s, req := cdnDownloadServer(t, func(key, info []byte) []byte {
				return encryptCDN(t, make([]byte, tt.size), key, info)
			})
			rec := post(t, s.handleDownload, req, nil)
			if rec.Code != tt.want {
				t.Fatalf("status %d, want %d: %s", rec.Code, tt.want, rec.Body)
			}
		})
	}
}

// The CDN read-and-decrypt holds the shared download slot: with one slot taken
// elsewhere the request waits, and it finishes once the slot is released.
func TestCDNPathTakesTheSharedDownloadSlot(t *testing.T) {
	t.Setenv("MEDIA_DOWNLOAD_CONCURRENCY", "1")
	whatsapp.ResetMediaSlots()
	t.Cleanup(whatsapp.ResetMediaSlots)
	s, req := cdnDownloadServer(t, func(key, info []byte) []byte {
		return encryptCDN(t, []byte("hello"), key, info)
	})

	release, err := whatsapp.AcquireMediaSlot(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan int, 1)
	go func() { done <- post(t, s.handleDownload, req, nil).Code }()

	select {
	case code := <-done:
		t.Fatalf("download finished (%d) while the only slot was held", code)
	case <-time.After(300 * time.Millisecond):
	}
	release()
	select {
	case code := <-done:
		if code != http.StatusOK {
			t.Fatalf("status %d after the slot was released, want 200", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("download did not proceed after the slot was released")
	}
	// And the slot is free again afterwards.
	r2, err := whatsapp.AcquireMediaSlot(t.Context())
	if err != nil {
		t.Fatalf("slot not released by the handler: %v", err)
	}
	r2()
}
