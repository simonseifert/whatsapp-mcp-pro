package database

import (
	"testing"
	"time"
)

func newSendStateStore(t *testing.T) *MessageStore {
	t.Helper()
	store := newTestMessageStore(t)
	if err := createSendStateTables(store.db); err != nil {
		t.Fatalf("create send state tables: %v", err)
	}
	return store
}

func TestClaimIdempotencyKeyLifecycle(t *testing.T) {
	store := newSendStateStore(t)
	now := time.Now()

	rec, proceed, err := store.ClaimIdempotencyKey("k1", "send|a", "h1", now)
	if err != nil || !proceed || rec.Status != IdempotencyInFlight {
		t.Fatalf("first claim: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	// Second claim while in flight: not allowed.
	rec, proceed, err = store.ClaimIdempotencyKey("k1", "send|a", "h1", now)
	if err != nil || proceed || rec.Status != IdempotencyInFlight {
		t.Fatalf("in-flight claim: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	if err := store.SetIdempotencyMessageID("k1", "send|a", "MSGID"); err != nil {
		t.Fatal(err)
	}
	if err := store.FinishIdempotencyKey("k1", "send|a", IdempotencyFailed, "", 504, []byte(`{"success":false}`), now); err != nil {
		t.Fatal(err)
	}

	// Failed: a retry with the same payload re-claims and sees the earlier message ID.
	rec, proceed, err = store.ClaimIdempotencyKey("k1", "send|a", "h1", now)
	if err != nil || !proceed || rec.MessageID != "MSGID" {
		t.Fatalf("reclaim after failure: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	if err := store.FinishIdempotencyKey("k1", "send|a", IdempotencyDone, "MSGID", 200, []byte(`{"success":true}`), now); err != nil {
		t.Fatal(err)
	}
	rec, proceed, err = store.ClaimIdempotencyKey("k1", "send|a", "h1", now)
	if err != nil || proceed || rec.Status != IdempotencyDone || string(rec.Response) != `{"success":true}` || rec.HTTPStatus != 200 {
		t.Fatalf("claim after done: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	// Different payload under the same key: not claimed, hash differs.
	rec, proceed, err = store.ClaimIdempotencyKey("k1", "send|a", "other", now)
	if err != nil || proceed || rec.PayloadHash != "h1" {
		t.Fatalf("mismatched payload: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	// Same key, different scope (recipient): independent.
	_, proceed, err = store.ClaimIdempotencyKey("k1", "send|b", "h1", now)
	if err != nil || !proceed {
		t.Fatalf("other scope: proceed=%v err=%v", proceed, err)
	}
}

func TestClaimIdempotencyKeyExpiryAndStaleInFlight(t *testing.T) {
	store := newSendStateStore(t)
	start := time.Now()

	if _, _, err := store.ClaimIdempotencyKey("old", "send|a", "h", start); err != nil {
		t.Fatal(err)
	}
	if err := store.FinishIdempotencyKey("old", "send|a", IdempotencyDone, "M", 200, []byte(`{}`), start); err != nil {
		t.Fatal(err)
	}
	// After the TTL the key is purged and claimable as new.
	rec, proceed, err := store.ClaimIdempotencyKey("old", "send|a", "different", start.Add(IdempotencyTTL+time.Minute))
	if err != nil || !proceed || rec.MessageID != "" {
		t.Fatalf("expired key: rec=%+v proceed=%v err=%v", rec, proceed, err)
	}

	// An in-flight row abandoned by a crash becomes reclaimable once stale.
	if _, _, err := store.ClaimIdempotencyKey("stuck", "send|a", "h", start); err != nil {
		t.Fatal(err)
	}
	if _, proceed, _ := store.ClaimIdempotencyKey("stuck", "send|a", "h", start.Add(time.Minute)); proceed {
		t.Fatal("fresh in-flight key must not be reclaimed")
	}
	if _, proceed, _ := store.ClaimIdempotencyKey("stuck", "send|a", "h", start.Add(idempotencyStaleInFlight+time.Minute)); !proceed {
		t.Fatal("stale in-flight key should be reclaimable")
	}
}

func TestBridgeStateTime(t *testing.T) {
	store := newSendStateStore(t)
	if got, err := store.GetBridgeStateTime("x"); err != nil || !got.IsZero() {
		t.Fatalf("unset: %v %v", got, err)
	}
	want := time.Unix(1_900_000_000, 0)
	if err := store.SetBridgeStateTime("x", want); err != nil {
		t.Fatal(err)
	}
	if got, err := store.GetBridgeStateTime("x"); err != nil || !got.Equal(want) {
		t.Fatalf("get: %v %v", got, err)
	}
	if err := store.SetBridgeStateTime("x", time.Time{}); err != nil {
		t.Fatal(err)
	}
	if got, _ := store.GetBridgeStateTime("x"); !got.IsZero() {
		t.Fatalf("cleared: %v", got)
	}
}

func TestChatHasMessages(t *testing.T) {
	store := newSendStateStore(t)
	if err := store.StoreChat("1@s.whatsapp.net", "A", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("m1", "1@s.whatsapp.net", "1", "A", "hi", time.Now(), false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name string
		jids []string
		want bool
	}{
		{"existing", []string{"1@s.whatsapp.net"}, true},
		{"via alias", []string{"2@s.whatsapp.net", "1@s.whatsapp.net"}, true},
		{"new", []string{"2@s.whatsapp.net"}, false},
		{"empty", []string{""}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := store.ChatHasMessages(tt.jids...)
			if err != nil || got != tt.want {
				t.Fatalf("ChatHasMessages(%v) = %v, %v; want %v", tt.jids, got, err, tt.want)
			}
		})
	}
}
