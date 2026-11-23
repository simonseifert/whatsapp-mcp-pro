package whatsapp

import (
	"context"
	"time"

	"go.mau.fi/whatsmeow/appstate"

	"whatsapp-bridge/internal/database"
)

const (
	appStateFullSyncKey     = "appstate_full_sync_v1"
	readTrackingSinceKey    = "read_tracking_since"
	appStateFullSyncTimeout = 2 * time.Minute
)

// EnsureReadTrackingSince records, once, since when own-device read receipts
// are stored.
func (c *Client) EnsureReadTrackingSince(store *database.MessageStore) {
	v, err := store.GetBridgeState(readTrackingSinceKey)
	if err != nil {
		c.logger.Warnf("read_tracking_since: %v", err)
		return
	}
	if v != "" {
		return
	}
	if err := store.SetBridgeState(readTrackingSinceKey, time.Now().UTC().Format(time.RFC3339)); err != nil {
		c.logger.Warnf("read_tracking_since: %v", err)
	}
}

// StartAppStateFullSync runs a one-time full app state fetch in the
// background, so pin, mute, archive and read state of chats paired before the
// chat_state handlers existed gets replayed as events. It does nothing once
// appstate_full_sync_v1 is set, and never runs twice at the same time.
func (c *Client) StartAppStateFullSync(store *database.MessageStore) {
	if done, err := store.GetBridgeState(appStateFullSyncKey); err != nil || done != "" {
		if err != nil {
			c.logger.Warnf("[APPSTATE] cannot read %s: %v", appStateFullSyncKey, err)
		}
		return
	}
	if !c.appStateSyncing.CompareAndSwap(false, true) {
		return
	}
	go func() {
		defer c.appStateSyncing.Store(false)
		allOK := true
		for _, name := range appstate.AllPatchNames {
			ctx, cancel := context.WithTimeout(context.Background(), appStateFullSyncTimeout)
			err := c.FetchAppState(ctx, name, true, false)
			cancel()
			if err != nil {
				allOK = false
				c.logger.Warnf("[APPSTATE] full sync of %s failed: %v", name, err)
				continue
			}
			c.logger.Infof("[APPSTATE] full sync of %s done", name)
		}
		if !allOK {
			return // retried on the next connect
		}
		if err := store.SetBridgeState(appStateFullSyncKey, time.Now().UTC().Format(time.RFC3339)); err != nil {
			c.logger.Warnf("[APPSTATE] cannot persist %s: %v", appStateFullSyncKey, err)
		}
	}()
}
