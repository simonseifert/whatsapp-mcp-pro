package database

import (
	"testing"
	"time"
)

func TestChatStateNewerWins(t *testing.T) {
	const chat = "123@s.whatsapp.net"
	t1 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	t2, t3 := t1.Add(time.Hour), t1.Add(2*time.Hour)
	t4, t5 := t1.Add(3*time.Hour), t1.Add(4*time.Hour)
	t6, t7 := t1.Add(5*time.Hour), t1.Add(6*time.Hour)

	tests := []struct {
		name  string
		steps func(t *testing.T, s *MessageStore)
		check func(t *testing.T, st ChatState)
	}{
		{"history seed does not undo a pin", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatPinned(chat, true, t2))
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(false, false, false, 0, 1, t1)))
		}, func(t *testing.T, st ChatState) {
			if !st.Pinned {
				t.Error("pinned = false, want true")
			}
		}},
		{"older unpin ignored, newer unpin applies", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatPinned(chat, true, t2))
			must(t, s.SetChatPinned(chat, false, t1))
			st, _ := s.GetChatState(chat)
			if !st.Pinned {
				t.Fatal("older unpin won")
			}
			must(t, s.SetChatPinned(chat, false, t3))
		}, func(t *testing.T, st ChatState) {
			if st.Pinned {
				t.Error("pinned = true after newer unpin")
			}
		}},
		{"mute forever then unmute", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatMuted(chat, -1, t1))
			st, _ := s.GetChatState(chat)
			if st.MutedUntil != -1 {
				t.Fatalf("MutedUntil = %d, want -1", st.MutedUntil)
			}
			must(t, s.SetChatMuted(chat, 0, t2))
		}, func(t *testing.T, st ChatState) {
			if st.MutedUntil != 0 {
				t.Errorf("MutedUntil = %d, want 0", st.MutedUntil)
			}
		}},
		{"archive newer wins", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatArchived(chat, true, t2))
			must(t, s.SetChatArchived(chat, false, t1))
		}, func(t *testing.T, st ChatState) {
			if !st.Archived {
				t.Error("archived = false, want true")
			}
		}},
		{"read only moves forward", func(t *testing.T, s *MessageStore) {
			must(t, s.MarkChatReadAt(chat, t5))
			must(t, s.MarkChatReadAt(chat, t4))
		}, func(t *testing.T, st ChatState) {
			if st.LastReadAt == nil || !st.LastReadAt.Equal(t5) {
				t.Errorf("LastReadAt = %v, want %v", st.LastReadAt, t5)
			}
		}},
		{"read after marked unread clears it", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatMarkedUnread(chat, true, t6))
			must(t, s.MarkChatReadAt(chat, t7))
		}, func(t *testing.T, st ChatState) {
			if st.MarkedUnread {
				t.Error("MarkedUnread = true, want false")
			}
		}},
		{"read before marked unread keeps it", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatMarkedUnread(chat, true, t6))
			must(t, s.MarkChatReadAt(chat, t5))
		}, func(t *testing.T, st ChatState) {
			if !st.MarkedUnread {
				t.Error("MarkedUnread = false, want true")
			}
		}},
		{"stale marked-unread after a read on a fresh chat is ignored", func(t *testing.T, s *MessageStore) {
			must(t, s.MarkChatReadAt(chat, t7))
			must(t, s.SetChatMarkedUnread(chat, true, t6))
		}, func(t *testing.T, st ChatState) {
			if st.MarkedUnread {
				t.Error("MarkedUnread = true, want false")
			}
		}},
		{"seed last read only moves forward", func(t *testing.T, s *MessageStore) {
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(false, false, false, 0, 0, t3)))
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(false, false, false, 0, 0, t5)))
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(false, false, false, 0, 0, t4)))
		}, func(t *testing.T, st ChatState) {
			if st.LastReadAt == nil || !st.LastReadAt.Equal(t5) {
				t.Errorf("LastReadAt = %v, want %v", st.LastReadAt, t5)
			}
		}},
		{"seed with zero unread sets last read", func(t *testing.T, s *MessageStore) {
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(false, false, false, 0, 0, t3)))
		}, func(t *testing.T, st ChatState) {
			if st.LastReadAt == nil || !st.LastReadAt.Equal(t3) {
				t.Errorf("LastReadAt = %v, want %v", st.LastReadAt, t3)
			}
		}},
		{"seed with unread leaves last read nil", func(t *testing.T, s *MessageStore) {
			must(t, s.SeedChatStateFromHistory(chat, fullSeed(true, true, true, 1900000000, 3, t3)))
		}, func(t *testing.T, st ChatState) {
			if st.LastReadAt != nil {
				t.Errorf("LastReadAt = %v, want nil", st.LastReadAt)
			}
			if st.HistoryUnreadCount == nil || *st.HistoryUnreadCount != 3 {
				t.Errorf("HistoryUnreadCount = %v, want 3", st.HistoryUnreadCount)
			}
			if !st.Pinned || !st.Archived || !st.MarkedUnread || st.MutedUntil != 1900000000 {
				t.Errorf("seed values not stored: %+v", st)
			}
		}},
		{"future event time cannot beat a later real event", func(t *testing.T, s *MessageStore) {
			must(t, s.SetChatPinned(chat, true, time.Now().Add(48*time.Hour)))
			must(t, s.SetChatPinned(chat, false, time.Now().Add(time.Second)))
		}, func(t *testing.T, st ChatState) {
			if st.Pinned {
				t.Error("forged future pin still wins")
			}
		}},
		{"non-UTC times compare correctly", func(t *testing.T, s *MessageStore) {
			plus9 := time.FixedZone("x", 9*3600)
			must(t, s.SetChatPinned(chat, true, t2.In(plus9)))
			must(t, s.SetChatPinned(chat, false, t1))
		}, func(t *testing.T, st ChatState) {
			if !st.Pinned {
				t.Error("older UTC unpin beat a later non-UTC pin")
			}
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s := newTestMessageStore(t)
			tt.steps(t, s)
			st, err := s.GetChatState(chat)
			must(t, err)
			tt.check(t, st)
		})
	}
}

func TestGetChatStateUnknownChat(t *testing.T) {
	s := newTestMessageStore(t)
	st, err := s.GetChatState("999@s.whatsapp.net")
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if st.ChatJID != "" || st.Pinned || st.LastReadAt != nil {
		t.Errorf("want zero ChatState, got %+v", st)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// fullSeed is a seed with every field present, as a first full history sync
// delivers it.
func fullSeed(pinned, archived, markedUnread bool, mutedUntil int64, unread int, latest time.Time) ChatSeed {
	return ChatSeed{Pinned: &pinned, Archived: &archived, MarkedUnread: &markedUnread,
		MutedUntil: &mutedUntil, UnreadCount: &unread, LatestMsg: latest}
}

// A partial conversation (fields absent from the proto) must change nothing:
// the old getters turned a missing unread count into 0, i.e. "fully read".
func TestSeedChatStateIgnoresAbsentFields(t *testing.T) {
	const chat = "123@s.whatsapp.net"
	t1 := time.Date(2026, 11, 1, 10, 0, 0, 0, time.UTC)
	s := newTestMessageStore(t)
	must(t, s.SeedChatStateFromHistory(chat, fullSeed(true, true, true, 1900000000, 4, t1)))

	// Nothing set: no field, no read mark, no unread count may move.
	must(t, s.SeedChatStateFromHistory(chat, ChatSeed{LatestMsg: t1.Add(time.Hour)}))
	st, err := s.GetChatState(chat)
	must(t, err)
	if !st.Pinned || !st.Archived || !st.MarkedUnread || st.MutedUntil != 1900000000 {
		t.Errorf("absent fields overwrote seeded state: %+v", st)
	}
	if st.LastReadAt != nil {
		t.Errorf("LastReadAt = %v, want nil: absent unread count is not 'read'", st.LastReadAt)
	}
	if st.HistoryUnreadCount == nil || *st.HistoryUnreadCount != 4 {
		t.Errorf("HistoryUnreadCount = %v, want 4", st.HistoryUnreadCount)
	}

	// A chat never seen: an empty seed creates no meaningful state.
	must(t, s.SeedChatStateFromHistory("456@s.whatsapp.net", ChatSeed{LatestMsg: t1}))
	st, err = s.GetChatState("456@s.whatsapp.net")
	must(t, err)
	if st.LastReadAt != nil || st.HistoryUnreadCount != nil || st.Pinned || st.Archived || st.MarkedUnread || st.MutedUntil != 0 {
		t.Errorf("empty seed produced state: %+v", st)
	}

	// Present fields do apply, each on its own.
	unpinned := false
	must(t, s.SeedChatStateFromHistory(chat, ChatSeed{Pinned: &unpinned}))
	st, _ = s.GetChatState(chat)
	if st.Pinned || !st.Archived {
		t.Errorf("a pinned-only seed changed more than pinned: %+v", st)
	}
}
