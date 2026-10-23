package whatsapp

import (
	"testing"

	waLog "go.mau.fi/whatsmeow/util/log"
)

func TestGuardHandlerRecoversPanics(t *testing.T) {
	calls := 0
	h := guardHandler(waLog.Noop, func(evt any) {
		calls++
		if evt == "boom" {
			panic("malformed event")
		}
	})

	h("boom") // must not propagate
	h("fine")

	if calls != 2 {
		t.Fatalf("handler calls = %d, want 2", calls)
	}
}
