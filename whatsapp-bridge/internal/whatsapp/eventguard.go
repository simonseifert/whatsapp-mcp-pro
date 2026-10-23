package whatsapp

import (
	"runtime/debug"

	"go.mau.fi/whatsmeow"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// AddEventHandler registers handler like whatsmeow's, but a panic inside it is
// logged and swallowed. whatsmeow calls handlers on its own goroutine without a
// recover (upstream issue #1272), so one malformed message hitting a nil
// pointer in any of our handlers would take the whole bridge down, and systemd
// would restart it straight into the same message on the next history replay.
func (c *Client) AddEventHandler(handler whatsmeow.EventHandler) uint32 {
	return c.Client.AddEventHandler(guardHandler(c.logger, handler))
}

func guardHandler(logger waLog.Logger, handler whatsmeow.EventHandler) whatsmeow.EventHandler {
	return func(evt any) {
		defer func() {
			if r := recover(); r != nil {
				logger.Errorf("[EVENT] handler panicked on %T: %v\n%s", evt, r, debug.Stack())
			}
		}()
		handler(evt)
	}
}
