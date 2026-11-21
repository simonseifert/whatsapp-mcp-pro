package whatsapp

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestCommunityFields(t *testing.T) {
	ownPN := types.NewJID("123", types.DefaultUserServer)
	ownLID := types.NewJID("999", types.HiddenUserServer)
	parent := types.NewJID("P", types.GroupServer)
	other := types.NewJID("456", types.DefaultUserServer)

	withParticipants := func(ps ...types.GroupParticipant) *types.GroupInfo {
		g := &types.GroupInfo{}
		g.Participants = ps
		return g
	}
	parentCommunity := withParticipants()
	parentCommunity.IsParent = true
	subgroup := withParticipants()
	subgroup.LinkedParentJID = parent
	defaultSub := withParticipants()
	defaultSub.LinkedParentJID = parent
	defaultSub.IsDefaultSubGroup = true

	tests := []struct {
		name         string
		g            *types.GroupInfo
		isCommunity  bool
		communityJID string
		defaultSub   bool
		iAmAdmin     bool
	}{
		{"parent community", parentCommunity, true, "", false, false},
		{"linked subgroup", subgroup, false, "P@g.us", false, false},
		{"default announcement subgroup", defaultSub, false, "P@g.us", true, false},
		{"own LID admin", withParticipants(types.GroupParticipant{JID: ownLID, LID: ownLID, IsAdmin: true}), false, "", false, true},
		{"own PN super admin via phone field", withParticipants(types.GroupParticipant{JID: ownLID, PhoneNumber: ownPN, IsSuperAdmin: true}), false, "", false, true},
		{"own PN plain member", withParticipants(types.GroupParticipant{JID: ownPN}), false, "", false, false},
		{"other admin only", withParticipants(types.GroupParticipant{JID: other, IsAdmin: true}), false, "", false, false},
		{"empty participants", withParticipants(), false, "", false, false},
		{"same digits other server is not me", withParticipants(types.GroupParticipant{JID: types.NewJID("123", types.HiddenUserServer), IsAdmin: true}), false, "", false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ic, cj, ds, admin := communityFields(tc.g, ownPN, ownLID)
			if ic != tc.isCommunity || cj != tc.communityJID || ds != tc.defaultSub || admin != tc.iAmAdmin {
				t.Fatalf("got (%v,%q,%v,%v), want (%v,%q,%v,%v)", ic, cj, ds, admin,
					tc.isCommunity, tc.communityJID, tc.defaultSub, tc.iAmAdmin)
			}
		})
	}
}
