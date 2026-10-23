package whatsapp

import (
	"context"
	"os"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Group metadata cache.
//
// GetJoinedGroups and GetGroupInfo are IQ queries to WhatsApp's servers, and
// nothing rate-limited them: the identity rebuild calls GetJoinedGroups on
// connect, on every coalesced contact-event burst and on its ticker; every
// incoming message from an unnamed group called GetGroupInfo; /api/group/{jid}
// passed straight through. A linked device hammering group queries is not what
// a phone does. Answers (errors too, so a failing query is not retried in a
// tight loop) are reused for GROUP_METADATA_CACHE_TTL, and concurrent callers
// share one in-flight fetch. WhatsApp's own group-change events drop the
// affected entries, so a rename or membership change is not served stale.

const defaultGroupCacheTTL = 5 * time.Minute

// groupCacheTTL reads GROUP_METADATA_CACHE_TTL (Go duration). "0" turns the
// cache off; unparseable or negative values keep the default.
func groupCacheTTL() time.Duration {
	raw := os.Getenv("GROUP_METADATA_CACHE_TTL")
	if raw == "" {
		return defaultGroupCacheTTL
	}
	d, err := time.ParseDuration(raw)
	if err != nil || d < 0 {
		return defaultGroupCacheTTL
	}
	return d
}

type groupInfoEntry struct {
	info *types.GroupInfo
	err  error
	at   time.Time
}

type groupCache struct {
	ttl time.Duration
	now func() time.Time

	// Each mutex is held across its fetch, so concurrent callers wait for the
	// one query instead of each sending their own.
	joinedMu  sync.Mutex
	joined    []*types.GroupInfo
	joinedErr error
	joinedAt  time.Time

	infoMu sync.Mutex
	info   map[types.JID]groupInfoEntry
}

func newGroupCache(ttl time.Duration) *groupCache {
	return &groupCache{ttl: ttl, now: time.Now, info: make(map[types.JID]groupInfoEntry)}
}

func (g *groupCache) fresh(at time.Time) bool {
	return !at.IsZero() && g.now().Sub(at) < g.ttl
}

func (g *groupCache) joinedGroups(ctx context.Context, fetch func(context.Context) ([]*types.GroupInfo, error)) ([]*types.GroupInfo, error) {
	if g == nil || g.ttl <= 0 {
		return fetch(ctx)
	}
	g.joinedMu.Lock()
	defer g.joinedMu.Unlock()
	if g.fresh(g.joinedAt) {
		return g.joined, g.joinedErr
	}
	groups, err := fetch(ctx)
	if err != nil && ctx.Err() != nil {
		// The caller gave up; that says nothing about WhatsApp, so don't
		// make the next caller wait out a cooldown for it.
		return nil, err
	}
	g.joined, g.joinedErr, g.joinedAt = groups, err, g.now()
	return groups, err
}

func (g *groupCache) groupInfo(ctx context.Context, jid types.JID, fetch func(context.Context, types.JID) (*types.GroupInfo, error)) (*types.GroupInfo, error) {
	if g == nil || g.ttl <= 0 {
		return fetch(ctx, jid)
	}
	g.infoMu.Lock()
	defer g.infoMu.Unlock()
	if e, ok := g.info[jid]; ok && g.fresh(e.at) {
		return e.info, e.err
	}
	info, err := fetch(ctx, jid)
	if err != nil && ctx.Err() != nil {
		return nil, err
	}
	now := g.now()
	g.info[jid] = groupInfoEntry{info: info, err: err, at: now}
	// Expired entries are only dropped here, so the map stays bounded by the
	// groups seen within one TTL.
	for k, e := range g.info {
		if !g.fresh(e.at) {
			delete(g.info, k)
		}
	}
	return info, err
}

// invalidate drops what is cached for jid and the joined-groups list.
func (g *groupCache) invalidate(jid types.JID) {
	if g == nil {
		return
	}
	g.infoMu.Lock()
	delete(g.info, jid)
	g.infoMu.Unlock()
	g.joinedMu.Lock()
	g.joinedAt = time.Time{}
	g.joinedMu.Unlock()
}

func (g *groupCache) handleEvent(evt any) {
	switch v := evt.(type) {
	case *events.GroupInfo:
		g.invalidate(v.JID)
	case *events.JoinedGroup:
		g.invalidate(v.JID)
	}
}

// GetJoinedGroups shadows whatsmeow's method with the cached version, so every
// caller in the bridge goes through the cooldown.
func (c *Client) GetJoinedGroups(ctx context.Context) ([]*types.GroupInfo, error) {
	return c.groups.joinedGroups(ctx, c.Client.GetJoinedGroups)
}

// cachedGroupInfo is whatsmeow's GetGroupInfo behind the cache.
func (c *Client) cachedGroupInfo(ctx context.Context, jid types.JID) (*types.GroupInfo, error) {
	return c.groups.groupInfo(ctx, jid, c.Client.GetGroupInfo)
}
