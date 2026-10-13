package whatsapp

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestExtractTextContent(t *testing.T) {
	tests := []struct {
		name         string
		msg          *waE2E.Message
		wantText     string
		wantContains string
	}{
		{
			name:     "nil message",
			msg:      nil,
			wantText: "",
		},
		{
			name:     "empty message",
			msg:      &waE2E.Message{},
			wantText: "",
		},
		{
			name:     "plain text",
			msg:      &waE2E.Message{Conversation: proto.String("hello world")},
			wantText: "hello world",
		},
		{
			name: "extended text",
			msg: &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: proto.String("rich text with link"),
			}},
			wantText: "rich text with link",
		},
		{
			name: "image with caption",
			msg: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
				Caption: proto.String("photo caption"),
			}},
			wantText: "photo caption",
		},
		{
			name:     "image without caption",
			msg:      &waE2E.Message{ImageMessage: &waE2E.ImageMessage{}},
			wantText: "",
		},
		{
			name: "video with caption",
			msg: &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
				Caption: proto.String("video caption"),
			}},
			wantText: "video caption",
		},
		{
			name: "document with caption",
			msg: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				Caption: proto.String("doc caption"),
			}},
			wantText: "doc caption",
		},
		{
			name: "document with title only",
			msg: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				Title: proto.String("report.pdf"),
			}},
			wantText: "report.pdf",
		},
		{
			name:     "sticker",
			msg:      &waE2E.Message{StickerMessage: &waE2E.StickerMessage{}},
			wantText: "[Sticker]",
		},
		{
			name: "location with name",
			msg: &waE2E.Message{LocationMessage: &waE2E.LocationMessage{
				Name:             proto.String("Raffles Place"),
				DegreesLatitude:  proto.Float64(1.2840),
				DegreesLongitude: proto.Float64(103.8510),
			}},
			wantText: "[Location: Raffles Place]",
		},
		{
			name: "location without name",
			msg: &waE2E.Message{LocationMessage: &waE2E.LocationMessage{
				DegreesLatitude:  proto.Float64(1.2840),
				DegreesLongitude: proto.Float64(103.8510),
			}},
			wantContains: "[Location:",
		},
		{
			name: "contact card",
			msg: &waE2E.Message{ContactMessage: &waE2E.ContactMessage{
				DisplayName: proto.String("John Doe"),
			}},
			wantText: "[Contact: John Doe]",
		},
		{
			name: "poll",
			msg: &waE2E.Message{PollCreationMessage: &waE2E.PollCreationMessage{
				Name: proto.String("Favourite food?"),
			}},
			wantText: "[Poll: Favourite food?]",
		},
		{
			name: "reaction",
			msg: &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{
				Text: proto.String("👍"),
			}},
			wantText: "[Reaction: 👍]",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ExtractTextContent(tt.msg)
			if tt.wantText != "" {
				if got != tt.wantText {
					t.Errorf("ExtractTextContent() = %q, want %q", got, tt.wantText)
				}
			} else if tt.wantContains != "" {
				if !strings.Contains(got, tt.wantContains) {
					t.Errorf("ExtractTextContent() = %q, want to contain %q", got, tt.wantContains)
				}
			}
		})
	}
}

func TestValidateMediaPathRejectsSiblingOfAllowedDir(t *testing.T) {
	// /tmp is allowed; /tmp-evil is a different directory and must not ride in
	// on a bare prefix match.
	if err := validateMediaPath("/tmp-evil/payload.jpg"); err == nil {
		t.Error("validateMediaPath(/tmp-evil/...) = nil, want error")
	}
	if err := validateMediaPath("/tmp/ok.jpg"); err != nil {
		t.Errorf("validateMediaPath(/tmp/ok.jpg) = %v, want nil", err)
	}
}

func TestValidateMediaPathHonorsEnvAllowlist(t *testing.T) {
	t.Setenv("MEDIA_ALLOWED_DIRS", "/home/alice, /srv/media")
	for _, ok := range []string{"/home/alice/Code/pic.png", "/srv/media/a.ogg"} {
		if err := validateMediaPath(ok); err != nil {
			t.Errorf("validateMediaPath(%s) = %v, want nil", ok, err)
		}
	}
	if err := validateMediaPath("/home/simonevil/pic.png"); err == nil {
		t.Error("sibling of env-allowed dir accepted, want error")
	}
	t.Setenv("MEDIA_ALLOWED_DIRS", "")
	if err := validateMediaPath("/home/alice/Code/pic.png"); err == nil {
		t.Error("path accepted after env allowlist cleared, want error")
	}
}

func TestValidateMediaPathBlocksSymlinkEscape(t *testing.T) {
	// A symlink inside an allowed dir pointing outside it must not pass — the
	// bridge would otherwise read the target and send it.
	tmp := t.TempDir()
	t.Setenv("MEDIA_ALLOWED_DIRS", tmp)

	// Target must be outside every allowed dir. /tmp is a default allow-root, so
	// t.TempDir() (which lives under /tmp) would be legitimately allowed; use a
	// stable read-only file outside it.
	secret := "/etc/hostname"
	if _, err := os.Stat(secret); err != nil {
		t.Skipf("%s unavailable: %v", secret, err)
	}
	link := filepath.Join(tmp, "innocent.txt")
	if err := os.Symlink(secret, link); err != nil {
		t.Skipf("symlinks unsupported here: %v", err)
	}

	if err := validateMediaPath(link); err == nil {
		t.Error("symlink escaping the allowed dir was accepted, want error")
	}

	// A real file inside the allowed dir still works.
	real := filepath.Join(tmp, "ok.jpg")
	if err := os.WriteFile(real, []byte("x"), 0o600); err != nil {
		t.Fatalf("write real: %v", err)
	}
	if err := validateMediaPath(real); err != nil {
		t.Errorf("real in-dir file rejected: %v", err)
	}
}

func TestExtractReplyContext(t *testing.T) {
	quotedLink := &waE2E.Message{Conversation: proto.String("https://www.instagram.com/reel/abc/")}
	reply := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
		Text: proto.String("analyze this post"),
		ContextInfo: &waE2E.ContextInfo{
			StanzaID:      proto.String("QUOTED1"),
			Participant:   proto.String("123@s.whatsapp.net"),
			QuotedMessage: quotedLink,
		},
	}}
	rc := ExtractReplyContext(reply)
	if rc.QuotedID != "QUOTED1" || rc.QuotedSender != "123@s.whatsapp.net" || rc.QuotedText != "https://www.instagram.com/reel/abc/" || rc.IsForwarded {
		t.Fatalf("reply context = %+v", rc)
	}

	fwdImage := &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
		Mimetype:    proto.String("image/jpeg"),
		ContextInfo: &waE2E.ContextInfo{IsForwarded: proto.Bool(true)},
	}}
	if rc := ExtractReplyContext(fwdImage); !rc.IsForwarded || rc.QuotedID != "" {
		t.Fatalf("forwarded image context = %+v", rc)
	}

	quotedPhoto := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
		Text: proto.String("what is this"),
		ContextInfo: &waE2E.ContextInfo{
			StanzaID:      proto.String("Q2"),
			QuotedMessage: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg"), URL: proto.String("https://mmg.whatsapp.net/x")}},
		},
	}}
	if rc := ExtractReplyContext(quotedPhoto); rc.QuotedText != "[image]" {
		t.Fatalf("quoted photo text = %q, want [image]", rc.QuotedText)
	}

	if rc := ExtractReplyContext(&waE2E.Message{Conversation: proto.String("plain")}); rc != (ReplyContext{}) {
		t.Fatalf("plain message context = %+v", rc)
	}
}
