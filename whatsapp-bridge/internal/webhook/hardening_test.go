package webhook

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	"whatsapp-bridge/internal/types"
)

func TestIsPrivateIPUnspecifiedMappedAndCGNAT(t *testing.T) {
	tests := []struct {
		ip   string
		want bool
	}{
		{"::", true},
		{"0.0.0.0", true},
		{"::ffff:127.0.0.1", true},
		{"::ffff:10.0.0.1", true},
		{"::ffff:0.0.0.0", true},
		{"::ffff:8.8.8.8", false},
		{"100.64.0.1", true},
		{"100.100.100.100", true},
		{"100.127.255.255", true},
		{"100.63.255.255", false},
		{"100.128.0.1", false},
		{"64:ff9b::7f00:1", true},
		{"2002:7f00:1::1", true},
		{"198.18.0.1", true},
		{"2606:4700:4700::1111", false},
		{"8.8.8.8", false},
	}
	for _, tt := range tests {
		t.Run(tt.ip, func(t *testing.T) {
			if got := isPrivateIP(net.ParseIP(tt.ip)); got != tt.want {
				t.Fatalf("isPrivateIP(%s) = %v, want %v", tt.ip, got, tt.want)
			}
		})
	}
}

func stageDNS(t *testing.T, answers map[string][]string) {
	t.Helper()
	old := lookupIP
	var mu sync.Mutex
	lookupIP = func(_ context.Context, host string) ([]net.IP, error) {
		mu.Lock()
		defer mu.Unlock()
		var out []net.IP
		for _, a := range answers[host] {
			out = append(out, net.ParseIP(a))
		}
		return out, nil
	}
	t.Cleanup(func() { lookupIP = old })
}

// Every resolved address is checked, and the connection goes to the address
// that was checked (review-bridge #4).
func TestSSRFDialChecksEveryAddressAndDialsTheCheckedOne(t *testing.T) {
	t.Setenv("DISABLE_SSRF_CHECK", "")
	t.Setenv("WEBHOOK_ALLOWED_ADDRS", "")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) }))
	defer srv.Close()
	_, port, _ := net.SplitHostPort(srv.Listener.Addr().String())
	dial := ssrfSafeDialContext()

	t.Run("a mixed answer with one private address is refused", func(t *testing.T) {
		stageDNS(t, map[string][]string{"mixed.example": {"8.8.8.8", "127.0.0.1"}})
		if _, err := dial(context.Background(), "tcp", "mixed.example:"+port); err == nil {
			t.Fatal("dial succeeded although one address is loopback")
		}
	})
	t.Run("unspecified and mapped forms are refused", func(t *testing.T) {
		for _, a := range []string{"::", "0.0.0.0", "::ffff:127.0.0.1", "100.100.100.100"} {
			stageDNS(t, map[string][]string{"h.example": {a}})
			if _, err := dial(context.Background(), "tcp", "h.example:"+port); err == nil {
				t.Fatalf("dial to %s succeeded", a)
			}
		}
	})
	t.Run("an allowed private address is dialled as resolved", func(t *testing.T) {
		t.Setenv("WEBHOOK_ALLOWED_ADDRS", "127.0.0.1:"+port)
		stageDNS(t, map[string][]string{"ok.example": {"127.0.0.1"}})
		conn, err := dial(context.Background(), "tcp", "ok.example:"+port)
		if err != nil {
			t.Fatalf("dial: %v", err)
		}
		_ = conn.Close()
		// The name now resolves elsewhere: a new dial re-checks, it does not reuse trust.
		stageDNS(t, map[string][]string{"ok.example": {"10.0.0.5"}})
		if _, err := dial(context.Background(), "tcp", "ok.example:"+port); err == nil {
			t.Fatal("rebound name was dialled")
		}
	})
}

func TestValidateWebhookURLBlocksNewForms(t *testing.T) {
	t.Setenv("DISABLE_SSRF_CHECK", "")
	t.Setenv("WEBHOOK_ALLOWED_ADDRS", "")
	for _, u := range []string{"http://[::]:8080/x", "http://0.0.0.0/x", "http://[::ffff:127.0.0.1]/x", "http://100.100.100.100/x"} {
		if err := ValidateWebhookURL(u); err == nil {
			t.Errorf("ValidateWebhookURL(%s) = nil, want error", u)
		}
	}
	t.Setenv("WEBHOOK_ALLOWED_ADDRS", "100.64.0.0/10")
	if err := ValidateWebhookURL("http://100.100.100.100/x"); err != nil {
		t.Errorf("tailnet address allowed via WEBHOOK_ALLOWED_ADDRS: %v", err)
	}
}

// The signed material is "<timestamp>.<body>"; the legacy header stays
// (review-bridge #5).
func TestWebhookSignatureCoversTimestamp(t *testing.T) {
	var got http.Header
	var body []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		b := make([]byte, 1024)
		n, _ := r.Body.Read(b)
		body = b[:n]
		w.WriteHeader(200)
	}))
	defer srv.Close()
	t.Setenv("DISABLE_SSRF_CHECK", "")
	t.Setenv("WEBHOOK_ALLOWED_ADDRS", srv.Listener.Addr().String())

	ds := newDeliveryService(nil, waLog.Noop)
	cfg := &types.WebhookConfig{WebhookURL: srv.URL, SecretToken: "s3cret"}
	payload := []byte(`{"a":1}`)
	if ok, status, resp := ds.sendHTTPRequest(cfg, payload); !ok {
		t.Fatalf("send failed: %d %s", status, resp)
	}
	ts := got.Get("X-Webhook-Timestamp")
	if n, err := strconv.ParseInt(ts, 10, 64); err != nil || time.Since(time.Unix(n, 0)) > time.Minute {
		t.Fatalf("X-Webhook-Timestamp = %q", ts)
	}
	mac := func(parts ...string) string {
		h := hmac.New(sha256.New, []byte("s3cret"))
		h.Write([]byte(strings.Join(parts, "")))
		return "sha256=" + hex.EncodeToString(h.Sum(nil))
	}
	if want := mac(ts, ".", string(body)); got.Get("X-Webhook-Signature-V2") != want {
		t.Fatalf("V2 signature = %q, want %q", got.Get("X-Webhook-Signature-V2"), want)
	}
	if want := mac(string(body)); got.Get("X-Webhook-Signature") != want {
		t.Fatalf("legacy signature = %q, want body-only %q", got.Get("X-Webhook-Signature"), want)
	}
	if got.Get("X-Webhook-Signature-V2") == mac("1"+ts, ".", string(body)) {
		t.Fatal("signature does not depend on the timestamp")
	}

	// No secret: timestamp still sent, no signatures.
	got = nil
	if ok, _, _ := ds.sendHTTPRequest(&types.WebhookConfig{WebhookURL: srv.URL}, payload); !ok {
		t.Fatal("unsigned send failed")
	}
	if got.Get("X-Webhook-Timestamp") == "" || got.Get("X-Webhook-Signature") != "" || got.Get("X-Webhook-Signature-V2") != "" {
		t.Fatalf("unsigned headers wrong: %v", got)
	}
}

// Deliveries run on a fixed pool; a full queue drops and counts instead of
// spawning more work (review-bridge #6).
func TestDeliveryQueueIsBoundedAndDropsWhenFull(t *testing.T) {
	ds := newDeliveryService(nil, waLog.Noop)
	release := make(chan struct{})
	var mu sync.Mutex
	started := 0
	ds.deliver = func(deliveryJob) {
		mu.Lock()
		started++
		mu.Unlock()
		<-release
	}
	ds.startWorkers(2, 3)
	cfg := &types.WebhookConfig{Name: "w"}
	trig := &types.WebhookTrigger{}

	// Two workers take two jobs; wait until both are busy so the queue is empty.
	for i := 0; i < 2; i++ {
		if !ds.Submit(cfg, &types.WebhookPayload{}, "m", "c", trig) {
			t.Fatal("submit to an idle pool was dropped")
		}
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		mu.Lock()
		n := started
		mu.Unlock()
		if n == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("workers started %d jobs, want 2", n)
		}
		time.Sleep(time.Millisecond)
	}
	accepted, dropped := 0, 0
	for i := 0; i < 10; i++ {
		if ds.Submit(cfg, &types.WebhookPayload{}, "m", "c", trig) {
			accepted++
		} else {
			dropped++
		}
	}
	if accepted != 3 || dropped != 7 || ds.Dropped() != 7 {
		t.Fatalf("accepted %d dropped %d (counter %d), want 3 / 7 / 7", accepted, dropped, ds.Dropped())
	}
	close(release)
}

func TestLogRetentionDays(t *testing.T) {
	tests := []struct {
		env  string
		want int
	}{{"", 30}, {"7", 7}, {"0", 0}, {"-1", 30}, {"abc", 30}}
	for _, tt := range tests {
		t.Run(tt.env, func(t *testing.T) {
			t.Setenv("WEBHOOK_LOG_RETENTION_DAYS", tt.env)
			if got := LogRetentionDays(); got != tt.want {
				t.Fatalf("LogRetentionDays = %d, want %d", got, tt.want)
			}
		})
	}
}

// A message that reused a stored ID is delivered flagged, with the sender as
// received and no download URL; a normal one carries no flag.
func TestProcessCollidingMessageFlagsPayload(t *testing.T) {
	ds := newDeliveryService(nil, waLog.Noop)
	var mu sync.Mutex
	var jobs []deliveryJob
	done := make(chan struct{}, 4)
	ds.deliver = func(j deliveryJob) { mu.Lock(); jobs = append(jobs, j); mu.Unlock(); done <- struct{}{} }
	ds.startWorkers(1, 4)
	wm := &Manager{
		logger:          waLog.Noop,
		delivery:        ds,
		downloadBaseURL: "http://localhost:8080/api/download",
		configs: []*types.WebhookConfig{{
			ID: 1, Name: "w", Enabled: true,
			Triggers: []types.WebhookTrigger{{TriggerType: "all", MatchType: "exact", Enabled: true}},
		}},
	}
	msg := textMessage(groupJID, phoneJID)
	msg.Message = &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("spoof")}}

	wm.ProcessCollidingMessage(nil, msg, "Group")
	wm.ProcessMessage(nil, msg, "Group")
	<-done
	<-done
	mu.Lock()
	defer mu.Unlock()
	if len(jobs) != 2 {
		t.Fatalf("deliveries = %d, want 2", len(jobs))
	}
	flagged, normal := jobs[0].payload.Message, jobs[1].payload.Message
	if !flagged.IDCollision || flagged.MediaDownloadURL != "" || flagged.Sender != phoneJID.String() {
		t.Fatalf("flagged payload = %+v", flagged)
	}
	if normal.IDCollision || normal.MediaDownloadURL == "" {
		t.Fatalf("normal payload = %+v", normal)
	}
	raw, _ := json.Marshal(jobs[0].payload)
	if !strings.Contains(string(raw), `"id_collision":true`) {
		t.Fatalf("JSON lacks the flag: %s", raw)
	}
	if raw2, _ := json.Marshal(jobs[1].payload); strings.Contains(string(raw2), "id_collision") {
		t.Fatalf("normal JSON carries the flag: %s", raw2)
	}
}
