package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"whatsapp-bridge/internal/database"
)

func TestHandleMediaRequests(t *testing.T) {
	store, err := database.OpenMessageStore("file:TestHandleMediaRequests?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	if _, err := store.EnqueueMediaRequest("P1", "1@s.whatsapp.net", now); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkMediaRequestSent("S1", "1@s.whatsapp.net", now); err != nil {
		t.Fatal(err)
	}
	s := &Server{messageStore: store}

	tests := []struct {
		name, method, query string
		wantCode, wantCount int
	}{
		{"pending only", http.MethodGet, "?state=pending", http.StatusOK, 1},
		{"all states", http.MethodGet, "", http.StatusOK, 2},
		{"limit", http.MethodGet, "?limit=1", http.StatusOK, 1},
		{"empty state list", http.MethodGet, "?state=failed", http.StatusOK, 0},
		{"bad state", http.MethodGet, "?state=bogus", http.StatusBadRequest, 0},
		{"bad limit", http.MethodGet, "?limit=-3", http.StatusBadRequest, 0},
		{"read only", http.MethodPost, "", http.StatusMethodNotAllowed, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			s.handleMediaRequests(rec, httptest.NewRequest(tt.method, "/api/media-requests"+tt.query, nil))
			if rec.Code != tt.wantCode {
				t.Fatalf("code = %d, want %d: %s", rec.Code, tt.wantCode, rec.Body)
			}
			if tt.wantCode != http.StatusOK {
				return
			}
			var resp struct {
				Data struct {
					Requests []database.MediaRequest `json:"requests"`
					Count    int                     `json:"count"`
				} `json:"data"`
			}
			if err := json.NewDecoder(strings.NewReader(rec.Body.String())).Decode(&resp); err != nil {
				t.Fatal(err)
			}
			if resp.Data.Count != tt.wantCount || len(resp.Data.Requests) != tt.wantCount {
				t.Errorf("count = %d (%d rows), want %d", resp.Data.Count, len(resp.Data.Requests), tt.wantCount)
			}
		})
	}
}
