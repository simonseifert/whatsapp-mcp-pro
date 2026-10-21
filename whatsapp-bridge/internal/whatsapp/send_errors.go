package whatsapp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"

	"whatsapp-bridge/internal/antiban"
	bridgeTypes "whatsapp-bridge/internal/types"
)

// SendErrorKind classifies why a send failed, so the HTTP layer can answer
// with a status that tells the caller whether retrying makes sense. Every
// failure used to be a 500, which callers treated as "retry", and a retry
// after a slow-but-successful send delivered the message twice.
type SendErrorKind string

// Send failure classes. The HTTP mapping lives in the api package.
const (
	SendErrInvalid          SendErrorKind = "invalid_request"   // 400: bad JID, bad media path, unreadable file
	SendErrForbidden        SendErrorKind = "forbidden"         // 403: allowlist / media refused
	SendErrRateLimited      SendErrorKind = "rate_limited"      // 429: antiban pause or warm-up cap
	SendErrReachoutTimelock SendErrorKind = "reachout_timelock" // 429: WhatsApp error 463, new contacts locked
	SendErrNotConnected     SendErrorKind = "not_connected"     // 503: socket down / not logged in
	SendErrUpstream         SendErrorKind = "whatsapp_error"    // 502: WhatsApp rejected or failed the send
	SendErrTimeout          SendErrorKind = "timeout"           // 504: deadline hit or caller went away
)

// SendError is a classified send failure.
type SendError struct {
	Kind SendErrorKind
	Err  error
	// Until is set for SendErrReachoutTimelock: when the restriction is
	// expected to lift.
	Until time.Time
}

// Error implements error.
func (e *SendError) Error() string {
	if e.Err == nil {
		return string(e.Kind)
	}
	return e.Err.Error()
}

// Unwrap exposes the underlying cause to errors.Is / errors.As.
func (e *SendError) Unwrap() error { return e.Err }

func newSendError(kind SendErrorKind, format string, args ...any) *SendError {
	return &SendError{Kind: kind, Err: fmt.Errorf(format, args...)}
}

// reachoutTimelockCode is the error code WhatsApp puts on a message ack when
// the account may not start new chats. whatsmeow surfaces it only as
// fmt.Errorf("%w %d", ErrServerReturnedError, code); there is no typed error.
const reachoutTimelockCode = 463

// IsReachoutTimelockError reports whether err is WhatsApp's error 463.
func IsReachoutTimelockError(err error) bool {
	if err == nil || !errors.Is(err, whatsmeow.ErrServerReturnedError) {
		return false
	}
	return strings.HasSuffix(err.Error(), fmt.Sprintf(" %d", reachoutTimelockCode))
}

// ClassifySendError maps any send-path error to a SendErrorKind. A *SendError
// anywhere in the chain wins; otherwise whatsmeow / antiban / context sentinels
// are recognised, and anything else is an upstream (WhatsApp) failure.
func ClassifySendError(err error) SendErrorKind {
	if err == nil {
		return ""
	}
	var se *SendError
	if errors.As(err, &se) {
		return se.Kind
	}
	switch {
	case IsReachoutTimelockError(err):
		return SendErrReachoutTimelock
	case errors.Is(err, context.DeadlineExceeded),
		errors.Is(err, context.Canceled),
		errors.Is(err, whatsmeow.ErrMessageTimedOut),
		errors.Is(err, whatsmeow.ErrIQTimedOut):
		return SendErrTimeout
	case errors.Is(err, whatsmeow.ErrNotConnected),
		errors.Is(err, whatsmeow.ErrNotLoggedIn),
		errors.Is(err, whatsmeow.ErrClientIsNil):
		return SendErrNotConnected
	case errors.Is(err, antiban.ErrHealthPaused),
		errors.Is(err, antiban.ErrWarmUpLimitReached):
		return SendErrRateLimited
	default:
		return SendErrUpstream
	}
}

// classify wraps err as a *SendError (keeping an existing classification).
func classify(err error, format string) *SendError {
	var se *SendError
	if errors.As(err, &se) {
		return se
	}
	return &SendError{Kind: ClassifySendError(err), Err: fmt.Errorf(format, err)}
}

// failed builds a failed SendResult from a classified error.
func failed(se *SendError, messageID string) bridgeTypes.SendResult {
	return bridgeTypes.SendResult{Success: false, Error: se.Error(), Err: se, MessageID: messageID}
}
