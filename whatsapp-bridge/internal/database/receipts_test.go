package database

import (
	"testing"
	"time"
)

func receiptRow(t *testing.T, s *MessageStore, id string) (status int, at time.Time) {
	t.Helper()
	err := s.db.QueryRow(`SELECT status, at FROM message_receipts WHERE message_id = ? AND chat_jid = 'c' AND participant = 'p'`, id).Scan(&status, &at)
	if err != nil {
		t.Fatalf("row %s: %v", id, err)
	}
	return status, at
}

func TestRecordMessageReceiptsOnlyRaises(t *testing.T) {
	s := newTestMessageStore(t)
	t1 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	t2 := t1.Add(time.Minute)

	must(t, s.RecordMessageReceipts("c", "p", []string{"A", "B"}, ReceiptDelivered, t1))
	must(t, s.RecordMessageReceipts("c", "p", []string{"A"}, ReceiptRead, t2))
	// A late delivered receipt must not lower A, and does nothing to its time.
	must(t, s.RecordMessageReceipts("c", "p", []string{"A", "B"}, ReceiptDelivered, t2.Add(time.Hour)))

	if st, at := receiptRow(t, s, "A"); st != ReceiptRead || !at.Equal(t2) {
		t.Errorf("A = %d at %v, want read at %v", st, at, t2)
	}
	if st, at := receiptRow(t, s, "B"); st != ReceiptDelivered || !at.Equal(t1) {
		t.Errorf("B = %d at %v, want delivered at %v", st, at, t1)
	}

	must(t, s.RecordMessageReceipts("c", "p", []string{"A"}, ReceiptPlayed, t2.Add(time.Minute)))
	if st, _ := receiptRow(t, s, "A"); st != ReceiptPlayed {
		t.Errorf("A = %d, want played", st)
	}
	must(t, s.RecordMessageReceipts("c", "p", nil, ReceiptRead, t2))
}
