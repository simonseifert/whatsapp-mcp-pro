package api

import (
	"math"
	"time"
)

// phoneStatusFields converts whatsapp.Client.PhoneStatus into the
// /api/connection fields. Days are rounded to one decimal: thresholds are
// whole days and finer precision only adds noise to health-check output.
func phoneStatusFields(lastSeen time.Time, offline time.Duration, ok bool) (*string, *float64) {
	if !ok {
		return nil, nil
	}
	days := math.Round(offline.Hours()/24*10) / 10
	if lastSeen.IsZero() {
		return nil, &days
	}
	ts := lastSeen.UTC().Format(time.RFC3339)
	return &ts, &days
}
