package antiban

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// MessageType tells pacing and caps what kind of protocol message is going out.
type MessageType int

// Message types. Media sends count as Text.
const (
	Text MessageType = iota
	Reaction
	Edit
	Delete
	Poll
	Peer
)

// Config holds the antiban settings read from the environment.
type Config struct {
	// Enabled turns on pacing, warm-up and the health monitor. Opt-in, because
	// pacing adds seconds to every send.
	Enabled            bool
	TextDelayMin       time.Duration
	TextDelayMax       time.Duration
	FeedbackDelayMin   time.Duration
	FeedbackDelayMax   time.Duration
	TypingMsPerChar    int
	WarmUpDays         int
	WarmUpStartLimit   int
	WarmUpStatePath    string
	RiskPauseThreshold int

	// Caps are hard ceilings on send volume. They are independent of Enabled:
	// pacing is opt-in because it costs latency, but a ceiling costs nothing in
	// normal use and is the only thing that stops a runaway agent loop once
	// warm-up has ended (warm-up stops limiting entirely after its last day).
	// A value <= 0 disables that one cap.
	CapsEnabled           bool
	RecipientMaxPerMinute int // text/media/poll to one chat
	RecipientMaxPerHour   int
	GlobalMaxPerHour      int // text/media/poll across all chats
	NewRecipientsPerDay   int // first contacts in a rolling 24h
	FeedbackMaxPerMinute  int // reactions/edits/deletes to one chat
	FeedbackMaxPerHour    int // reactions/edits/deletes across all chats
	CapsStatePath         string
}

// Defaults for the caps. Sized against personal use: an agent replying in a
// handful of chats, plus automations sending tens of messages a day. Any of
// them firing in that pattern would be a sign something is looping.
const (
	// An agent often splits one answer into several bubbles, or is asked to
	// send a dozen photos at once; 15 a minute leaves room for that, and the
	// hourly cap below still stops a loop within minutes.
	DefaultRecipientMaxPerMinute = 15
	// A long back-and-forth with one person stays well under one a minute on
	// average.
	DefaultRecipientMaxPerHour = 60
	// Two a minute sustained across all chats is far beyond a person's pace,
	// but a burst of replies after a quiet morning still fits.
	DefaultGlobalMaxPerHour = 120
	// First contact is the pattern that gets accounts banned. Personal use
	// starts a few new chats a day at most.
	DefaultNewRecipientsPerDay = 10
	// Reactions, edits and deletes ride on existing messages, so they get
	// looser ceilings of their own instead of eating into the message budget.
	DefaultFeedbackMaxPerMinute = 20
	DefaultFeedbackMaxPerHour   = 300
)

// LoadConfig reads the antiban settings from the environment. Unparseable
// values fall back to the default rather than disabling anything.
func LoadConfig() *Config {
	return &Config{
		Enabled:            loadBoolEnv("ANTIBAN_ENABLED", false),
		TextDelayMin:       loadDurationEnv("ANTIBAN_TEXT_DELAY_MIN", 1500*time.Millisecond),
		TextDelayMax:       loadDurationEnv("ANTIBAN_TEXT_DELAY_MAX", 4*time.Second),
		FeedbackDelayMin:   loadDurationEnv("ANTIBAN_FEEDBACK_DELAY_MIN", 500*time.Millisecond),
		FeedbackDelayMax:   loadDurationEnv("ANTIBAN_FEEDBACK_DELAY_MAX", 1500*time.Millisecond),
		TypingMsPerChar:    loadIntEnv("ANTIBAN_TYPING_MS_PER_CHAR", 30),
		WarmUpDays:         loadIntEnv("ANTIBAN_WARMUP_DAYS", 7),
		WarmUpStartLimit:   loadIntEnv("ANTIBAN_WARMUP_START_LIMIT", 20),
		WarmUpStatePath:    loadStringEnv("ANTIBAN_WARMUP_STATE_PATH", "store/antiban_warmup.json"),
		RiskPauseThreshold: loadIntEnv("ANTIBAN_RISK_PAUSE_THRESHOLD", 70),

		CapsEnabled:           loadBoolEnv("ANTIBAN_CAPS_ENABLED", true),
		RecipientMaxPerMinute: loadIntEnv("ANTIBAN_RECIPIENT_MAX_PER_MINUTE", DefaultRecipientMaxPerMinute),
		RecipientMaxPerHour:   loadIntEnv("ANTIBAN_RECIPIENT_MAX_PER_HOUR", DefaultRecipientMaxPerHour),
		GlobalMaxPerHour:      loadIntEnv("ANTIBAN_GLOBAL_MAX_PER_HOUR", DefaultGlobalMaxPerHour),
		NewRecipientsPerDay:   loadIntEnv("ANTIBAN_NEW_RECIPIENTS_PER_DAY", DefaultNewRecipientsPerDay),
		FeedbackMaxPerMinute:  loadIntEnv("ANTIBAN_FEEDBACK_MAX_PER_MINUTE", DefaultFeedbackMaxPerMinute),
		FeedbackMaxPerHour:    loadIntEnv("ANTIBAN_FEEDBACK_MAX_PER_HOUR", DefaultFeedbackMaxPerHour),
		CapsStatePath:         loadStringEnv("ANTIBAN_CAPS_STATE_PATH", "store/antiban_caps.json"),
	}
}

// sendClass groups message types that share a cap budget.
type sendClass int

const (
	classMessage  sendClass = iota // puts a new bubble in the chat
	classFeedback                  // modifies or annotates an existing one
	classExempt                    // never reaches another person's chat
)

func (m MessageType) class() sendClass {
	switch m {
	case Reaction, Edit, Delete:
		return classFeedback
	case Peer:
		// History requests go to the account's own phone; nobody else sees them.
		return classExempt
	default:
		return classMessage
	}
}

// String returns the lower-case name used in logs.
func (m MessageType) String() string {
	switch m {
	case Text:
		return "text"
	case Reaction:
		return "reaction"
	case Edit:
		return "edit"
	case Delete:
		return "delete"
	case Poll:
		return "poll"
	case Peer:
		return "peer"
	default:
		return fmt.Sprintf("unknown(%d)", int(m))
	}
}

func loadBoolEnv(key string, fallback bool) bool {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func loadIntEnv(key string, fallback int) int {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func loadDurationEnv(key string, fallback time.Duration) time.Duration {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func loadStringEnv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}
