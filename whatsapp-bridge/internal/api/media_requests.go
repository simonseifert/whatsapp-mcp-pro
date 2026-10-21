package api

import (
	"net/http"
	"strconv"

	"whatsapp-bridge/internal/database"
)

// maxMediaRequestsLimit bounds one listing; the queue is paced at a few dozen
// sends an hour, so anything larger is a backlog nobody reads row by row.
const maxMediaRequestsLimit = 1000

// handleMediaRequests handles GET /api/media-requests — read-only visibility
// into the persistent media retry queue.
//
// Query parameters:
//   - state: pending | sent | succeeded | failed (optional; all when omitted)
//   - limit: max rows, default 100, capped at 1000
//
// Response: { success: true, data: { requests: [...], count: int } }, most
// recently updated first.
func (s *Server) handleMediaRequests(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		SendJSONError(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	q := r.URL.Query()
	state := database.MediaRequestState(q.Get("state"))
	if state != "" && !state.Valid() {
		SendJSONError(w, "state must be one of pending, sent, succeeded, failed", http.StatusBadRequest)
		return
	}
	limit := 100
	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n <= 0 {
			SendJSONError(w, "limit must be a positive integer", http.StatusBadRequest)
			return
		}
		limit = min(n, maxMediaRequestsLimit)
	}

	reqs, err := s.messageStore.ListMediaRequests(state, limit)
	if err != nil {
		SendJSONError(w, err.Error(), http.StatusInternalServerError)
		return
	}
	if reqs == nil {
		reqs = []database.MediaRequest{}
	}
	SendJSONSuccess(w, map[string]any{"requests": reqs, "count": len(reqs)}, "")
}
