// Package antiban paces and bounds outgoing WhatsApp traffic: randomised
// delays, a warm-up ramp for new accounts, a health score that pauses sends,
// and hard send caps.
package antiban

import (
	"context"
	"errors"
	"strings"
)

// ErrWarmUpLimitReached is returned while today's warm-up allowance is spent.
var ErrWarmUpLimitReached = errors.New("antiban: daily warm-up limit reached")

// SendInterceptor wraps every outgoing message. Pacing, warm-up and health
// only run with ANTIBAN_ENABLED; caps run whenever ANTIBAN_CAPS_ENABLED (on by
// default), so turning pacing off does not also remove the ceiling.
type SendInterceptor struct {
	cfg         *Config
	rateLimiter *RateLimiter
	warmUp      *WarmUp
	health      *HealthMonitor
	caps        *SendCaps
}

// NewSendInterceptor builds the interceptor from cfg (environment if nil).
// Bad on-disk state never makes it fail; see NewWarmUp and NewSendCaps.
func NewSendInterceptor(cfg *Config) (*SendInterceptor, error) {
	if cfg == nil {
		cfg = LoadConfig()
	}

	interceptor := &SendInterceptor{cfg: cfg}
	if cfg.CapsEnabled {
		interceptor.caps = NewSendCaps(cfg)
	}
	if !cfg.Enabled {
		return interceptor, nil
	}

	interceptor.rateLimiter = NewRateLimiter(cfg)

	warmUp, err := NewWarmUp(cfg)
	if err != nil {
		return nil, err
	}
	interceptor.warmUp = warmUp
	interceptor.health = NewHealthMonitor(cfg.RiskPauseThreshold)

	return interceptor, nil
}

// BeforeSend blocks until req may go out, or returns why it may not:
// ErrHealthPaused, ErrWarmUpLimitReached, a *CapError, or a context error.
// When it returns nil, req has been counted against the caps, so call it only
// immediately before dispatching.
func (s *SendInterceptor) BeforeSend(ctx context.Context, req SendRequest) error {
	if s == nil {
		return nil
	}
	if s.hasPacing() {
		if s.health.IsPaused() {
			return ErrHealthPaused
		}
		canSend, err := s.warmUp.CanSend()
		if err != nil {
			return err
		}
		if !canSend {
			return ErrWarmUpLimitReached
		}
	}

	// Fail fast before the pacing delay; Reserve re-checks after it because
	// another send may have taken the last slot while this one slept.
	if err := s.caps.Check(req); err != nil {
		return err
	}
	if s.hasPacing() {
		if err := s.rateLimiter.Wait(ctx, req.Type, req.Len); err != nil {
			return err
		}
	}
	return s.caps.Reserve(req)
}

// Precheck reports whether req would be refused right now, without counting
// it or waiting. Senders call it before going online, uploading media or
// showing "typing": an agent looping on refused sends must not keep putting
// presence and typing in front of the contact, which is the pattern the caps
// exist to stop. BeforeSend still decides; this only refuses early.
func (s *SendInterceptor) Precheck(req SendRequest) error {
	if s == nil {
		return nil
	}
	if s.hasPacing() {
		if s.health.IsPaused() {
			return ErrHealthPaused
		}
		canSend, err := s.warmUp.CanSend()
		if err != nil {
			return err
		}
		if !canSend {
			return ErrWarmUpLimitReached
		}
	}
	return s.caps.Check(req)
}

// AfterSend records a successful send.
func (s *SendInterceptor) AfterSend(_ MessageType) {
	if !s.hasPacing() {
		return
	}

	s.warmUp.RecordSend()
	s.health.RecordEvent(EventSendSuccess)
}

// AfterSendFailed feeds a failed send into the health score.
func (s *SendInterceptor) AfterSendFailed(_ MessageType, err error) {
	if !s.hasPacing() {
		return
	}

	if isSendFailed403(err) {
		s.health.RecordEvent(EventSendFailed403)
		return
	}

	s.health.RecordEvent(EventSendFailed)
}

// RecordEvent feeds a connection or protocol event into the health score.
func (s *SendInterceptor) RecordEvent(eventType HealthEventType) {
	if !s.hasPacing() {
		return
	}

	s.health.RecordEvent(eventType)
}

// Close flushes warm-up state and stops the health decay loop.
func (s *SendInterceptor) Close() error {
	if !s.hasPacing() {
		return nil
	}

	err := s.warmUp.saveState()
	s.health.Close()

	return err
}

// Enabled reports whether pacing (ANTIBAN_ENABLED) is on. Callers use it to
// decide on typing indicators; caps apply regardless.
func (s *SendInterceptor) Enabled() bool {
	return s != nil && s.cfg != nil && s.cfg.Enabled
}

func (s *SendInterceptor) hasPacing() bool {
	return s != nil &&
		s.Enabled() &&
		s.rateLimiter != nil &&
		s.warmUp != nil &&
		s.health != nil
}

func isSendFailed403(err error) bool {
	if err == nil {
		return false
	}

	message := err.Error()
	return strings.Contains(message, "403") || strings.Contains(message, "401")
}
