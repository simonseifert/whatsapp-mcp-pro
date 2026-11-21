package database

import (
	"reflect"
	"testing"
	"time"
)

func TestReactions(t *testing.T) {
	const chat = "123@s.whatsapp.net"
	t0 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	r := func(target, sender, emoji string, min int) Reaction {
		return Reaction{ChatJID: chat, TargetID: target, Sender: sender, Emoji: emoji, At: t0.Add(time.Duration(min) * time.Minute)}
	}
	tests := []struct {
		name   string
		events []Reaction
		target string
		want   map[string]int
	}{
		{"two reactors", []Reaction{r("M1", "a", "👍", 1), r("M1", "b", "❤️", 2)}, "M1", map[string]int{"👍": 1, "❤️": 1}},
		{"change emoji", []Reaction{r("M1", "a", "👍", 1), r("M1", "b", "❤️", 2), r("M1", "a", "😂", 3)}, "M1", map[string]int{"😂": 1, "❤️": 1}},
		{"empty removes", []Reaction{r("M1", "a", "👍", 1), r("M1", "b", "❤️", 2), r("M1", "a", "", 3)}, "M1", map[string]int{"❤️": 1}},
		{"older event ignored", []Reaction{r("M1", "b", "❤️", 5), r("M1", "b", "👎", 2)}, "M1", map[string]int{"❤️": 1}},
		{"older removal ignored", []Reaction{r("M1", "b", "❤️", 5), r("M1", "b", "", 2)}, "M1", map[string]int{"❤️": 1}},
		{"target not stored", []Reaction{r("M9", "a", "👍", 1)}, "M9", map[string]int{"👍": 1}},
		{"no reactions", nil, "M7", map[string]int{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := newTestMessageStore(t)
			for _, e := range tt.events {
				if err := store.StoreReaction(e); err != nil {
					t.Fatalf("StoreReaction: %v", err)
				}
			}
			got, err := store.GetMessageReactionSummary(chat, tt.target)
			if err != nil {
				t.Fatalf("summary: %v", err)
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}

// Once a reactor is canonicalised, every event from that person uses the same
// key, so a later one replaces the earlier and the count stays 1. (The LID to
// phone mapping itself is tested in internal/whatsapp.)
func TestReactionSameCanonicalKeyReplaces(t *testing.T) {
	store := newTestMessageStore(t)
	const chat, pn = "g@g.us", "123@s.whatsapp.net"
	t0 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	for _, r := range []Reaction{
		{ChatJID: chat, TargetID: "M1", Sender: pn, Emoji: "👍", At: t0},
		{ChatJID: chat, TargetID: "M1", Sender: pn, Emoji: "😂", At: t0.Add(time.Minute)},
	} {
		if err := store.StoreReaction(r); err != nil {
			t.Fatal(err)
		}
	}
	got, err := store.GetMessageReactionSummary(chat, "M1")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, map[string]int{"😂": 1}) {
		t.Errorf("got %v, want {😂:1}", got)
	}
}

// A non-UTC time must order against UTC ones by instant, not by text.
func TestReactionOrderingAcrossTimeZones(t *testing.T) {
	store := newTestMessageStore(t)
	const chat = "123@s.whatsapp.net"
	plus5 := time.FixedZone("plus5", 5*3600)
	// 12:00+05:00 is 07:00 UTC; as text it sorts after "10:00Z" though it is earlier.
	earlier := time.Date(2026, 11, 1, 12, 0, 0, 0, plus5)
	later := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	for _, r := range []Reaction{
		{ChatJID: chat, TargetID: "M1", Sender: "a", Emoji: "❤️", At: later},
		{ChatJID: chat, TargetID: "M1", Sender: "a", Emoji: "👍", At: earlier},
	} {
		if err := store.StoreReaction(r); err != nil {
			t.Fatal(err)
		}
	}
	got, err := store.GetMessageReactionSummary(chat, "M1")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, map[string]int{"❤️": 1}) {
		t.Errorf("got %v, want {❤️:1}", got)
	}
}
