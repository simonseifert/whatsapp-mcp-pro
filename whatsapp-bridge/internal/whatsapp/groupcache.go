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
// for the same group share one in-flight fetch. WhatsApp's own group-change events drop the
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

// flight is one fetch in progress; callers asking for the same thing wait on
// done and share its result.
type flight[T any] struct {
	done chan struct{}
	val  T
	err  error
}

type groupCache struct {
	ttl time.Duration
	now func() time.Time

	// The mutex guards the maps only and is never held across a network
	// fetch: invalidate runs inside whatsmeow's synchronous event dispatch, so
	// a lock held for a slow IQ would stall every incoming message behind it.
	mu             sync.Mutex
	joined         []*types.GroupInfo
	joinedErr      error
	joinedAt       time.Time
	joinedInFlight *flight[[]*types.GroupInfo]
	info           map[types.JID]groupInfoEntry
	infoInFlight   map[types.JID]*flight[*types.GroupInfo]
}

func newGroupCache(ttl time.Duration) *groupCache {
	return &groupCache{
		ttl:          ttl,
		now:          time.Now,
		info:         make(map[types.JID]groupInfoEntry),
		infoInFlight: make(map[types.JID]*flight[*types.GroupInfo]),
	}
}

func (g *groupCache) fresh(at time.Time) bool {
	return !at.IsZero() && g.now().Sub(at) < g.ttl
}

// wait blocks until f lands or ctx ends.
func wait[T any](ctx context.Context, f *flight[T]) (T, error) {
	select {
	case <-f.done:
		return f.val, f.err
	case <-ctx.Done():
		var zero T
		return zero, ctx.Err()
	}
}

func (g *groupCache) joinedGroups(ctx context.Context, fetch func(context.Context) ([]*types.GroupInfo, error)) ([]*types.GroupInfo, error) {
	if g == nil || g.ttl <= 0 {
		return fetch(ctx)
	}
	g.mu.Lock()
	if g.fresh(g.joinedAt) {
		groups, err := g.joined, g.joinedErr
		g.mu.Unlock()
		return groups, err
	}
	if f := g.joinedInFlight; f != nil {
		g.mu.Unlock()
		return wait(ctx, f)
	}
	f := &flight[[]*types.GroupInfo]{done: make(chan struct{})}
	g.joinedInFlight = f
	g.mu.Unlock()

	f.val, f.err = fetch(ctx)

	g.mu.Lock()
	g.joinedInFlight = nil
	// A caller that gave up says nothing about WhatsApp, so don't make the
	// next caller wait out a cooldown for it.
	if f.err == nil || ctx.Err() == nil {
		g.joined, g.joinedErr, g.joinedAt = f.val, f.err, g.now()
	}
	g.mu.Unlock()
	close(f.done)
	return f.val, f.err
}

func (g *groupCache) groupInfo(ctx context.Context, jid types.JID, fetch func(context.Context, types.JID) (*types.GroupInfo, error)) (*types.GroupInfo, error) {
	if g == nil || g.ttl <= 0 {
		return fetch(ctx, jid)
	}
	g.mu.Lock()
	if e, ok := g.info[jid]; ok && g.fresh(e.at) {
		g.mu.Unlock()
		return e.info, e.err
	}
	if f, ok := g.infoInFlight[jid]; ok {
		g.mu.Unlock()
		return wait(ctx, f)
	}
	f := &flight[*types.GroupInfo]{done: make(chan struct{})}
	g.infoInFlight[jid] = f
	g.mu.Unlock()

	f.val, f.err = fetch(ctx, jid)

	g.mu.Lock()
	delete(g.infoInFlight, jid)
	if f.err == nil || ctx.Err() == nil {
		now := g.now()
		g.info[jid] = groupInfoEntry{info: f.val, err: f.err, at: now}
		// Expired entries are only dropped here, so the map stays bounded by
		// the groups seen within one TTL.
		for k, e := range g.info {
			if !g.fresh(e.at) {
				delete(g.info, k)
			}
		}
	}
	g.mu.Unlock()
	close(f.done)
	return f.val, f.err
}

// invalidate drops what is cached for jid and the joined-groups list.
func (g *groupCache) invalidate(jid types.JID) {
	if g == nil {
		return
	}
	g.mu.Lock()
	delete(g.info, jid)
	g.joinedAt = time.Time{}
	g.mu.Unlock()
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
