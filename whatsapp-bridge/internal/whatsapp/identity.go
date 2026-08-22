package whatsapp

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/types"
	"whatsapp-bridge/internal/database"
)

// IdentitySyncStats summarises one run of SyncIdentities.
type IdentitySyncStats struct {
	Rows        int
	Users       int
	Groups      int
	Linked      int // rows where both a LID and a phone JID are known
	Pruned      int
	GroupsEnric int // groups enriched with live metadata from the server
	Duration    time.Duration
}

// LIDMergeStats summarises one run of MergeLIDChats.
type LIDMergeStats struct {
	ChatsMerged   int
	MessagesMoved int
	Duplicates    int
	Unresolved    int // LID chats with no phone mapping yet — retried next run
}

// SyncIdentities rebuilds the unified directory in messages.db.
//
// Everything it needs already exists, just scattered: whatsmeow's contact store
// keys names by JID and files the same person twice (once under the phone JID
// with the address-book name, once under the LID with the push name), its LID
// map knows which two are the same person but is joined to nothing, and the
// chat and message tables know who we actually talk to. This collapses all of
// it onto one canonical JID per person or group.
//
// Safe to run repeatedly; it is a full rebuild, not an increment.
func (c *Client) SyncIdentities(messageStore *database.MessageStore) (IdentitySyncStats, error) {
	started := time.Now()
	var stats IdentitySyncStats
	ctx := context.Background()

	byJID := make(map[string]*database.Identity)

	// Contacts first, phone-JID rows before LID rows. Field merging is
	// fill-if-empty, so this ordering is what makes the address-book name win
	// over the self-chosen push name — and what makes the result deterministic
	// rather than dependent on Go's map iteration order.
	contacts, err := c.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return stats, fmt.Errorf("read contact store: %w", err)
	}

	contactJIDs := make([]types.JID, 0, len(contacts))
	for jid := range contacts {
		contactJIDs = append(contactJIDs, jid)
	}
	sort.Slice(contactJIDs, func(i, j int) bool {
		a, b := contactJIDs[i], contactJIDs[j]
		if (a.Server == types.DefaultUserServer) != (b.Server == types.DefaultUserServer) {
			return a.Server == types.DefaultUserServer
		}
		return a.String() < b.String()
	})

	for _, jid := range contactJIDs {
		info := contacts[jid]
		row := c.identityFor(ctx, byJID, jid)
		if row == nil {
			continue
		}
		fillIfEmpty(&row.FirstName, info.FirstName)
		fillIfEmpty(&row.FullName, info.FullName)
		fillIfEmpty(&row.PushName, info.PushName)
		fillIfEmpty(&row.BusinessName, info.BusinessName)
		fillIfEmpty(&row.RedactedPhone, info.RedactedPhone)
		if info.BusinessName != "" {
			row.IsBusiness = true
		}
		if info.Found {
			row.IsContact = true
		}
		addSource(row, "contacts")
	}

	// Chats contribute groups, newsletters, and anyone we talk to who never made
	// it into the address book.
	chatRows, err := messageStore.GetChatRows()
	if err != nil {
		return stats, fmt.Errorf("read chats: %w", err)
	}
	for _, chat := range chatRows {
		jid, err := types.ParseJID(chat.JID)
		if err != nil {
			c.logger.Debugf("[IDENTITY] skipping unparseable chat jid %q: %v", chat.JID, err)
			continue
		}
		row := c.identityFor(ctx, byJID, jid)
		if row == nil {
			continue
		}
		// A merged-away chat still tells us the identity exists, but its
		// activity has already been folded into the target's row.
		if chat.MergedInto == "" {
			row.HasChat = true
			fillIfEmpty(&row.ChatName, chat.Name)
			row.LastSeen = laterOf(row.LastSeen, chat.LastMessageTime)
		}
		addSource(row, "chats")
	}

	// Message aggregates, folded onto the canonical identity so a conversation
	// that was split across two JIDs counts once.
	chatStats, err := messageStore.GetChatStats()
	if err != nil {
		return stats, fmt.Errorf("read chat stats: %w", err)
	}
	for chatJID, stat := range chatStats {
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			continue
		}
		row := c.identityFor(ctx, byJID, jid)
		if row == nil {
			continue
		}
		row.MessageCount += stat.Count
		row.FirstSeen = earlierOf(row.FirstSeen, stat.FirstSeen)
		row.LastSeen = laterOf(row.LastSeen, stat.LastSeen)
		addSource(row, "messages")
	}

	// Nicknames are a deliberate user override and win the display name.
	nicknames, err := messageStore.GetNicknames()
	if err != nil {
		return stats, fmt.Errorf("read nicknames: %w", err)
	}
	for nickJID, nickname := range nicknames {
		jid, err := types.ParseJID(nickJID)
		if err != nil {
			continue
		}
		row := c.identityFor(ctx, byJID, jid)
		if row == nil {
			continue
		}
		row.Nickname = nickname
		addSource(row, "nicknames")
	}

	stats.GroupsEnric = c.enrichGroups(ctx, byJID)

	rows := make([]database.Identity, 0, len(byJID))
	keep := make([]string, 0, len(byJID))
	for _, row := range byJID {
		row.DisplayName = row.ResolveDisplayName()
		sort.Strings(row.Sources)
		rows = append(rows, *row)
		keep = append(keep, row.JID)
		switch row.Kind {
		case "group":
			stats.Groups++
		case "user":
			stats.Users++
		}
		if row.LID != "" && row.PhoneJID != "" {
			stats.Linked++
		}
	}

	if err := messageStore.UpsertIdentities(rows); err != nil {
		return stats, err
	}
	pruned, err := messageStore.PruneIdentities(keep)
	if err != nil {
		return stats, err
	}

	stats.Rows = len(rows)
	stats.Pruned = pruned
	stats.Duration = time.Since(started)
	return stats, nil
}

// identityFor returns the directory row a JID belongs to, creating it on first
// sight. Both halves of a split identity resolve to the same row.
func (c *Client) identityFor(ctx context.Context, byJID map[string]*database.Identity, jid types.JID) *database.Identity {
	jid = jid.ToNonAD()
	canonical, lid, phone := c.resolveIdentity(ctx, jid)
	if canonical.IsEmpty() {
		return nil
	}

	key := canonical.String()
	row, ok := byJID[key]
	if !ok {
		row = &database.Identity{JID: key, Kind: kindForServer(canonical.Server)}
		byJID[key] = row
	}
	if !lid.IsEmpty() {
		row.LID = lid.String()
	}
	if !phone.IsEmpty() {
		row.PhoneJID = phone.String()
		row.Phone = phone.User
	}
	return row
}

// resolveIdentity maps any JID onto its canonical form plus both known halves.
// The phone JID is canonical whenever we know it: it is stable, human-readable,
// and is what every other tool in this repo already keys on.
func (c *Client) resolveIdentity(ctx context.Context, jid types.JID) (canonical, lid, phone types.JID) {
	switch jid.Server {
	case types.HiddenUserServer:
		lid = jid
		if resolved, err := c.Store.GetAltJID(ctx, jid); err == nil && !resolved.IsEmpty() {
			phone = resolved.ToNonAD()
		}
	case types.DefaultUserServer:
		phone = jid
		if resolved, err := c.Store.GetAltJID(ctx, jid); err == nil && !resolved.IsEmpty() {
			lid = resolved.ToNonAD()
		}
	default:
		return jid, types.EmptyJID, types.EmptyJID
	}

	if !phone.IsEmpty() {
		return phone, lid, phone
	}
	return jid, lid, phone
}

// enrichGroups pulls live group metadata in a single round trip. Best-effort:
// the directory is still useful without it, so a failure is logged, not fatal.
func (c *Client) enrichGroups(ctx context.Context, byJID map[string]*database.Identity) int {
	if !c.IsConnected() {
		return 0
	}
	groups, err := c.GetJoinedGroups(ctx)
	if err != nil {
		c.logger.Debugf("[IDENTITY] group enrichment skipped: %v", err)
		return 0
	}

	enriched := 0
	for _, group := range groups {
		row, ok := byJID[group.JID.String()]
		if !ok {
			row = &database.Identity{JID: group.JID.String(), Kind: "group"}
			byJID[row.JID] = row
		}
		if group.Name != "" {
			row.FullName = group.Name
			row.ChatName = group.Name
		}
		row.ParticipantCount = group.ParticipantCount
		if row.ParticipantCount == 0 {
			row.ParticipantCount = len(group.Participants)
		}
		row.IsAnnounce = group.IsAnnounce
		if !group.GroupCreated.IsZero() {
			created := group.GroupCreated
			row.FirstSeen = earlierOf(row.FirstSeen, &created)
		}
		if !group.OwnerPN.IsEmpty() {
			row.OwnerJID = group.OwnerPN.ToNonAD().String()
		} else if !group.OwnerJID.IsEmpty() {
			row.OwnerJID = group.OwnerJID.ToNonAD().String()
		}
		addSource(row, "groups")
		enriched++
	}
	return enriched
}

// MergeLIDChats folds every LID-filed chat into its phone-JID twin.
//
// NormalizeChatJID keeps new messages from splitting, but history written
// before it existed is already split — and a LID chat whose mapping only
// arrives later splits again until the mapping lands. Running this on each
// connect catches both.
func (c *Client) MergeLIDChats(messageStore *database.MessageStore) (LIDMergeStats, error) {
	var stats LIDMergeStats
	ctx := context.Background()

	chatRows, err := messageStore.GetChatRows()
	if err != nil {
		return stats, fmt.Errorf("read chats: %w", err)
	}

	for _, chat := range chatRows {
		if chat.MergedInto != "" {
			continue
		}
		jid, err := types.ParseJID(chat.JID)
		if err != nil || jid.Server != types.HiddenUserServer {
			continue
		}
		resolved, err := c.Store.GetAltJID(ctx, jid)
		if err != nil || resolved.IsEmpty() {
			stats.Unresolved++
			continue
		}
		target := resolved.ToNonAD().String()
		if target == chat.JID {
			continue
		}

		result, err := messageStore.MergeChat(chat.JID, target)
		if err != nil {
			c.logger.Warnf("[IDENTITY] merge %s -> %s failed: %v", chat.JID, target, err)
			continue
		}
		stats.ChatsMerged++
		stats.MessagesMoved += result.MessagesMoved
		stats.Duplicates += result.MessagesDuplicate
		c.logger.Infof("[IDENTITY] merged %s into %s (%d messages moved, %d duplicates dropped)",
			chat.JID, target, result.MessagesMoved, result.MessagesDuplicate)
	}

	return stats, nil
}

func kindForServer(server string) string {
	switch server {
	case types.GroupServer:
		return "group"
	case types.NewsletterServer:
		return "newsletter"
	case types.BroadcastServer:
		return "broadcast"
	default:
		return "user"
	}
}

func fillIfEmpty(dst *string, value string) {
	if strings.TrimSpace(*dst) == "" && strings.TrimSpace(value) != "" {
		*dst = strings.TrimSpace(value)
	}
}

func addSource(row *database.Identity, source string) {
	for _, existing := range row.Sources {
		if existing == source {
			return
		}
	}
	row.Sources = append(row.Sources, source)
}

func earlierOf(a, b *time.Time) *time.Time {
	if b == nil || b.IsZero() {
		return a
	}
	if a == nil || a.IsZero() || b.Before(*a) {
		return b
	}
	return a
}

func laterOf(a, b *time.Time) *time.Time {
	if b == nil || b.IsZero() {
		return a
	}
	if a == nil || a.IsZero() || b.After(*a) {
		return b
	}
	return a
}

// IdentitySyncer keeps the directory fresh without rebuilding it on every
// contact event. Contact, push-name and business-name events arrive in bursts
// during app-state sync — hundreds in a few seconds — and a full rebuild costs
// a table scan, so events only mark the directory dirty and a single worker
// coalesces them.
type IdentitySyncer struct {
	client   *Client
	store    *database.MessageStore
	dirty    chan struct{}
	interval time.Duration
	merge    bool
}

// NewIdentitySyncer wires a syncer to a store. Call Start to run it.
func (c *Client) NewIdentitySyncer(messageStore *database.MessageStore, interval time.Duration, merge bool) *IdentitySyncer {
	return &IdentitySyncer{
		client:   c,
		store:    messageStore,
		dirty:    make(chan struct{}, 1),
		interval: interval,
		merge:    merge,
	}
}

// Trigger requests a rebuild. Never blocks: a pending request already covers
// this one, since the rebuild is a full refresh rather than an increment.
func (s *IdentitySyncer) Trigger() {
	if s == nil {
		return
	}
	select {
	case s.dirty <- struct{}{}:
	default:
	}
}

// Start runs the sync worker until ctx is cancelled.
func (s *IdentitySyncer) Start(ctx context.Context) {
	go func() {
		var tick <-chan time.Time
		if s.interval > 0 {
			ticker := time.NewTicker(s.interval)
			defer ticker.Stop()
			tick = ticker.C
		}
		for {
			select {
			case <-ctx.Done():
				return
			case <-s.dirty:
				s.RunOnce()
			case <-tick:
				s.RunOnce()
			}
		}
	}()
}

// RunOnce merges any newly resolvable LID chats, then rebuilds the directory.
// Merge runs first so the rebuild sees the collapsed chat and counts each
// conversation once.
func (s *IdentitySyncer) RunOnce() {
	if s.merge {
		mergeStats, err := s.client.MergeLIDChats(s.store)
		if err != nil {
			s.client.logger.Warnf("[IDENTITY] LID chat merge failed: %v", err)
		} else if mergeStats.ChatsMerged > 0 {
			s.client.logger.Infof("[IDENTITY] merged %d LID chats (%d messages moved, %d duplicates dropped, %d still unmapped)",
				mergeStats.ChatsMerged, mergeStats.MessagesMoved, mergeStats.Duplicates, mergeStats.Unresolved)
		}
	}

	stats, err := s.client.SyncIdentities(s.store)
	if err != nil {
		s.client.logger.Warnf("[IDENTITY] directory sync failed: %v", err)
		return
	}
	s.client.logger.Infof("[IDENTITY] directory: %d rows (%d users, %d groups, %d lid↔phone linked, %d pruned) in %v",
		stats.Rows, stats.Users, stats.Groups, stats.Linked, stats.Pruned, stats.Duration.Round(time.Millisecond))
}
