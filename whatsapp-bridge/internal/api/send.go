package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/types"
	"whatsapp-bridge/internal/whatsapp"
)

// Message-creating endpoints: /api/send (text, files, voice notes),
// /api/reaction and /api/poll/create.
//
// Two things make them safe to retry:
//
//  1. Idempotency keys. A caller may pass idempotency_key (body) or an
//     Idempotency-Key header. The first request with a key claims it; a repeat
//     while it runs gets 409, a repeat after success gets the stored response
//     (header Idempotent-Replay: true) and nothing is sent, a repeat after a
//     failure tries again. A key reused with a different payload gets 422. Keys
//     are scoped to endpoint + recipient and expire after 24h.
//
//  2. Honest status codes. Every failure used to be a 500, which callers read
//     as "transient, retry", so a send that was merely slow got delivered
//     twice. Now: 400 bad input, 403 refused (allowlist / media path), 409
//     duplicate in flight, 422 key/payload mismatch, 429 rate-limited / antiban
//     / reachout timelock, 502 WhatsApp error, 503 not connected, 504 timeout.
//
// The send runs on the request context bounded by SEND_TIMEOUT_SECONDS, so a
// caller that disconnects cancels it, and the bridge answers 504 before a
// client with the usual 30s timeout gives up and retries blind.

// sendBackend is the slice of *whatsapp.Client these handlers use; an
// interface so handler tests can run without a WhatsApp connection.
type sendBackend interface {
	SendMessageWithOptions(ctx context.Context, store *database.MessageStore, opts whatsapp.SendOptions) types.SendResult
	SendReactionWithOptions(ctx context.Context, store *database.MessageStore, opts whatsapp.ReactionOptions) types.SendResult
	CreatePollWithOptions(ctx context.Context, store *database.MessageStore, opts whatsapp.PollOptions) types.SendResult
}

const (
	defaultSendTimeout = 25 * time.Second
	// Media sends upload first; a large video on a slow link takes well over
	// 25s, so they get their own, longer budget (the MCP waits correspondingly).
	defaultMediaSendTimeout = 150 * time.Second
	maxIdempotencyKeyLen    = 255
	idempotencyHeader       = "Idempotency-Key"
	replayHeader            = "Idempotent-Replay"
)

// sendTimeout reads SEND_TIMEOUT_SECONDS. The default (25s) sits under the MCP
// server's 30s HTTP timeout on purpose: the bridge should report the timeout
// (and record the idempotency outcome) before the client abandons the request.
func sendTimeout() time.Duration {
	return envSeconds("SEND_TIMEOUT_SECONDS", defaultSendTimeout)
}

// mediaSendTimeout reads SEND_MEDIA_TIMEOUT_SECONDS (default 150s), the budget
// for sends that carry a file.
func mediaSendTimeout() time.Duration {
	return envSeconds("SEND_MEDIA_TIMEOUT_SECONDS", defaultMediaSendTimeout)
}

func envSeconds(name string, def time.Duration) time.Duration {
	if raw := strings.TrimSpace(os.Getenv(name)); raw != "" {
		if secs, err := strconv.Atoi(raw); err == nil && secs > 0 {
			return time.Duration(secs) * time.Second
		}
	}
	return def
}

// httpStatusForSendError maps a send failure class to its HTTP status.
func httpStatusForSendError(err error) int {
	switch whatsapp.ClassifySendError(err) {
	case whatsapp.SendErrInvalid:
		return http.StatusBadRequest
	case whatsapp.SendErrForbidden:
		return http.StatusForbidden
	case whatsapp.SendErrRateLimited, whatsapp.SendErrReachoutTimelock:
		return http.StatusTooManyRequests
	case whatsapp.SendErrNotConnected:
		return http.StatusServiceUnavailable
	case whatsapp.SendErrUpstream:
		return http.StatusBadGateway
	case whatsapp.SendErrTimeout:
		return http.StatusGatewayTimeout
	default:
		return http.StatusInternalServerError
	}
}

// retryAfterOf returns the time a reachout-timelocked send may be retried.
func retryAfterOf(err error) time.Time {
	var se *whatsapp.SendError
	if errors.As(err, &se) {
		return se.Until
	}
	return time.Time{}
}

// sendOutcome is what a send attempt produced, before it is written out.
type sendOutcome struct {
	status     int
	body       any
	messageID  string
	retryAfter time.Time
}

func failureOutcome(res types.SendResult, body func(code, msg string) any) sendOutcome {
	code := string(whatsapp.ClassifySendError(res.Err))
	return sendOutcome{
		status:     httpStatusForSendError(res.Err),
		body:       body(code, res.Error),
		messageID:  res.MessageID,
		retryAfter: retryAfterOf(res.Err),
	}
}

// payloadHash fingerprints the request minus its idempotency key, so a key
// reused for a different message is caught.
func payloadHash(v any) string {
	b, _ := json.Marshal(v)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// idempotencyKey prefers the body field and falls back to the header.
func idempotencyKey(r *http.Request, bodyKey string) string {
	if k := strings.TrimSpace(bodyKey); k != "" {
		return k
	}
	return strings.TrimSpace(r.Header.Get(idempotencyHeader))
}

func writeJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
	_, _ = w.Write([]byte("\n"))
}

func writeOutcome(w http.ResponseWriter, out sendOutcome, body []byte) {
	if !out.retryAfter.IsZero() {
		secs := int(math.Ceil(time.Until(out.retryAfter).Seconds()))
		if secs > 0 {
			w.Header().Set("Retry-After", strconv.Itoa(secs))
		}
	}
	writeJSON(w, out.status, body)
}

// runIdempotentSend wraps one send attempt with idempotency handling.
// payload must exclude the idempotency key itself. do receives the WhatsApp
// message ID of an earlier failed attempt (to reuse) and a hook to persist the
// ID it is about to send with.
func (s *Server) runIdempotentSend(
	w http.ResponseWriter, r *http.Request,
	endpoint, recipient, key string, payload any, timeout time.Duration,
	do func(ctx context.Context, priorMessageID string, beforeDispatch func(string)) sendOutcome,
) {
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()

	if key == "" {
		out := do(ctx, "", nil)
		body, _ := json.Marshal(out.body)
		writeOutcome(w, out, body)
		return
	}
	if len(key) > maxIdempotencyKeyLen {
		SendJSONError(w, fmt.Sprintf("idempotency_key longer than %d characters", maxIdempotencyKeyLen), http.StatusBadRequest)
		return
	}

	scope := endpoint + "|" + recipient
	hash := payloadHash(payload)
	rec, proceed, err := s.messageStore.ClaimIdempotencyKey(key, scope, hash, time.Now())
	if err != nil {
		SendJSONError(w, fmt.Sprintf("idempotency store error: %v", err), http.StatusInternalServerError)
		return
	}
	if !proceed {
		switch {
		case rec.PayloadHash != hash:
			SendJSONError(w, "idempotency_key was already used for a different payload to this recipient; use a new key", http.StatusUnprocessableEntity)
		case rec.Status == database.IdempotencyDone:
			writeReplay(w, rec)
		default:
			SendJSONError(w, "a request with this idempotency_key is still being sent; wait for it instead of retrying", http.StatusConflict)
		}
		return
	}

	beforeDispatch := func(messageID string) {
		if err := s.messageStore.SetIdempotencyMessageID(key, scope, messageID); err != nil {
			log.Printf("WARN idempotency: could not store message id for key %q: %v", key, err)
		}
	}
	out := do(ctx, rec.MessageID, beforeDispatch)
	body, _ := json.Marshal(out.body)

	status := database.IdempotencyFailed
	if out.status >= 200 && out.status < 300 {
		status = database.IdempotencyDone
	}
	if err := s.messageStore.FinishIdempotencyKey(key, scope, status, out.messageID, out.status, body, time.Now()); err != nil {
		log.Printf("WARN idempotency: could not record outcome for key %q: %v", key, err)
	}
	writeOutcome(w, out, body)
}

// writeReplay answers with the stored response of a completed send.
func writeReplay(w http.ResponseWriter, rec *database.IdempotencyRecord) {
	body := rec.Response
	var m map[string]any
	if json.Unmarshal(body, &m) == nil {
		m["idempotent_replay"] = true
		if b, err := json.Marshal(m); err == nil {
			body = b
		}
	}
	status := rec.HTTPStatus
	if status == 0 {
		status = http.StatusOK
	}
	w.Header().Set(replayHeader, "true")
	writeJSON(w, status, body)
}

// handleSendMessage handles POST /api/send for sending WhatsApp messages.
//
// Request body:
//   - recipient: WhatsApp JID (required, e.g., "1234567890@s.whatsapp.net")
//   - message: Text content (required if media_path not provided)
//   - media_path: Path to media file (optional, for images/videos/documents/voice)
//   - quoted_message_id, mentioned_jids: optional
//   - idempotency_key: optional (or Idempotency-Key header), see top of file
//
// Response: { success, message_id, timestamp, recipient, message (error text),
// code (failure class), retry_after (RFC 3339, on 429 timelock) }
func (s *Server) handleSendMessage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		SendJSONError(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req types.SendMessageRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		SendJSONError(w, "Invalid request format", http.StatusBadRequest)
		return
	}
	if req.Recipient == "" {
		SendJSONError(w, "Recipient is required", http.StatusBadRequest)
		return
	}
	if refuseUnlistedRecipient(w, req.Recipient) {
		return
	}
	if req.Message == "" && req.MediaPath == "" {
		SendJSONError(w, "Message or media path is required", http.StatusBadRequest)
		return
	}

	key := idempotencyKey(r, req.IdempotencyKey)
	payload := req
	payload.IdempotencyKey = ""

	timeout := sendTimeout()
	if req.MediaPath != "" {
		timeout = mediaSendTimeout()
	}
	s.runIdempotentSend(w, r, "send", req.Recipient, key, payload, timeout, func(ctx context.Context, priorID string, beforeDispatch func(string)) sendOutcome {
		// Presence is handled centrally by the client's presence manager.
		res := s.sender.SendMessageWithOptions(ctx, s.messageStore, whatsapp.SendOptions{
			Recipient:       req.Recipient,
			Message:         req.Message,
			MediaPath:       req.MediaPath,
			QuotedMessageID: req.QuotedMessageID,
			MentionedJIDs:   req.MentionedJIDs,
			MessageID:       priorID,
			BeforeDispatch:  beforeDispatch,
		})
		if !res.Success {
			return failureOutcome(res, func(code, msg string) any {
				resp := types.SendMessageResponse{Success: false, Message: msg, Recipient: req.Recipient, Code: code}
				if t := retryAfterOf(res.Err); !t.IsZero() {
					resp.RetryAfter = t.Format(time.RFC3339)
				}
				return resp
			})
		}
		return sendOutcome{
			status:    http.StatusOK,
			messageID: res.MessageID,
			body: types.SendMessageResponse{
				Success:   true,
				MessageID: res.MessageID,
				Timestamp: res.Timestamp,
				Recipient: req.Recipient,
			},
		}
	})
}

// errorBody is the {success:false,error,code} shape the other endpoints use.
func errorBody(code, msg string) any {
	return map[string]any{"success": false, "error": msg, "code": code}
}

// handleReaction handles POST /api/reaction for sending emoji reactions.
//
// Request body:
//   - chat_jid: Chat containing the message (required)
//   - message_id: Target message ID (required)
//   - emoji: Reaction emoji (empty string to remove reaction)
//   - idempotency_key: optional (or Idempotency-Key header)
//
// Response: { success: bool, message: string, message_id: reaction id }
func (s *Server) handleReaction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		SendJSONError(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req types.ReactionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		SendJSONError(w, "Invalid request format", http.StatusBadRequest)
		return
	}
	if req.ChatJID == "" || req.MessageID == "" {
		SendJSONError(w, "chat_jid and message_id are required", http.StatusBadRequest)
		return
	}
	if refuseUnlistedRecipient(w, req.ChatJID) {
		return
	}

	key := idempotencyKey(r, req.IdempotencyKey)
	payload := req
	payload.IdempotencyKey = ""

	s.runIdempotentSend(w, r, "reaction", req.ChatJID, key, payload, sendTimeout(), func(ctx context.Context, priorID string, beforeDispatch func(string)) sendOutcome {
		res := s.sender.SendReactionWithOptions(ctx, s.messageStore, whatsapp.ReactionOptions{
			ChatJID:         req.ChatJID,
			TargetMessageID: req.MessageID,
			Emoji:           req.Emoji,
			MessageID:       priorID,
			BeforeDispatch:  beforeDispatch,
		})
		if !res.Success {
			return failureOutcome(res, func(code, msg string) any {
				return errorBody(code, "Failed to send reaction: "+msg)
			})
		}
		return sendOutcome{
			status:    http.StatusOK,
			messageID: res.MessageID,
			body:      map[string]any{"success": true, "message": "Reaction sent", "message_id": res.MessageID},
		}
	})
}

// handleCreatePoll handles POST /api/poll/create for creating WhatsApp polls.
//
// Request body:
//   - chat_jid: Target chat (required)
//   - question: Poll question (required)
//   - options: Array of 2-12 answer options (required)
//   - multi_select: Allow multiple selections (default false)
//   - idempotency_key: optional (or Idempotency-Key header)
//
// Response: { success, message_id, timestamp, chat_jid, question, options }
func (s *Server) handleCreatePoll(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		SendJSONError(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req types.CreatePollRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		SendJSONError(w, "Invalid request format", http.StatusBadRequest)
		return
	}
	if req.ChatJID == "" || req.Question == "" || len(req.Options) < 2 {
		SendJSONError(w, "chat_jid, question, and at least 2 options are required", http.StatusBadRequest)
		return
	}
	if len(req.Options) > 12 {
		SendJSONError(w, "Maximum 12 options allowed", http.StatusBadRequest)
		return
	}
	if refuseUnlistedRecipient(w, req.ChatJID) {
		return
	}

	key := idempotencyKey(r, req.IdempotencyKey)
	payload := req
	payload.IdempotencyKey = ""

	s.runIdempotentSend(w, r, "poll", req.ChatJID, key, payload, sendTimeout(), func(ctx context.Context, priorID string, beforeDispatch func(string)) sendOutcome {
		res := s.sender.CreatePollWithOptions(ctx, s.messageStore, whatsapp.PollOptions{
			ChatJID:        req.ChatJID,
			Question:       req.Question,
			Options:        req.Options,
			MultiSelect:    req.MultiSelect,
			MessageID:      priorID,
			BeforeDispatch: beforeDispatch,
		})
		if !res.Success {
			return failureOutcome(res, func(code, msg string) any {
				return errorBody(code, "Failed to create poll: "+msg)
			})
		}
		return sendOutcome{
			status:    http.StatusOK,
			messageID: res.MessageID,
			body: map[string]any{
				"success":    true,
				"message_id": res.MessageID,
				"timestamp":  res.Timestamp,
				"chat_jid":   req.ChatJID,
				"question":   req.Question,
				"options":    req.Options,
			},
		}
	})
}
