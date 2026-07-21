#!/usr/bin/env python3
"""wa-dispatch PreToolUse hook — enforces prepare-ahead mode.

Blocks outbound WhatsApp sends and destructive/outbound shell commands, even
under --dangerously-skip-permissions (PreToolUse hooks run before the tool and
can veto it regardless of permission mode). Everything else — reading, drafting,
local git, research, building — is allowed.

Exit 0 = allow, exit 2 = deny (stderr is shown to the model).
"""
import sys
import json
import re

# MCP tools are ALLOWLISTED, not denylisted.
#
# The previous denylist named only mcp__whatsapp__* — which left every other
# connected server reachable from a session processing untrusted input:
# protonmail could send mail as Simon, github could push, meta-ads could spend
# money. It also contained two tool names that do not exist
# (create_group, block_user) while missing four that do, so it was blocking
# nothing there either. A denylist over a tool namespace that grows whenever a
# server is added cannot hold; an allowlist fails closed instead.
ALLOWED_MCP = {
    # WhatsApp: read-only surface only
    "mcp__whatsapp__list_messages",
    "mcp__whatsapp__list_chats",
    "mcp__whatsapp__get_chat",
    "mcp__whatsapp__get_message_context",
    "mcp__whatsapp__get_contact_chats",
    "mcp__whatsapp__get_direct_chat_by_contact",
    "mcp__whatsapp__get_last_interaction",
    "mcp__whatsapp__search_contacts",
    "mcp__whatsapp__list_all_contacts",
    "mcp__whatsapp__get_contact_details",
    "mcp__whatsapp__get_contact_context",
    "mcp__whatsapp__get_group_info",
    "mcp__whatsapp__get_nickname",
    "mcp__whatsapp__list_nicknames",
    "mcp__whatsapp__recall",
    "mcp__whatsapp__recall_index_status",
    "mcp__whatsapp__check_inbox",
    "mcp__whatsapp__download_media",
    "mcp__whatsapp__transcribe_audio",
    "mcp__whatsapp__transcribe_audio_file",
    # Reading the vault is how a session learns who it is dealing with.
    "mcp__obsidian__read-note",
    "mcp__obsidian__search-vault",
    "mcp__obsidian__list-available-vaults",
}

# Destructive or outbound shell patterns. Local/reversible git (branch, add,
# commit, checkout) is intentionally allowed so the session can stage work.
DANGER = [
    r"\bgit\b[^\n]*\bpush\b",   # git -C . push, git --git-dir=… push, etc
    r"\bgit\b[^\n]*\breset\b[^\n]*--hard\b",
    r"\brm\s+-[a-z]*[rf]",             # rm -rf / -fr / -r / -f
    r"\bsudo\b",
    r"\blaunchctl\b",
    r"\bpkill\b",
    r"\bkill\s+(-9\s+)?\d",
    r"\b(npm|yarn|pnpm)\s+publish\b",
    r"\bvercel\b.*(--prod|\bdeploy\b)",
    r"\bnetlify\s+deploy\b",
    r"\bdocker(-compose)?\s+(up|push|run)\b",
    r"/api/send\b",                    # bridge send endpoint via curl/wget
    # Interpreters and shells re-entered from Bash defeat any pattern matching
    # on the outer command, so treat them as outbound-capable outright.
    r"\b(sh|bash|zsh|python3?|node|ruby|perl|osascript)\s+-c\b",
    r"\bbase64\s+(-d|--decode)\b",
    # No general egress rule existed at all: curl/wget could exfiltrate freely.
    r"\b(curl|wget)\b",
    r"\b(ssh|scp|rsync|nc|ncat)\b",
    r"\bcrontab\b",
    r"\blaunchctl\b",
    # Writing into shell/agent config is persistence, not a local edit.
    r">>?\s*~?/?[^\s]*(\.zshrc|\.bashrc|\.profile|claude/settings|\.claude\.json)",
    # wa-approve: submitting a draft (/draft) is allowed and expected; actually
    # approving one is Simon's tap on his phone, never a tool call. The token is
    # never handed to the model, so this is belt-and-braces.
    r":8086/(approve|discard)/",
]
DANGER_RE = re.compile("|".join(DANGER), re.IGNORECASE)


def deny(reason: str) -> None:
    print(
        "wa-dispatch prepare-ahead mode: %s "
        "Draft or stage it locally and leave it for Simon to run." % reason,
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Can't parse the hook payload — fall through to Claude Code's own
        # gating rather than break every tool call.
        sys.exit(0)

    tool = data.get("tool_name", "")

    if tool.startswith("mcp__") and tool not in ALLOWED_MCP:
        deny("MCP tool '%s' is not on the prepare-ahead allowlist." % tool)

    if tool == "Bash":
        cmd = (data.get("tool_input") or {}).get("command", "") or ""
        if DANGER_RE.search(cmd):
            deny("shell command matches a blocked pattern: %r." % cmd[:100])

    sys.exit(0)


main()
