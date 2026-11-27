package whatsapp

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestHistorySenderJID(t *testing.T) {
	c := &Client{}
	tests := []struct {
		name, chat, sender, want string
	}{
		{"full jid kept", "120363@g.us", "38591@s.whatsapp.net", "38591@s.whatsapp.net"},
		{"bare phone gets user server", "120363@g.us", "38591", "38591@s.whatsapp.net"},
		{"empty sender in dm is the other side", "38591@s.whatsapp.net", "", "38591@s.whatsapp.net"},
		{"empty sender in group stays empty", "120363@g.us", "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := c.historySenderJID(tt.chat, tt.sender); got != tt.want {
				t.Errorf("historySenderJID(%q, %q) = %q, want %q", tt.chat, tt.sender, got, tt.want)
			}
		})
	}
}

func withBackfillJobs(t *testing.T, jobs ...*BackfillStatus) {
	t.Helper()
	backfillMu.Lock()
	saved := backfillJobs
	backfillJobs = map[string]*BackfillStatus{}
	for _, j := range jobs {
		backfillJobs[j.ChatJID] = j
	}
	backfillMu.Unlock()
	t.Cleanup(func() {
		backfillMu.Lock()
		backfillJobs = saved
		backfillMu.Unlock()
	})
}

func TestNoteBackfillAnswerMatchesEitherForm(t *testing.T) {
	a := &BackfillStatus{ChatJID: "120363000000000001@g.us", Running: true}
	b := &BackfillStatus{ChatJID: "385910000001@s.whatsapp.net", Running: true}
	withBackfillJobs(t, a, b)

	noteBackfillAnswer("999@lid", "385910000001@s.whatsapp.net")
	if a.Answers != 0 || b.Answers != 1 {
		t.Errorf("answers a=%d b=%d, want 0 and 1", a.Answers, b.Answers)
	}
	noteBackfillAnswer("120363000000000001@g.us")
	if a.Answers != 1 || b.Answers != 1 {
		t.Errorf("answers a=%d b=%d, want 1 and 1", a.Answers, b.Answers)
	}
}

func TestNoteBackfillAnswerSingleRunningJobFallback(t *testing.T) {
	running := &BackfillStatus{ChatJID: "385910000001@s.whatsapp.net", Running: true}
	finished := &BackfillStatus{ChatJID: "385910000002@s.whatsapp.net", Running: false}
	withBackfillJobs(t, running, finished)

	noteBackfillAnswer("999@lid", "385910000009@s.whatsapp.net")
	if running.Answers != 1 {
		t.Errorf("single running job answers = %d, want 1", running.Answers)
	}
	if finished.Answers != 0 {
		t.Errorf("finished job answers = %d, want 0", finished.Answers)
	}
}

func TestNoteBackfillAnswerNoFallbackWithSeveralRunning(t *testing.T) {
	a := &BackfillStatus{ChatJID: "385910000001@s.whatsapp.net", Running: true}
	b := &BackfillStatus{ChatJID: "385910000002@s.whatsapp.net", Running: true}
	withBackfillJobs(t, a, b)

	noteBackfillAnswer("385910000009@s.whatsapp.net")
	if a.Answers != 0 || b.Answers != 0 {
		t.Errorf("answers a=%d b=%d, want 0 and 0", a.Answers, b.Answers)
	}
}

func TestNoteBackfillAnswerIgnoresFinishedJobs(t *testing.T) {
	done := &BackfillStatus{ChatJID: "385910000001@s.whatsapp.net", Running: false}
	withBackfillJobs(t, done)

	noteBackfillAnswer("385910000001@s.whatsapp.net")
	if done.Answers != 0 {
		t.Errorf("finished job answers = %d, want 0", done.Answers)
	}
}

func TestBackfillStatusJSONHasAnswers(t *testing.T) {
	out, err := json.Marshal(BackfillStatus{Answers: 2})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"answers":2`) {
		t.Errorf("json = %s, want answers field", out)
	}
}
