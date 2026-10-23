package antiban

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"os"
	"sync"
	"time"
)

const (
	warmUpDailyCap        = 680
	warmUpDateLayout      = "2006-01-02"
	warmUpInactivityReset = 72 * time.Hour
)

// WarmUpState is the persisted warm-up progress.
type WarmUpState struct {
	StartDate    time.Time      `json:"startDate"`
	DaySent      map[string]int `json:"daySent"`
	LastSendTime time.Time      `json:"lastSendTime"`
}

// WarmUp ramps the daily send limit for a new account.
type WarmUp struct {
	cfg   *Config
	state *WarmUpState
	mu    sync.Mutex
}

// NewWarmUp loads the warm-up state. It fails safe and never returns an error
// for bad state: an unreadable or implausible file used to make the caller
// drop antiban altogether, so a corrupt JSON file meant no protection at all.
// Now the file is moved aside, logged loudly, and warm-up restarts at day 1,
// the most restrictive limit. The only error left is an empty state path.
func NewWarmUp(cfg *Config) (*WarmUp, error) {
	if cfg == nil {
		cfg = LoadConfig()
	}
	if cfg.WarmUpStatePath == "" {
		return nil, errors.New("warm-up state path is empty")
	}

	now := time.Now()

	state, err := loadState(cfg.WarmUpStatePath)
	switch {
	case err == nil:
		if state.StartDate.IsZero() {
			state.StartDate = now
		}
		if !state.LastSendTime.IsZero() && now.Sub(state.LastSendTime) > warmUpInactivityReset {
			state = freshWarmUpState(now)
		}
	case errors.Is(err, os.ErrNotExist):
		state = freshWarmUpState(now)
	default:
		moved := quarantineStateFile(cfg.WarmUpStatePath, now)
		log.Printf("ANTIBAN ERROR warm-up state %s is unreadable (%v); moved to %q. Failing safe: warm-up restarts at day 1 (limit %d/day)",
			cfg.WarmUpStatePath, err, moved, max(cfg.WarmUpStartLimit, 0))
		state = freshWarmUpState(now)
	}

	return &WarmUp{
		cfg:   cfg,
		state: state,
	}, nil
}

func (w *WarmUp) DailyLimit() int {
	w.mu.Lock()
	defer w.mu.Unlock()

	now := time.Now()
	w.ensureStateLocked(now)

	return w.dailyLimitLocked(now)
}

func (w *WarmUp) CanSend() (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	now := time.Now()
	w.ensureStateLocked(now)

	today := todayKey(now)
	return w.state.DaySent[today] < w.dailyLimitLocked(now), nil
}

func (w *WarmUp) RecordSend() {
	w.mu.Lock()
	now := time.Now()
	w.ensureStateLocked(now)

	today := todayKey(now)
	w.state.DaySent[today]++
	w.state.LastSendTime = now
	w.mu.Unlock()

	if err := w.saveState(); err != nil {
		// The in-memory count still limits this process; only a restart would
		// forget today's sends.
		log.Printf("ANTIBAN WARN could not persist warm-up state: %v", err)
	}
}

func (w *WarmUp) Day() int {
	w.mu.Lock()
	defer w.mu.Unlock()

	now := time.Now()
	w.ensureStateLocked(now)

	return w.dayLocked(now)
}

func (w *WarmUp) TodaySent() int {
	w.mu.Lock()
	defer w.mu.Unlock()

	now := time.Now()
	w.ensureStateLocked(now)

	return w.state.DaySent[todayKey(now)]
}

func (w *WarmUp) saveState() error {
	if w == nil || w.cfg == nil {
		return nil
	}
	if w.cfg.WarmUpStatePath == "" {
		return errors.New("warm-up state path is empty")
	}

	w.mu.Lock()
	snapshot := w.snapshotStateLocked()
	path := w.cfg.WarmUpStatePath
	w.mu.Unlock()

	return writeJSONAtomic(path, snapshot)
}

func loadState(path string) (*WarmUpState, error) {
	if path == "" {
		return nil, errors.New("warm-up state path is empty")
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var state WarmUpState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, err
	}
	if err := state.validate(time.Now()); err != nil {
		return nil, err
	}

	if state.DaySent == nil {
		state.DaySent = make(map[string]int)
	}

	return &state, nil
}

// validate rejects state that parses but would loosen the limit: a negative
// count adds headroom to today's cap, and a start date in the future is not
// something this code ever writes.
func (s *WarmUpState) validate(now time.Time) error {
	skew := now.Add(time.Hour)
	if s.StartDate.After(skew) {
		return fmt.Errorf("startDate %s is in the future", s.StartDate.Format(time.RFC3339))
	}
	if s.LastSendTime.After(skew) {
		return fmt.Errorf("lastSendTime %s is in the future", s.LastSendTime.Format(time.RFC3339))
	}
	for day, n := range s.DaySent {
		if n < 0 {
			return fmt.Errorf("daySent[%s] is negative (%d)", day, n)
		}
	}
	return nil
}

func (w *WarmUp) dailyLimitLocked(now time.Time) int {
	if w.cfg == nil || w.cfg.WarmUpDays <= 0 {
		return math.MaxInt32
	}

	day := w.dayLocked(now)
	if day >= w.cfg.WarmUpDays {
		return math.MaxInt32
	}

	startLimit := w.cfg.WarmUpStartLimit
	if startLimit < 0 {
		startLimit = 0
	}
	if startLimit > warmUpDailyCap {
		startLimit = warmUpDailyCap
	}
	if w.cfg.WarmUpDays == 1 {
		return startLimit
	}

	// The last configured warm-up day must hit the capped limit before the unlimited branch starts.
	limit := startLimit + day*(warmUpDailyCap-startLimit)/(w.cfg.WarmUpDays-1)
	if limit > warmUpDailyCap {
		return warmUpDailyCap
	}

	return limit
}

func (w *WarmUp) dayLocked(now time.Time) int {
	if w.state == nil || w.state.StartDate.IsZero() {
		return 0
	}

	day := int(now.Sub(w.state.StartDate) / (24 * time.Hour))
	if day < 0 {
		return 0
	}

	return day
}

func (w *WarmUp) ensureStateLocked(now time.Time) {
	if w.state == nil {
		w.state = freshWarmUpState(now)
		return
	}

	if w.state.StartDate.IsZero() {
		w.state.StartDate = now
	}
	if w.state.DaySent == nil {
		w.state.DaySent = make(map[string]int)
	}

	// Runtime inactivity reset: if no sends for >72h, restart warm-up.
	if !w.state.LastSendTime.IsZero() && now.Sub(w.state.LastSendTime) > warmUpInactivityReset {
		w.state = freshWarmUpState(now)
	}
}

func (w *WarmUp) snapshotStateLocked() *WarmUpState {
	w.ensureStateLocked(time.Now())

	// Copy the map before unlocking so JSON marshaling cannot race a later send update.
	daySent := make(map[string]int, len(w.state.DaySent))
	for day, count := range w.state.DaySent {
		daySent[day] = count
	}

	return &WarmUpState{
		StartDate:    w.state.StartDate,
		DaySent:      daySent,
		LastSendTime: w.state.LastSendTime,
	}
}

func freshWarmUpState(now time.Time) *WarmUpState {
	return &WarmUpState{
		StartDate: now,
		DaySent:   make(map[string]int),
	}
}

func todayKey(now time.Time) string {
	return now.Format(warmUpDateLayout)
}
