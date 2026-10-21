package api

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/crypto/hkdf"

	"whatsapp-bridge/internal/mediastore"
	"whatsapp-bridge/internal/whatsapp"
)

// maxMediaBytes caps the encrypted payload accepted from the WhatsApp CDN to
// bound memory use per request. WhatsApp's largest documented media (documents)
// is 64 MiB; 100 MiB leaves headroom for the trailing MAC and future limits.
const maxMediaBytes = 100 << 20

// hkdfInfo maps WhatsApp media types to the HKDF info string used for key derivation.
var hkdfInfo = map[string][]byte{
	"image":    []byte("WhatsApp Image Keys"),
	"video":    []byte("WhatsApp Video Keys"),
	"audio":    []byte("WhatsApp Audio Keys"),
	"document": []byte("WhatsApp Document Keys"),
}

type downloadRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// handleDownload handles POST /api/download — fetches and decrypts WhatsApp media
// using the URL/MediaKey/MediaType stored in the bridge's SQLite for the given message.
//
// Request body:
//   - message_id: WhatsApp message ID (required)
//   - chat_jid:   chat JID containing the message (required)
//
// Response: { success: bool, path: string, size: int, via: string, message?: string }
//
// path is absolute. via says where the bytes came from: "auto-download" (the
// copy saved when the message arrived), "cdn" (the URL stored with the message)
// "whatsmeow" (direct_path re-resolved, for when that URL has expired) or
// "saved" (a copy already in store/media, e.g. from the media retry queue).
func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		SendJSONError(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req downloadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		SendJSONError(w, "Invalid request format", http.StatusBadRequest)
		return
	}
	if req.MessageID == "" || req.ChatJID == "" {
		SendJSONError(w, "message_id and chat_jid are required", http.StatusBadRequest)
		return
	}

	var (
		mediaType, url, filename string
		mediaKey, fileSHA256     []byte
	)
	row := s.messageStore.GetDB().QueryRow(
		`SELECT media_type, url, media_key, COALESCE(filename, ''), file_sha256
		 FROM messages WHERE id = ? AND chat_jid = ?`,
		req.MessageID, req.ChatJID,
	)
	if err := row.Scan(&mediaType, &url, &mediaKey, &filename, &fileSHA256); err != nil {
		SendJSONError(w, "message not found: "+err.Error(), http.StatusNotFound)
		return
	}
	info, ok := hkdfInfo[mediaType]
	if !ok || len(mediaKey) == 0 {
		SendJSONError(w, "no downloadable media for this message", http.StatusBadRequest)
		return
	}

	// The bridge already saved most media on receipt, while its URL was fresh.
	// Serving that copy is the only thing that still works once WhatsApp has
	// purged the file from its servers. Saved names are only unique to the
	// second, so the copy must hash to this message's file_sha256; one that
	// doesn't belongs to another message and the network paths below run.
	if paths, err := whatsapp.AutoDownloadPaths(req.ChatJID, req.MessageID, filename); err == nil {
		if saved := whatsapp.FindAutoDownloaded(paths, fileSHA256); saved != "" {
			writeDownloadResult(w, saved, "auto-download")
			return
		}
	}
	// A copy saved by an earlier download, or by the background media retry
	// queue once the sender's phone re-uploaded purged media. Hash-checked like
	// the auto-download copy, so a stale file never masks the real media.
	if saved, err := mediastore.Path(req.ChatJID, req.MessageID, mediaType, filename); err == nil {
		if whatsapp.FindAutoDownloaded([]string{saved}, fileSHA256) != "" {
			writeDownloadResult(w, saved, "saved")
			return
		}
	}
	if url == "" {
		s.downloadViaWhatsmeow(w, r, req, "no stored media URL")
		return
	}

	// The URL column is copied verbatim from the *sender's* message protobuf, so
	// it is remote-controlled data: a crafted message could point it at cloud
	// metadata or an internal service and this handler would dutifully GET it.
	// Real WhatsApp media only ever lives on Meta CDNs — enforce that.
	if err := validateCDNURL(url); err != nil {
		// whatsmeow picks the media host itself, so the fallback is not exposed
		// to whatever the sender put in the URL.
		s.downloadViaWhatsmeow(w, r, req, "refusing media URL: "+err.Error())
		return
	}

	// Fetch encrypted media from WhatsApp CDN, bounded by request context and total timeout.
	httpClient := &http.Client{
		Timeout: 60 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 3 {
				return fmt.Errorf("too many redirects")
			}
			return validateCDNURL(req.URL.String())
		},
	}
	greq, err := http.NewRequestWithContext(r.Context(), http.MethodGet, url, nil)
	if err != nil {
		SendJSONError(w, "bad CDN URL: "+err.Error(), http.StatusBadGateway)
		return
	}
	greq.Header.Set("User-Agent", "WhatsApp/2.24.0")
	resp, err := httpClient.Do(greq)
	if err != nil {
		s.downloadViaWhatsmeow(w, r, req, "fetch failed: "+err.Error())
		return
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		s.downloadViaWhatsmeow(w, r, req, fmt.Sprintf("CDN returned HTTP %d", resp.StatusCode))
		return
	}
	enc, err := io.ReadAll(io.LimitReader(resp.Body, maxMediaBytes+1))
	if err != nil {
		SendJSONError(w, "read failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	if int64(len(enc)) > maxMediaBytes {
		SendJSONError(w, "media exceeds size limit", http.StatusRequestEntityTooLarge)
		return
	}
	if len(enc) < 26 {
		SendJSONError(w, "ciphertext too short", http.StatusBadGateway)
		return
	}

	// Derive keys: HKDF-SHA256(mediaKey, salt=zero32, info, 112 bytes).
	expanded := make([]byte, 112)
	kdf := hkdf.New(sha256.New, mediaKey, make([]byte, 32), info)
	if _, err := io.ReadFull(kdf, expanded); err != nil {
		SendJSONError(w, "hkdf failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	iv, cipherKey, macKey := expanded[:16], expanded[16:48], expanded[48:80]

	body, mac := enc[:len(enc)-10], enc[len(enc)-10:]
	hm := hmac.New(sha256.New, macKey)
	hm.Write(iv)
	hm.Write(body)
	expectedMAC := hm.Sum(nil)[:10]
	if !hmac.Equal(mac, expectedMAC) {
		SendJSONError(w, "MAC verification failed", http.StatusBadRequest)
		return
	}

	block, err := aes.NewCipher(cipherKey)
	if err != nil {
		SendJSONError(w, "cipher init: "+err.Error(), http.StatusInternalServerError)
		return
	}
	if len(body) == 0 || len(body)%aes.BlockSize != 0 {
		SendJSONError(w, "ciphertext not block-aligned", http.StatusBadRequest)
		return
	}
	plain := make([]byte, len(body))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, body)
	// Strict PKCS#7 unpad: validate every padding byte equals the pad length.
	pad := int(plain[len(plain)-1])
	if pad <= 0 || pad > aes.BlockSize || pad > len(plain) {
		SendJSONError(w, "bad PKCS7 padding", http.StatusInternalServerError)
		return
	}
	for i := len(plain) - pad; i < len(plain); i++ {
		if int(plain[i]) != pad {
			SendJSONError(w, "bad PKCS7 padding", http.StatusInternalServerError)
			return
		}
	}
	plain = plain[:len(plain)-pad]

	outPath, err := saveMedia(req.ChatJID, req.MessageID, mediaType, filename, plain)
	if err != nil {
		SendJSONError(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeDownloadResult(w, outPath, "cdn")
}

// downloadViaWhatsmeow is the fallback when the stored CDN URL is unusable. Those
// URLs carry signed, expiring query params, so anything older than a few weeks
// 403s. whatsmeow re-resolves the message's direct_path against fresh media
// hosts, which works for as long as WhatsApp still holds the file.
func (s *Server) downloadViaWhatsmeow(w http.ResponseWriter, r *http.Request, req downloadRequest, cdnErr string) {
	data, mediaType, filename, err := s.client.FetchMessageMedia(r.Context(), s.messageStore, req.MessageID, req.ChatJID)
	if err != nil {
		SendJSONError(w, cdnErr+"; whatsmeow fallback: "+err.Error(), http.StatusBadGateway)
		return
	}
	outPath, err := saveMedia(req.ChatJID, req.MessageID, mediaType, filename, data)
	if err != nil {
		SendJSONError(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeDownloadResult(w, outPath, "whatsmeow")
}

// saveMedia writes decrypted media to store/media/<chat_jid>/<message_id><ext>
// and returns the absolute path. The layout lives in mediastore so the
// background media retry queue saves exactly where this handler looks.
func saveMedia(chatJID, messageID, mediaType, filename string, data []byte) (string, error) {
	return mediastore.Save(chatJID, messageID, mediaType, filename, data)
}

// mediaExtension is mediastore.Extension, kept for this package's tests.
func mediaExtension(mediaType, filename string) string {
	return mediastore.Extension(mediaType, filename)
}

func writeDownloadResult(w http.ResponseWriter, path, via string) {
	if abs, err := filepath.Abs(path); err == nil {
		path = abs
	}
	var size int64
	if info, err := os.Stat(path); err == nil {
		size = info.Size()
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"success": true,
		"path":    path,
		"size":    size,
		"via":     via,
	})
}

// whatsappCDNSuffixes are the host suffixes Meta serves WhatsApp media from.
var whatsappCDNSuffixes = []string{".whatsapp.net", ".fbcdn.net", ".cdninstagram.com"}

// validateCDNURL accepts only HTTPS URLs on known WhatsApp/Meta CDN hosts.
// DISABLE_SSRF_CHECK=true bypasses it, mirroring the webhook validator's
// escape hatch for closed test networks.
func validateCDNURL(raw string) error {
	if os.Getenv("DISABLE_SSRF_CHECK") == "true" {
		return nil
	}
	u, err := neturl.Parse(raw)
	if err != nil {
		return fmt.Errorf("unparseable URL")
	}
	if u.Scheme != "https" {
		return fmt.Errorf("scheme %q not allowed", u.Scheme)
	}
	host := strings.ToLower(u.Hostname())
	for _, suffix := range whatsappCDNSuffixes {
		if strings.HasSuffix(host, suffix) {
			return nil
		}
	}
	return fmt.Errorf("host %q is not a WhatsApp CDN", host)
}
