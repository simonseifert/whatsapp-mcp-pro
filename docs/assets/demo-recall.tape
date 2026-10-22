# README demo: recall across languages. Fictional data only.
# Re-record from the repo root, with the pro-cpu extra installed in the active env
# and ttyd + ffmpeg on PATH:
#   vhs docs/assets/demo-recall.tape && gifsicle -O3 --lossy=80 -b docs/assets/demo-recall.gif
# DEMO_STORE (default ~/.cache/wa-demo-store) holds the throwaway demo messages.db.

Output docs/assets/demo-recall.gif

Set Shell bash
Set FontSize 16
Set Width 1000
Set Height 520
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Framerate 12
Set CursorBlink false

Hide
Type "export PS1='$ ' && cd whatsapp-mcp-server && S=${DEMO_STORE:-$HOME/.cache/wa-demo-store}"
Enter
Type "python scripts/demo_recall.py --store $S --build >/dev/null 2>&1 && python scripts/demo_recall.py --store $S --repl 2>/dev/null"
Enter
Wait+Screen@60s /fictional messages/
Show

Sleep 600ms
Type "when is the proposal due?"
Sleep 300ms
Enter
Sleep 3500ms
Type "what price did we agree for the logo?"
Sleep 300ms
Enter
Sleep 5s
