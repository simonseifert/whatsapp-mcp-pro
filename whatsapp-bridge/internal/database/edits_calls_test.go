package database

import (
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"
)

const (
	testPhoneChat = "4915112345678@s.whatsapp.net"
	testLIDChat   = "123456789012345@lid"
	testGroup     = "120363000000000001@g.us"
)

func TestEditsCallsSQLMatchesMigration(t *testing.T) {
	raw, err := os.ReadFile("../../migrations/009_edits_calls.sql")
	if err != nil {
		t.Fatalf("read migration: %v", err)
	}
	var body []string
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "--") {
			continue
		}
		body = append(body, line)
	}
	var stmts []string
	for _, s := range strings.Split(strings.Join(body, "\n"), ";") {
		if s = strings.TrimSpace(s); s != "" {
			stmts = append(stmts, s)
		}
	}
	if len(stmts) != len(editsCallsSQL) {
		t.Fatalf("migration has %d statements, Go mirror has %d", len(stmts), len(editsCallsSQL))
	}
	for i := range stmts {
		if stmts[i] != strings.TrimSpace(editsCallsSQL[i]) {
			t.Errorf("statement %d differs:\nfile: %s\ngo:   %s", i, stmts[i], editsCallsSQL[i])
		}
	}
}

func TestEditsCallsMigrationIsIdempotent(t *testing.T) {
	store := newTestMessageStore(t)
	for i := 0; i < 2; i++ {
		if err := runMigrations(store.db); err != nil {
			t.Fatalf("run %d: %v", i, err)
		}
	}
}

func storeText(t *testing.T, store *MessageStore, id, chat, text string, ts time.Time) {
	t.Helper()
	if _, err := store.db.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, chat); err != nil {
		t.Fatalf("chat: %v", err)
	}
	if err := store.StoreMessage(id, chat, "4915112345678", "Max", text, ts, false, "", "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("store %s: %v", id, err)
	}
}

type msgState struct {
	content   sql.NullString
	isEdited  bool
	editCount int
	deletedAt sql.NullTime
	deletedBy sql.NullString
}

func readMsg(t *testing.T, store *MessageStore, id, chat string) msgState {
	t.Helper()
	var s msgState
	err := store.db.QueryRow(
		`SELECT content, COALESCE(is_edited, 0), COALESCE(edit_count, 0), deleted_at, deleted_by
		   FROM messages WHERE id = ? AND chat_jid = ?`, id, chat,
	).Scan(&s.content, &s.isEdited, &s.editCount, &s.deletedAt, &s.deletedBy)
	if err != nil {
		t.Fatalf("read %s: %v", id, err)
	}
	return s
}

func TestApplyEdit(t *testing.T) {
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	t1, t2 := sent.Add(time.Minute), sent.Add(2*time.Minute)

	type step struct {
		edit                         Edit
		wantApplied, wantDup, wantPH bool
		wantOld                      string
	}
	tests := []struct {
		name        string
		original    string // "" means the original was never stored
		steps       []step
		replay      bool // StoreMessage the original again afterwards
		wantContent string
		wantCount   int
		wantEdits   int
	}{
		{
			name:     "plain edit",
			original: "helo",
			steps: []step{{edit: Edit{NewContent: "hello", EditedAt: t1, StanzaID: "E1"},
				wantApplied: true, wantOld: "helo"}},
			wantContent: "hello", wantCount: 1, wantEdits: 1,
		},
		{
			name:     "same edit twice is a duplicate",
			original: "helo",
			steps: []step{
				{edit: Edit{NewContent: "hello", EditedAt: t1, StanzaID: "E1"}, wantApplied: true, wantOld: "helo"},
				{edit: Edit{NewContent: "hello", EditedAt: t1, StanzaID: "E1"}, wantDup: true},
			},
			wantContent: "hello", wantCount: 1, wantEdits: 1,
		},
		{
			name:     "older edit arriving late does not overwrite newer text",
			original: "v0",
			steps: []step{
				{edit: Edit{NewContent: "v2", EditedAt: t2, StanzaID: "E2"}, wantApplied: true, wantOld: "v0"},
				{edit: Edit{NewContent: "v1", EditedAt: t1, StanzaID: "E1"}},
			},
			wantContent: "v2", wantCount: 2, wantEdits: 2,
		},
		{
			name:     "replay of the original after an edit keeps the edited text",
			original: "typo",
			steps: []step{{edit: Edit{NewContent: "fixed", EditedAt: t1, StanzaID: "E1"},
				wantApplied: true, wantOld: "typo"}},
			replay:      true,
			wantContent: "fixed", wantCount: 1, wantEdits: 1,
		},
		{
			name: "edit of an unknown message creates a placeholder",
			steps: []step{{edit: Edit{NewContent: "late", EditedAt: t1, StanzaID: "E1", Editor: testPhoneChat},
				wantApplied: true, wantPH: true}},
			replay:      true,
			wantContent: "late", wantCount: 1, wantEdits: 1,
		},
		{
			name:     "keyless edits at different times are distinct",
			original: "a",
			steps: []step{
				{edit: Edit{NewContent: "b", EditedAt: t1}, wantApplied: true, wantOld: "a"},
				{edit: Edit{NewContent: "c", EditedAt: t2}, wantApplied: true, wantOld: "b"},
			},
			wantContent: "c", wantCount: 2, wantEdits: 2,
		},
	}
	store := newTestMessageStore(t)
	for i, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			id := "MSG" + string(rune('A'+i))
			if tc.original != "" {
				storeText(t, store, id, testPhoneChat, tc.original, sent)
			}
			for j, s := range tc.steps {
				e := s.edit
				e.MessageID, e.ChatJIDs, e.Source = id, []string{testPhoneChat, testLIDChat}, SourceLive
				got, err := store.ApplyEdit(e)
				if err != nil {
					t.Fatalf("step %d: %v", j, err)
				}
				if got.Applied != s.wantApplied || got.Duplicate != s.wantDup || got.Placeholder != s.wantPH || got.OldContent != s.wantOld {
					t.Errorf("step %d: got %+v, want applied=%v dup=%v placeholder=%v old=%q",
						j, got, s.wantApplied, s.wantDup, s.wantPH, s.wantOld)
				}
				if got.ChatJID != testPhoneChat {
					t.Errorf("step %d: chat = %q", j, got.ChatJID)
				}
			}
			if tc.replay {
				orig := tc.original
				if orig == "" {
					orig = "original text"
				}
				storeText(t, store, id, testPhoneChat, orig, sent)
			}
			m := readMsg(t, store, id, testPhoneChat)
			if m.content.String != tc.wantContent || !m.isEdited || m.editCount != tc.wantCount {
				t.Errorf("message = %+v, want content %q, edited, count %d", m, tc.wantContent, tc.wantCount)
			}
			edits, err := store.GetMessageEdits(id, testPhoneChat)
			if err != nil {
				t.Fatal(err)
			}
			if len(edits) != tc.wantEdits {
				t.Fatalf("edits = %d, want %d", len(edits), tc.wantEdits)
			}
			for k := 1; k < len(edits); k++ {
				if edits[k].EditedAt.Before(edits[k-1].EditedAt) {
					t.Errorf("edits not oldest first: %v", edits)
				}
			}
		})
	}
}

func TestApplyEditFindsChatUnderOtherForm(t *testing.T) {
	store := newTestMessageStore(t)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	storeText(t, store, "M1", testLIDChat, "old", sent)
	got, err := store.ApplyEdit(Edit{MessageID: "M1", ChatJIDs: []string{testPhoneChat, testLIDChat},
		NewContent: "new", EditedAt: sent.Add(time.Minute), StanzaID: "E1", Source: SourceLive})
	if err != nil {
		t.Fatal(err)
	}
	if got.ChatJID != testLIDChat || got.Placeholder || !got.Applied {
		t.Errorf("got %+v, want applied under the LID chat", got)
	}
}

func TestApplyEditDropsEmbedding(t *testing.T) {
	store := newTestMessageStore(t)
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	storeText(t, store, "M1", testPhoneChat, "old", sent)
	if _, err := store.db.Exec(`CREATE TABLE message_embeddings (message_id TEXT, chat_jid TEXT, model TEXT, embedding BLOB)`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(`INSERT INTO message_embeddings VALUES ('M1', ?, 'm', x'00')`, testPhoneChat); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ApplyEdit(Edit{MessageID: "M1", ChatJIDs: []string{testPhoneChat}, NewContent: "new",
		EditedAt: sent.Add(time.Minute), StanzaID: "E1", Source: SourceLive}); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM message_embeddings`).Scan(&n); err != nil || n != 0 {
		t.Errorf("embeddings left = %d (err %v), want 0", n, err)
	}
}

func TestMarkDeleted(t *testing.T) {
	sent := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	store := newTestMessageStore(t)
	tests := []struct {
		name        string
		original    string
		revokes     int
		replay      bool
		wantContent string
	}{
		{name: "keeps content", original: "secret", revokes: 1, wantContent: "secret"},
		{name: "second revoke is not new", original: "x", revokes: 2, wantContent: "x"},
		{name: "unknown message gets a placeholder that a replay fills", revokes: 1, replay: true, wantContent: "late original"},
	}
	for i, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			id := "DEL" + string(rune('A'+i))
			if tc.original != "" {
				storeText(t, store, id, testPhoneChat, tc.original, sent)
			}
			first := sent.Add(time.Minute)
			for r := 0; r < tc.revokes; r++ {
				chat, newly, err := store.MarkDeleted(id, []string{testPhoneChat}, first.Add(time.Duration(r)*time.Minute), testPhoneChat)
				if err != nil {
					t.Fatal(err)
				}
				if chat != testPhoneChat || newly != (r == 0) {
					t.Errorf("revoke %d: chat %q newly %v", r, chat, newly)
				}
			}
			if tc.replay {
				storeText(t, store, id, testPhoneChat, "late original", sent)
			}
			m := readMsg(t, store, id, testPhoneChat)
			if m.content.String != tc.wantContent {
				t.Errorf("content = %q, want %q", m.content.String, tc.wantContent)
			}
			if !m.deletedAt.Valid || !m.deletedAt.Time.Equal(first) || m.deletedBy.String != testPhoneChat {
				t.Errorf("deleted = %v by %q, want %v by %q", m.deletedAt, m.deletedBy.String, first, testPhoneChat)
			}
		})
	}
}

func TestApplyCallEvent(t *testing.T) {
	t0 := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	ev := func(kind string, sec int, mod ...func(*CallEvent)) CallEvent {
		e := CallEvent{CallID: "C1", Kind: kind, ChatJID: testPhoneChat, CallerJID: testPhoneChat, At: t0.Add(time.Duration(sec) * time.Second), Source: SourceLive}
		for _, m := range mod {
			m(&e)
		}
		return e
	}
	incoming := func(e *CallEvent) { e.Direction = "incoming" }
	outgoing := func(e *CallEvent) { e.Direction = "outgoing" }
	byMe := func(e *CallEvent) { e.ByMe = true }

	tests := []struct {
		name         string
		events       []CallEvent
		wantOutcome  string
		wantDuration int64 // -1 = none
		wantVideo    bool
	}{
		{"rang out", []CallEvent{ev(CallEventOffer, 0, incoming), ev(CallEventTerminate, 30)}, CallMissed, -1, false},
		{"answered on phone has no duration", []CallEvent{ev(CallEventOffer, 0, incoming), ev(CallEventAccept, 5, byMe), ev(CallEventTerminate, 5)}, CallAnswered, -1, false},
		{"outgoing answered gets duration", []CallEvent{ev(CallEventOffer, 0, outgoing), ev(CallEventAccept, 4), ev(CallEventTerminate, 64)}, CallAnswered, 60, false},
		{"rejected", []CallEvent{ev(CallEventOffer, 0, outgoing), ev(CallEventReject, 3), ev(CallEventTerminate, 3)}, CallRejected, -1, false},
		{"busy terminate", []CallEvent{ev(CallEventOffer, 0, outgoing), ev(CallEventTerminate, 2, func(e *CallEvent) { e.Reason = "busy" })}, CallRejected, -1, false},
		{"log entry wins", []CallEvent{ev(CallEventOffer, 0, incoming, func(e *CallEvent) { e.IsVideo = true }), ev(CallEventAccept, 5, byMe), ev(CallEventTerminate, 5),
			ev(CallEventLog, 200, func(e *CallEvent) { e.Outcome = CallAnswered; e.Duration = 195 * time.Second })}, CallAnswered, 195, true},
		{"missed stub", []CallEvent{ev(CallEventMissed, 0)}, CallMissed, -1, false},
		{"missed stub does not override answered", []CallEvent{ev(CallEventAccept, 0), ev(CallEventMissed, 1)}, CallAnswered, -1, false},
		{"offer only", []CallEvent{ev(CallEventOffer, 0, incoming)}, CallUnknown, -1, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var c Call
			for _, e := range tc.events {
				c = applyCallEvent(c, e)
			}
			if c.Outcome != tc.wantOutcome {
				t.Errorf("outcome = %q, want %q", c.Outcome, tc.wantOutcome)
			}
			gotDur := int64(-1)
			if c.DurationSeconds.Valid {
				gotDur = c.DurationSeconds.Int64
			}
			if gotDur != tc.wantDuration {
				t.Errorf("duration = %d, want %d", gotDur, tc.wantDuration)
			}
			if c.IsVideo != tc.wantVideo || !c.StartedAt.Equal(t0) {
				t.Errorf("video %v started %v", c.IsVideo, c.StartedAt)
			}
		})
	}
}

func TestRecordCallEventMatchesLogEntry(t *testing.T) {
	store := newTestMessageStore(t)
	t0 := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	for _, ev := range []CallEvent{
		{CallID: "LIVE1", Kind: CallEventOffer, ChatJID: testPhoneChat, Direction: "incoming", At: t0, Source: SourceLive},
		{CallID: "LIVE1", Kind: CallEventAccept, ChatJID: testPhoneChat, ByMe: true, At: t0.Add(3 * time.Second), Source: SourceLive},
		{CallID: "LIVE1", Kind: CallEventTerminate, ChatJID: testPhoneChat, At: t0.Add(3 * time.Second), Source: SourceLive},
		// The phone's log entry arrives after the call, under its own ID.
		{CallID: "LOGMSG1", Kind: CallEventLog, ChatJID: testPhoneChat, Outcome: CallAnswered, Duration: 5 * time.Minute, At: t0.Add(5 * time.Minute), Source: SourceLive},
		// A log entry for a different chat must not attach to LIVE1.
		{CallID: "LOGMSG2", Kind: CallEventLog, ChatJID: testLIDChat, Outcome: CallMissed, At: t0.Add(time.Minute), Source: SourceLive},
	} {
		if _, err := store.RecordCallEvent(ev); err != nil {
			t.Fatalf("%s %s: %v", ev.CallID, ev.Kind, err)
		}
	}
	c, err := store.GetCall("LIVE1")
	if err != nil {
		t.Fatal(err)
	}
	if c.Outcome != CallAnswered || !c.DurationSeconds.Valid || c.DurationSeconds.Int64 != 300 || c.EndReason != acceptedElsewhere {
		t.Errorf("LIVE1 = %+v", c)
	}
	if _, err := store.GetCall("LOGMSG1"); err != sql.ErrNoRows {
		t.Errorf("LOGMSG1 should have merged into LIVE1, got err %v", err)
	}
	if other, err := store.GetCall("LOGMSG2"); err != nil || other.Outcome != CallMissed {
		t.Errorf("LOGMSG2 = %+v, %v", other, err)
	}
}

func TestCallSeedFromPseudoMessages(t *testing.T) {
	store := newTestMessageStore(t)
	ts := time.Date(2026, 9, 1, 18, 0, 0, 0, time.UTC)
	for _, r := range []struct{ id, chat, sender, content string }{
		{"call-AAA", testPhoneChat, "4915112345678", "📞 Missed call from Max"},
		{"call-BBB", testPhoneChat, "4915112345678", "📞 Call with Max (1:23)"},
		{"call-CCC", testGroup, testLIDChat, "📞 Incoming video call from Max"},
	} {
		if _, err := store.db.Exec(`INSERT OR IGNORE INTO chats (jid) VALUES (?)`, r.chat); err != nil {
			t.Fatal(err)
		}
		if err := store.StoreMessage(r.id, r.chat, r.sender, "Max", r.content, ts, false, "call", "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatal(err)
		}
	}
	if err := runMigrations(store.db); err != nil {
		t.Fatal(err)
	}
	want := map[string]struct {
		outcome, caller string
		video, group    bool
	}{
		"AAA": {CallMissed, testPhoneChat, false, false},
		"BBB": {CallUnknown, testPhoneChat, false, false},
		"CCC": {CallUnknown, testLIDChat, true, true},
	}
	for id, w := range want {
		c, err := store.GetCall(id)
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		if c.Outcome != w.outcome || c.CallerJID != w.caller || c.IsVideo != w.video || c.IsGroup != w.group ||
			c.Direction != "incoming" || c.Source != "message" || !c.StartedAt.Equal(ts) {
			t.Errorf("%s = %+v", id, c)
		}
	}
}
