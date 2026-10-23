package whatsapp

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"whatsapp-bridge/internal/antiban"
	"whatsapp-bridge/internal/database"
	bridgeTypes "whatsapp-bridge/internal/types"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// mentionPattern matches @phone_number patterns in message text (7-15 digits)
var mentionPattern = regexp.MustCompile(`@(\d{7,15})`)

func mediaTypeAndMimeType(mediaPath string) (whatsmeow.MediaType, string) {
	fileExt := strings.TrimPrefix(strings.ToLower(filepath.Ext(mediaPath)), ".")

	switch fileExt {
	case "jpg", "jpeg":
		return whatsmeow.MediaImage, "image/jpeg"
	case "png":
		return whatsmeow.MediaImage, "image/png"
	case "gif":
		return whatsmeow.MediaImage, "image/gif"
	case "webp":
		return whatsmeow.MediaImage, "image/webp"
	case "ogg":
		return whatsmeow.MediaAudio, "audio/ogg; codecs=opus"
	case "mp3":
		return whatsmeow.MediaAudio, "audio/mpeg"
	case "m4a", "aac":
		return whatsmeow.MediaAudio, "audio/mp4"
	case "mp4":
		return whatsmeow.MediaVideo, "video/mp4"
	case "avi":
		return whatsmeow.MediaVideo, "video/avi"
	case "mov":
		return whatsmeow.MediaVideo, "video/quicktime"
	case "epub":
		return whatsmeow.MediaDocument, "application/epub+zip"
	case "pdf":
		return whatsmeow.MediaDocument, "application/pdf"
	case "cbz":
		return whatsmeow.MediaDocument, "application/x-cbz"
	case "doc":
		return whatsmeow.MediaDocument, "application/msword"
	case "docx":
		return whatsmeow.MediaDocument, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
	case "xls":
		return whatsmeow.MediaDocument, "application/vnd.ms-excel"
	case "xlsx":
		return whatsmeow.MediaDocument, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
	case "ppt":
		return whatsmeow.MediaDocument, "application/vnd.ms-powerpoint"
	case "pptx":
		return whatsmeow.MediaDocument, "application/vnd.openxmlformats-officedocument.presentationml.presentation"
	case "txt":
		return whatsmeow.MediaDocument, "text/plain"
	case "csv":
		return whatsmeow.MediaDocument, "text/csv"
	case "zip":
		return whatsmeow.MediaDocument, "application/zip"
	default:
		return whatsmeow.MediaDocument, "application/octet-stream"
	}
}

func documentFileName(mediaPath string) string {
	return filepath.Base(strings.ReplaceAll(mediaPath, "\\", "/"))
}

// extractMentionsFromText detects @number mentions in text and returns WhatsApp JIDs
func extractMentionsFromText(text string) []string {
	matches := mentionPattern.FindAllStringSubmatch(text, -1)
	if len(matches) == 0 {
		return nil
	}
	seen := make(map[string]bool)
	var jids []string
	for _, m := range matches {
		jid := m[1] + "@s.whatsapp.net"
		if !seen[jid] {
			seen[jid] = true
			jids = append(jids, jid)
		}
	}
	return jids
}

// defaultMediaDirs are the directories media may be read from when
// MEDIA_ALLOWED_DIRS is unset: the Docker layout, /tmp (where the MCP server
// stages base64 uploads), and the native data dir.
var defaultMediaDirs = []string{
	"/app/media",
	"/app/store",
	"/app/whatsapp-bridge/store",
	"/tmp",
}

// allowedMediaDirs resolves the media allowlist. MEDIA_ALLOWED_DIRS is a
// comma-separated list of extra directories appended to the defaults, so a
// native install can say MEDIA_ALLOWED_DIRS=$HOME and let a local agent
// send files straight from its working tree. Resolved per call rather than
// cached: it is not hot (only send/download touch it), and this keeps tests
// and env changes honest.
func allowedMediaDirs() []string {
	dirs := append([]string{}, defaultMediaDirs...)
	for _, extra := range strings.Split(os.Getenv("MEDIA_ALLOWED_DIRS"), ",") {
		if trimmed := strings.TrimSpace(extra); trimmed != "" {
			dirs = append(dirs, trimmed)
		}
	}
	return dirs
}

// validateMediaPath checks if the path resolves to a location within an
// allowed directory and is not a secret (see checkSensitivePath). It resolves symlinks before matching so a symlink planted
// inside an allowed directory cannot redirect os.ReadFile to an arbitrary file,
// and it matches on path-segment boundaries so a sibling directory that merely
// shares a name prefix (e.g. /tmproot vs /tmp) is not treated as inside the
// allowlist.
func validateMediaPath(mediaPath string) error {
	if mediaPath == "" {
		return nil
	}

	// Check for path traversal attempts. Kept before DISABLE_PATH_CHECK so that
	// traversal is rejected even when the check is otherwise disabled.
	if strings.Contains(mediaPath, "..") {
		return fmt.Errorf("path traversal not allowed")
	}

	absPath, err := filepath.Abs(filepath.Clean(mediaPath))
	if err != nil {
		return fmt.Errorf("invalid media path: %w", err)
	}

	// os.ReadFile follows symlinks, so validation must see the real destination.
	// Resolve symlinks on the longest existing prefix of the path: this defeats
	// a symlink planted anywhere along the path while still permitting a leaf
	// file that has not been created yet.
	resolvedPath := resolveExistingPrefix(absPath)
	roots := allowedMediaRoots()

	// An allowed dir is usually broad (MEDIA_ALLOWED_DIRS=$HOME), so being
	// inside it says nothing about whether the file is safe to upload to a chat.
	// The caller is an LLM agent that message text can prompt-inject; refusing
	// secrets here is the last stop before they reach WhatsApp. Both forms are
	// checked because either can be the innocent-looking one: photo.jpg
	// symlinked to .env, or a link named .env pointing at a photo. This runs
	// before DISABLE_PATH_CHECK on purpose: that flag relaxes the directory
	// allowlist for development, not secret protection.
	for _, p := range []string{absPath, resolvedPath} {
		if err := checkSensitivePath(p, roots); err != nil {
			return err
		}
	}

	// Allow if DISABLE_PATH_CHECK is set (for development)
	if os.Getenv("DISABLE_PATH_CHECK") == "true" {
		return nil
	}

	if longestContainingRoot(resolvedPath, roots) == "" {
		return fmt.Errorf("media path outside allowed directories")
	}
	return nil
}

// allowedMediaRoots returns the allowlist as absolute paths, each in both its
// literal and symlink-resolved form (e.g. macOS /tmp is a symlink to
// /private/tmp). Entries that do not exist keep only their literal form rather
// than failing the whole check.
func allowedMediaRoots() []string {
	var roots []string
	for _, dir := range allowedMediaDirs() {
		abs, err := filepath.Abs(dir)
		if err != nil {
			continue
		}
		roots = append(roots, abs)
		if resolved, err := filepath.EvalSymlinks(abs); err == nil && resolved != abs {
			roots = append(roots, resolved)
		}
	}
	return roots
}

// longestContainingRoot returns the most specific root that p lies in, or ""
// if none. Matching is on path-segment boundaries, not a raw string prefix, so
// /tmpfoo is not inside /tmp. Most specific wins so that a data dir listed
// below a broad home dir decides which segments count as "inside" it.
func longestContainingRoot(p string, roots []string) string {
	sep := string(os.PathSeparator)
	best := ""
	for _, root := range roots {
		inside := p == root || strings.HasPrefix(p, strings.TrimSuffix(root, sep)+sep)
		if inside && len(root) > len(best) {
			best = root
		}
	}
	return best
}

// sensitiveNamePatterns are basenames (matched lower-cased) that hold keys,
// credentials or password vaults. Leading-dot names such as .env and .netrc
// are caught separately by the hidden-segment rule.
var sensitiveNamePatterns = []string{
	"*.env",
	"*.pem",
	"*.key",
	"*.p12",
	"*.kdbx",
	"id_rsa*",
	"id_ed25519*",
	"config.json",
	"credentials*.json",
	"secrets.*",
	// SQLite databases anywhere: the bridge's session keys and history are
	// one, and so is nearly every other app's local state. ".db" is matched as
	// a substring below so WAL/SHM/journal sidecars, which hold recent writes
	// in the clear, are covered too.
	"*.sqlite",
	"*.sqlite3",
	"*.sqlite-*",
}

// checkSensitivePath refuses dotfiles, anything under a dot-directory, known
// secret filenames, databases, and files sitting directly in a "store"
// directory.
//
// Media legitimately lives one level down (store/media/<jid>/..., and
// auto-downloads in store/<jid>/...); the top of store/ holds the session DB,
// pair-qr.txt and similar bridge state, none of which belongs in a chat. The
// Python MCP server applies the same rules so both layers agree.
//
// The dot rule only looks at segments below the matched allowed root: the
// operator chose the root, and a data dir such as ~/.local/share/... listed in
// MEDIA_ALLOWED_DIRS must keep working. With no matching root (reachable only
// under DISABLE_PATH_CHECK) every segment is checked.
func checkSensitivePath(p string, roots []string) error {
	rel := p
	if root := longestContainingRoot(p, roots); root != "" {
		if r, err := filepath.Rel(root, p); err == nil {
			rel = r
		}
	}
	for _, seg := range strings.Split(filepath.ToSlash(rel), "/") {
		if strings.HasPrefix(seg, ".") && seg != "." {
			return fmt.Errorf("refusing sensitive file: hidden path segment %q", seg)
		}
	}

	base := strings.ToLower(filepath.Base(p))
	for _, pattern := range sensitiveNamePatterns {
		if ok, _ := filepath.Match(pattern, base); ok {
			return fmt.Errorf("refusing sensitive file: name matches %q", pattern)
		}
	}

	if strings.Contains(base, ".db") {
		return fmt.Errorf("refusing sensitive file: database file")
	}

	if strings.EqualFold(filepath.Base(filepath.Dir(p)), "store") {
		return fmt.Errorf("refusing sensitive file: directly inside a store directory")
	}
	return nil
}

// resolveExistingPrefix returns absPath with its longest existing ancestor
// passed through filepath.EvalSymlinks and any remaining (not-yet-existing)
// components re-appended. filepath.EvalSymlinks requires the whole path to
// exist; this walks up until it finds a component that does, so a symlink in
// the existing portion is followed while a missing leaf file is tolerated.
func resolveExistingPrefix(absPath string) string {
	remainder := ""
	current := absPath
	for {
		if resolved, err := filepath.EvalSymlinks(current); err == nil {
			if remainder == "" {
				return resolved
			}
			return filepath.Join(resolved, remainder)
		}
		parent := filepath.Dir(current)
		if parent == current {
			// Reached the filesystem root without resolving anything.
			return absPath
		}
		remainder = filepath.Join(filepath.Base(current), remainder)
		current = parent
	}
}

// SendMessage sends a WhatsApp message with optional media, quoted message, and mentions

// IsRecipientAllowed reports whether a recipient passes the
// WHATSAPP_ALLOWLIST_JIDS safety gate.
//
// Matching is EXACT (case-insensitive) against either the full JID
// ("4915112345678@s.whatsapp.net") or its user part ("4915112345678"), which is
// what the documented syntax describes: "only to specific phone numbers or
// group JIDs (comma-separated)".
//
// It deliberately does NOT substring-match. An earlier version also allowed
// `strings.Contains(target, entry)`, which silently defeated the gate: because
// the comparison ran against the whole JID string, an entry of "net",
// "s.whatsapp.net" or "@" matched EVERY individual recipient, and a short entry
// like a country code matched every number containing those digits anywhere.
// This gate is the backstop for autonomous senders (wa-dispatch, wa-assistant)
// that run without a human approving each message, so a bypass here is the
// difference between "drafts a reply" and "messages a client".
//
// Prefix semantics are not supported. If they are ever wanted they need an
// explicit syntax (e.g. a trailing "*") parsed deliberately, never as the
// default behaviour of a containment check.
func IsRecipientAllowed(allowlist []string, recipient types.JID) bool {
	target := recipient.String()
	user := recipient.User
	for _, entry := range allowlist {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		if strings.EqualFold(entry, target) || strings.EqualFold(entry, user) {
			return true
		}
	}
	return false
}

func (c *Client) SendMessage(messageStore *database.MessageStore, recipient string, message string, mediaPath string, quotedMessageID string, mentionedJIDs ...[]string) bridgeTypes.SendResult {
	opts := SendOptions{Recipient: recipient, Message: message, MediaPath: mediaPath, QuotedMessageID: quotedMessageID}
	if len(mentionedJIDs) > 0 {
		opts.MentionedJIDs = mentionedJIDs[0]
	}
	return c.SendMessageWithOptions(context.Background(), messageStore, opts)
}

// SendMessageWithOptions sends a text or media message. ctx bounds the whole
// send (upload, antiban wait, server ack), so a caller that gives up cancels
// it. On failure SendResult.Err is a *SendError carrying the failure class.
func (c *Client) SendMessageWithOptions(ctx context.Context, messageStore *database.MessageStore, opts SendOptions) bridgeTypes.SendResult {
	recipient, message, mediaPath, quotedMessageID := opts.Recipient, opts.Message, opts.MediaPath, opts.QuotedMessageID
	if !c.IsConnected() {
		return failed(newSendError(SendErrNotConnected, "Not connected to WhatsApp"), "")
	}

	c.GoOnlineBriefly()

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return failed(newSendError(SendErrInvalid, "Error parsing JID: %w", err), "")
		}
		// WhatsApp now hands out LID addresses ("<id>@lid") for one-to-one chats, and that is what
		// the chat list stores. Sending straight to a LID is refused by the server with error 463,
		// so resolve it back to the phone JID we are actually allowed to address. The mapping comes
		// from the client's own LID store, which is filled while receiving. If there is no mapping
		// yet, keep the LID: a group JID or an unknown contact must not be silently rewritten.
		if recipientJID.Server == types.HiddenUserServer {
			if pn, altErr := c.Store.GetAltJID(context.Background(), recipientJID); altErr == nil && !pn.IsEmpty() {
				c.logger.Debugf("Resolved LID %s to %s for sending", recipientJID, pn)
				recipientJID = pn
			} else if altErr != nil {
				c.logger.Warnf("Could not resolve LID %s to a phone JID: %v", recipientJID, altErr)
			}
		}
	} else {
		recipientJID, err = bareRecipientJID(recipient, c.knownLIDFunc(messageStore))
		if err != nil {
			return failed(newSendError(SendErrInvalid, "%w", err), "")
		}
	}

	// Safety gate: Check WHATSAPP_ALLOWLIST_JIDS if configured
	if c.cfg != nil && len(c.cfg.AllowlistJIDs) > 0 {
		if !IsRecipientAllowed(c.cfg.AllowlistJIDs, recipientJID) {
			return failed(newSendError(SendErrForbidden, "recipient %s is blocked by WHATSAPP_ALLOWLIST_JIDS safety gate", recipient), "")
		}
	}

	msg := &waE2E.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Validate media path (prevent path traversal)
		if err := validateMediaPath(mediaPath); err != nil {
			return failed(newSendError(SendErrForbidden, "Invalid media path: %w", err), "")
		}

		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return failed(newSendError(SendErrInvalid, "Error reading media file: %w", err), "")
		}

		mediaType, mimeType := mediaTypeAndMimeType(mediaPath)

		// Upload media to WhatsApp servers
		resp, err := c.Upload(ctx, mediaData, mediaType)
		if err != nil {
			return failed(classify(err, "Error uploading media: %w"), "")
		}

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waE2E.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := AnalyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return failed(newSendError(SendErrInvalid, "Failed to analyze Ogg Opus file: %w", err), "")
				}
			}

			msg.AudioMessage = &waE2E.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waE2E.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			docName := documentFileName(mediaPath)
			msg.DocumentMessage = &waE2E.DocumentMessage{
				Title:         proto.String(docName),
				FileName:      proto.String(docName),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		// Collect mentions: explicit + auto-detected from @number patterns
		var mentions []string
		mentions = append(mentions, opts.MentionedJIDs...)
		mentions = append(mentions, extractMentionsFromText(message)...)

		if len(mentions) > 0 {
			msg.ExtendedTextMessage = &waE2E.ExtendedTextMessage{
				Text: proto.String(message),
				ContextInfo: &waE2E.ContextInfo{
					MentionedJID: mentions,
				},
			}
		} else {
			msg.Conversation = proto.String(message)
		}
	}

	// Build quoted message context if quotedMessageID is provided
	if quotedMessageID != "" && messageStore != nil {
		content, sender, isFromMe, err := messageStore.GetMessageContentAndSender(quotedMessageID)
		if err == nil {
			quoted, pErr := c.quotedParticipant(messageStore, sender, recipientJID, isFromMe)
			if pErr != nil {
				// A quote with a broken participant is delivered as a reply to
				// nothing and still reports success, so refuse instead.
				return failed(newSendError(SendErrInvalid, "cannot quote message %s: %w", quotedMessageID, pErr), "")
			}
			participant := quoted.String()
			contextInfo := &waE2E.ContextInfo{
				StanzaID:    proto.String(quotedMessageID),
				Participant: proto.String(participant),
				QuotedMessage: &waE2E.Message{
					Conversation: proto.String(content),
				},
			}

			if msg.ExtendedTextMessage != nil {
				if msg.ExtendedTextMessage.ContextInfo == nil {
					msg.ExtendedTextMessage.ContextInfo = contextInfo
				} else {
					msg.ExtendedTextMessage.ContextInfo.StanzaID = proto.String(quotedMessageID)
					msg.ExtendedTextMessage.ContextInfo.Participant = proto.String(participant)
					msg.ExtendedTextMessage.ContextInfo.QuotedMessage = contextInfo.QuotedMessage
				}
			} else if msg.ImageMessage != nil {
				msg.ImageMessage.ContextInfo = contextInfo
			} else if msg.AudioMessage != nil {
				msg.AudioMessage.ContextInfo = contextInfo
			} else if msg.VideoMessage != nil {
				msg.VideoMessage.ContextInfo = contextInfo
			} else if msg.DocumentMessage != nil {
				msg.DocumentMessage.ContextInfo = contextInfo
			} else if msg.Conversation != nil {
				msg.ExtendedTextMessage = &waE2E.ExtendedTextMessage{
					Text:        msg.Conversation,
					ContextInfo: contextInfo,
				}
				msg.Conversation = nil
			}
		}
	}

	// Reachout timelock: while WhatsApp is refusing new conversations (error
	// 463), only chats with history may be messaged. Checked before the typing
	// indicator so a refused send leaves no trace on the recipient's side.
	if se := c.checkReachoutGate(messageStore, recipientJID, recipient); se != nil {
		return failed(se, "")
	}

	// Antiban: simulate typing and enforce rate limit / warm-up before sending
	if c.antiban.Enabled() {
		_ = c.SendTypingIndicator(recipientJID.String(), "typing")
	}
	if err := c.antiban.BeforeSend(ctx, antiban.Text, len(message)); err != nil {
		_ = c.SendTypingIndicator(recipientJID.String(), "paused")
		return failed(classify(err, "antiban blocked send: %w"), "")
	}

	// Send message
	msgID := c.prepareMessageID(opts.MessageID, opts.BeforeDispatch)
	sendResp, err := c.Client.SendMessage(ctx, recipientJID, msg, whatsmeow.SendRequestExtra{ID: msgID})
	if err != nil {
		_ = c.SendTypingIndicator(recipientJID.String(), "paused")
		return failed(c.sendFailure(recipientJID, antiban.Text, err, "Error sending message: %w"), string(msgID))
	}
	_ = c.SendTypingIndicator(recipientJID.String(), "paused")
	c.antiban.AfterSend(antiban.Text)

	content := ExtractTextContent(msg)
	mediaType, filename, url, directPath, mediaKey, fileSHA256, fileEncSHA256, fileLength := ExtractMediaInfo(msg)
	sender := ""
	if c.Store != nil && c.Store.ID != nil {
		sender = c.Store.ID.ToNonAD().String()
	}

	_ = messageStore.StoreMessage(
		sendResp.ID, // Use the ID from SendResponse
		recipientJID.String(),
		sender,
		sender,
		content,
		sendResp.Timestamp, // Use the Timestamp from SendResponse
		true,               // IsFromMe is true since we are sending this message
		mediaType,
		filename,
		url,
		directPath,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)

	return bridgeTypes.SendResult{
		Success:   true,
		MessageID: string(sendResp.ID),
		Timestamp: sendResp.Timestamp,
	}
}

// SendReaction sends an emoji reaction to a message.
// It looks up the original message sender from the store so that
// BuildReaction constructs the correct MessageKey (FromMe flag).
func (c *Client) SendReaction(messageStore *database.MessageStore, chatJID, messageID, emoji string) error {
	res := c.SendReactionWithOptions(context.Background(), messageStore, ReactionOptions{ChatJID: chatJID, TargetMessageID: messageID, Emoji: emoji})
	if !res.Success {
		return res.Err
	}
	return nil
}

// SendReactionWithOptions is SendReaction bounded by ctx, returning a
// SendResult (MessageID is the reaction's own ID) with a typed Err on failure.
func (c *Client) SendReactionWithOptions(ctx context.Context, messageStore *database.MessageStore, opts ReactionOptions) bridgeTypes.SendResult {
	chatJID, messageID, emoji := opts.ChatJID, opts.TargetMessageID, opts.Emoji
	if !c.IsConnected() {
		return failed(newSendError(SendErrNotConnected, "not connected to WhatsApp"), "")
	}

	c.GoOnlineBriefly()

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return failed(newSendError(SendErrInvalid, "invalid chat JID: %w", err), "")
	}
	if se := c.checkAllowlist(chat); se != nil {
		return failed(se, "")
	}

	msgID := types.MessageID(messageID)

	// Look up the actual sender of the message being reacted to.
	// BuildReaction needs the original message sender to set FromMe correctly.
	senderJID := c.Store.ID.ToNonAD() // default: assume own message
	if messageStore != nil {
		senderStr, isFromMe, lookupErr := messageStore.GetMessageSender(messageID, chatJID)
		if lookupErr == nil && !isFromMe {
			parsed, pErr := participantJID(senderStr, chatJID, c.knownLIDFunc(messageStore))
			if pErr != nil {
				// Sending anyway would report success for a reaction that
				// WhatsApp attaches to no message.
				return failed(newSendError(SendErrInvalid, "cannot resolve sender of message %s: %w", messageID, pErr), "")
			}
			senderJID = parsed
		}
	}

	msg := c.Client.BuildReaction(chat, senderJID, msgID, emoji)

	if err := c.antiban.BeforeSend(ctx, antiban.Reaction, 0); err != nil {
		return failed(classify(err, "antiban blocked reaction: %w"), "")
	}
	reactionID := c.prepareMessageID(opts.MessageID, opts.BeforeDispatch)
	resp, err := c.Client.SendMessage(ctx, chat, msg, whatsmeow.SendRequestExtra{ID: reactionID})
	if err != nil {
		return failed(c.sendFailure(chat, antiban.Reaction, err, "failed to send reaction: %w"), string(reactionID))
	}
	c.antiban.AfterSend(antiban.Reaction)

	return bridgeTypes.SendResult{Success: true, MessageID: string(resp.ID), Timestamp: resp.Timestamp}
}

// EditMessage edits a previously sent message
func (c *Client) EditMessage(chatJID, messageID, newContent string) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID: %v", err)
	}
	if se := c.checkAllowlist(chat); se != nil {
		return se
	}

	c.GoOnlineBriefly()

	msgID := types.MessageID(messageID)

	newMsg := &waE2E.Message{
		Conversation: proto.String(newContent),
	}
	msg := c.Client.BuildEdit(chat, msgID, newMsg)

	if c.antiban.Enabled() {
		_ = c.SendTypingIndicator(chatJID, "typing")
	}
	if err := c.antiban.BeforeSend(context.Background(), antiban.Edit, len(newContent)); err != nil {
		_ = c.SendTypingIndicator(chatJID, "paused")
		return fmt.Errorf("antiban blocked edit: %v", err)
	}
	_, err = c.Client.SendMessage(context.Background(), chat, msg)
	_ = c.SendTypingIndicator(chatJID, "paused")
	if err != nil {
		c.antiban.AfterSendFailed(antiban.Edit, err)
		return fmt.Errorf("failed to edit message: %v", err)
	}
	c.antiban.AfterSend(antiban.Edit)

	return nil
}

// DeleteMessage revokes/deletes a message
func (c *Client) DeleteMessage(chatJID, messageID, senderJID string) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID: %v", err)
	}
	if se := c.checkAllowlist(chat); se != nil {
		return se
	}

	c.GoOnlineBriefly()

	msgID := types.MessageID(messageID)

	var sender types.JID
	if senderJID == "" {
		sender = c.Store.ID.ToNonAD() // own message
	} else {
		sender, err = types.ParseJID(senderJID)
		if err != nil {
			return fmt.Errorf("invalid sender JID: %v", err)
		}
	}

	msg := c.Client.BuildRevoke(chat, sender, msgID)

	if err := c.antiban.BeforeSend(context.Background(), antiban.Delete, 0); err != nil {
		return fmt.Errorf("antiban blocked delete: %v", err)
	}
	_, err = c.Client.SendMessage(context.Background(), chat, msg)
	if err != nil {
		c.antiban.AfterSendFailed(antiban.Delete, err)
		return fmt.Errorf("failed to delete message: %v", err)
	}
	c.antiban.AfterSend(antiban.Delete)

	return nil
}

// GetGroupInfo retrieves group metadata
func (c *Client) GetGroupInfo(groupJID string) (*types.GroupInfo, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	jid, err := types.ParseJID(groupJID)
	if err != nil {
		return nil, fmt.Errorf("invalid group JID: %v", err)
	}

	return c.Client.GetGroupInfo(context.Background(), jid)
}

// MarkMessagesRead marks messages as read (blue ticks)
func (c *Client) MarkMessagesRead(chatJID string, messageIDs []string, senderJID string) error {
	return c.markMessages(chatJID, messageIDs, senderJID, types.ReceiptTypeRead)
}

// MarkMessagesPlayed marks voice messages (PTT) as played, which turns the
// microphone icon blue on the sender's side. WhatsApp expects a read receipt
// before the played receipt, so both are sent in order.
func (c *Client) MarkMessagesPlayed(chatJID string, messageIDs []string, senderJID string) error {
	if err := c.markMessages(chatJID, messageIDs, senderJID, types.ReceiptTypeRead); err != nil {
		return err
	}
	return c.markMessages(chatJID, messageIDs, senderJID, types.ReceiptTypePlayed)
}

// markMessages sends a receipt of the given type for the given message IDs.
func (c *Client) markMessages(chatJID string, messageIDs []string, senderJID string, receiptType types.ReceiptType) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID: %v", err)
	}

	c.GoOnlineBriefly()

	ids := make([]types.MessageID, len(messageIDs))
	for i, id := range messageIDs {
		ids[i] = types.MessageID(id)
	}

	var sender types.JID
	if senderJID != "" {
		sender, err = types.ParseJID(senderJID)
		if err != nil {
			return fmt.Errorf("invalid sender JID: %v", err)
		}
	}

	return c.Client.MarkRead(context.Background(), ids, time.Now(), chat, sender, receiptType)
}

// Phase 2: Group Management

// CreateGroup creates a new WhatsApp group
func (c *Client) CreateGroup(name string, participants []string) (*types.GroupInfo, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	// Parse participant JIDs
	participantJIDs := make([]types.JID, len(participants))
	for i, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return nil, fmt.Errorf("invalid participant JID %s: %v", p, err)
		}
		participantJIDs[i] = jid
	}

	req := whatsmeow.ReqCreateGroup{
		Name:         name,
		Participants: participantJIDs,
	}

	return c.Client.CreateGroup(context.Background(), req)
}

// AddGroupParticipants adds members to a group
func (c *Client) AddGroupParticipants(groupJID string, participants []string) ([]types.GroupParticipant, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return nil, fmt.Errorf("invalid group JID: %v", err)
	}

	participantJIDs := make([]types.JID, len(participants))
	for i, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return nil, fmt.Errorf("invalid participant JID %s: %v", p, err)
		}
		participantJIDs[i] = jid
	}

	return c.Client.UpdateGroupParticipants(context.Background(), group, participantJIDs, whatsmeow.ParticipantChangeAdd)
}

// RemoveGroupParticipants removes members from a group
func (c *Client) RemoveGroupParticipants(groupJID string, participants []string) ([]types.GroupParticipant, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return nil, fmt.Errorf("invalid group JID: %v", err)
	}

	participantJIDs := make([]types.JID, len(participants))
	for i, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return nil, fmt.Errorf("invalid participant JID %s: %v", p, err)
		}
		participantJIDs[i] = jid
	}

	return c.Client.UpdateGroupParticipants(context.Background(), group, participantJIDs, whatsmeow.ParticipantChangeRemove)
}

// PromoteGroupParticipant promotes a participant to admin
func (c *Client) PromoteGroupParticipant(groupJID string, participant string) ([]types.GroupParticipant, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return nil, fmt.Errorf("invalid group JID: %v", err)
	}

	jid, err := types.ParseJID(participant)
	if err != nil {
		return nil, fmt.Errorf("invalid participant JID: %v", err)
	}

	return c.Client.UpdateGroupParticipants(context.Background(), group, []types.JID{jid}, whatsmeow.ParticipantChangePromote)
}

// DemoteGroupParticipant demotes an admin to regular participant
func (c *Client) DemoteGroupParticipant(groupJID string, participant string) ([]types.GroupParticipant, error) {
	if !c.IsConnected() {
		return nil, fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return nil, fmt.Errorf("invalid group JID: %v", err)
	}

	jid, err := types.ParseJID(participant)
	if err != nil {
		return nil, fmt.Errorf("invalid participant JID: %v", err)
	}

	return c.Client.UpdateGroupParticipants(context.Background(), group, []types.JID{jid}, whatsmeow.ParticipantChangeDemote)
}

// LeaveGroup leaves a WhatsApp group
func (c *Client) LeaveGroup(groupJID string) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return fmt.Errorf("invalid group JID: %v", err)
	}

	return c.Client.LeaveGroup(context.Background(), group)
}

// SetGroupName updates the group name
func (c *Client) SetGroupName(groupJID string, name string) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return fmt.Errorf("invalid group JID: %v", err)
	}

	return c.Client.SetGroupName(context.Background(), group, name)
}

// SetGroupTopic updates the group description/topic
func (c *Client) SetGroupTopic(groupJID string, topic string) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	group, err := types.ParseJID(groupJID)
	if err != nil {
		return fmt.Errorf("invalid group JID: %v", err)
	}

	return c.Client.SetGroupTopic(context.Background(), group, "", "", topic)
}

// Phase 3: Polls

// CreatePoll creates and sends a poll to a chat, and stores the poll (with its option list)
// locally so that votes on it can later be decrypted and resolved back to readable option text —
// see resolvePollVoteContent in polls.go. A poll this bridge sends never comes back to it as an
// incoming event (self-echo only happens across other linked devices), so this is the only place
// that poll's options are ever captured; skipping it means every future vote on this poll decrypts
// successfully but can't be matched to an option name.
func (c *Client) CreatePoll(messageStore *database.MessageStore, chatJID string, question string, options []string, multiSelect bool) (bridgeTypes.SendResult, error) {
	res := c.CreatePollWithOptions(context.Background(), messageStore, PollOptions{ChatJID: chatJID, Question: question, Options: options, MultiSelect: multiSelect})
	return res, res.Err
}

// CreatePollWithOptions is CreatePoll bounded by ctx, with a typed Err on failure.
func (c *Client) CreatePollWithOptions(ctx context.Context, messageStore *database.MessageStore, opts PollOptions) bridgeTypes.SendResult {
	chatJID, question, options, multiSelect := opts.ChatJID, opts.Question, opts.Options, opts.MultiSelect
	if !c.IsConnected() {
		return failed(newSendError(SendErrNotConnected, "not connected to WhatsApp"), "")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return failed(newSendError(SendErrInvalid, "invalid chat JID: %w", err), "")
	}

	if se := c.checkAllowlist(chat); se != nil {
		return failed(se, "")
	}
	if se := c.checkReachoutGate(messageStore, chat, chatJID); se != nil {
		return failed(se, "")
	}

	c.GoOnlineBriefly()

	// Determine selectable count based on multiSelect
	selectableCount := 1
	if multiSelect {
		selectableCount = len(options)
	}

	// Build poll creation message
	pollMsg := c.Client.BuildPollCreation(question, options, selectableCount)

	// Antiban: typing indicator + rate limit
	if c.antiban.Enabled() {
		_ = c.SendTypingIndicator(chatJID, "typing")
	}
	if err := c.antiban.BeforeSend(ctx, antiban.Poll, len(question)); err != nil {
		_ = c.SendTypingIndicator(chatJID, "paused")
		return failed(classify(err, "antiban blocked poll: %w"), "")
	}

	// Send the poll
	pollID := c.prepareMessageID(opts.MessageID, opts.BeforeDispatch)
	resp, err := c.Client.SendMessage(ctx, chat, pollMsg, whatsmeow.SendRequestExtra{ID: pollID})
	_ = c.SendTypingIndicator(chatJID, "paused")
	if err != nil {
		return failed(c.sendFailure(chat, antiban.Poll, err, "failed to send poll: %w"), string(pollID))
	}
	c.antiban.AfterSend(antiban.Poll)

	msgID := string(resp.ID)
	if err := messageStore.StorePollOptions(msgID, chatJID, options); err != nil {
		c.logger.Warnf("Failed to store poll options for %s: %v", msgID, err)
	}
	var ownJID string
	if c.Store != nil && c.Store.ID != nil {
		ownJID = c.Store.ID.ToNonAD().String()
	}
	if err := messageStore.StoreMessage(
		msgID, chatJID, ownJID, "", fmt.Sprintf("[Poll: %s]", question),
		resp.Timestamp, true, "", "", "", "", nil, nil, nil, 0,
	); err != nil {
		c.logger.Warnf("Failed to store sent poll message %s: %v", msgID, err)
	}

	return bridgeTypes.SendResult{
		Success:   true,
		MessageID: msgID,
		Timestamp: resp.Timestamp,
	}
}

// Phase 4: On-Demand History Request

// RequestChatHistory requests older messages for a specific chat.
// The response will come asynchronously via the HistorySync event handler.
// This requires knowing the oldest message in the chat to request messages before it.
func (c *Client) RequestChatHistory(chatJID string, oldestMsgID string, oldestMsgFromMe bool, oldestMsgSender string, oldestMsgTimestamp int64, count int) error {
	if !c.IsConnected() {
		return fmt.Errorf("not connected to WhatsApp")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID: %v", err)
	}

	ownJID := c.Store.ID.ToNonAD()

	// Create MessageInfo for the oldest known message
	msgInfo := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat,
			IsFromMe: oldestMsgFromMe,
		},
		ID:        types.MessageID(oldestMsgID),
		Timestamp: time.UnixMilli(oldestMsgTimestamp),
	}

	// Set sender correctly: fromMe uses own JID, incoming uses provided sender JID
	if oldestMsgFromMe {
		msgInfo.MessageSource.Sender = ownJID
	} else if oldestMsgSender != "" {
		senderJID, parseErr := types.ParseJID(oldestMsgSender)
		if parseErr == nil {
			msgInfo.MessageSource.Sender = senderJID
		} else {
			msgInfo.MessageSource.Sender = ownJID
		}
	} else if chat.Server != types.GroupServer {
		// An incoming 1:1 message was written by the other side of the chat;
		// anchoring it on our own JID names a message that doesn't exist.
		msgInfo.MessageSource.Sender = chat
	} else {
		msgInfo.MessageSource.Sender = ownJID
	}

	// Build the history sync request
	// Recommended count is 50 messages at a time
	if count <= 0 || count > 50 {
		count = 50
	}

	msg := c.Client.BuildHistorySyncRequest(msgInfo, count)

	// Send as peer message to own device (NOT to the group/chat).
	// History sync is a device-to-device protocol request to our own phone.
	if err := c.antiban.BeforeSend(context.Background(), antiban.Peer, 0); err != nil {
		return fmt.Errorf("antiban blocked history request: %v", err)
	}
	_, err = c.Client.SendMessage(context.Background(), ownJID, msg, whatsmeow.SendRequestExtra{Peer: true})
	if err != nil {
		c.antiban.AfterSendFailed(antiban.Peer, err)
		return fmt.Errorf("failed to send history request: %v", err)
	}
	c.antiban.AfterSend(antiban.Peer)

	return nil
}
