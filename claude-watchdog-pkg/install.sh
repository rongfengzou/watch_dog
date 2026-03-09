#!/usr/bin/env bash
# Claude Watchdog — Install Script
# Installs: claude-watchdog, claude-session, ollama-cli
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-7888}"

echo "=== Claude Watchdog Installer ==="
echo ""

# --- 1. Check dependencies ---
echo "[1/5] Checking dependencies..."

# Python 3.8+
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.8+."
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python: $PY_VER"

# tmux (optional but recommended)
if command -v tmux &>/dev/null; then
    echo "  tmux:   $(tmux -V)"
else
    echo "  tmux:   not found (optional — needed for terminal injection)"
fi

# Ollama (required for LLM features)
if command -v ollama &>/dev/null; then
    echo "  Ollama: found"
    if curl -s --max-time 2 http://localhost:11434/api/tags &>/dev/null; then
        echo "  Ollama: running"
        # Check for qwen3:14b
        if curl -s http://localhost:11434/api/tags | python3 -c "
import sys, json
tags = json.load(sys.stdin).get('models', [])
names = [t['name'] for t in tags]
if any('qwen3:14b' in n for n in names):
    print('  Model:  qwen3:14b found')
else:
    print('  Model:  qwen3:14b NOT found — run: ollama pull qwen3:14b')
" 2>/dev/null; then true; fi
    else
        echo "  Ollama: NOT running — start with: ollama serve"
    fi
else
    echo "  Ollama: not found (required for LLM features)"
    echo "          Install: https://ollama.com/download"
fi

# Claude Code
if command -v claude &>/dev/null; then
    echo "  Claude: $(claude --version 2>/dev/null || echo 'found')"
else
    echo "  Claude: not found (install Claude Code CLI first)"
fi

echo ""

# --- 2. Install binaries ---
echo "[2/5] Installing to $INSTALL_DIR ..."

for cmd in claude-watchdog claude-session ollama-cli; do
    if [ -f "$INSTALL_DIR/$cmd" ]; then
        echo "  Updating $cmd"
    else
        echo "  Installing $cmd"
    fi
    cp "$SCRIPT_DIR/bin/$cmd" "$INSTALL_DIR/$cmd"
    chmod +x "$INSTALL_DIR/$cmd"
done

echo ""

# --- 3. Create data directories ---
echo "[3/5] Creating data directories..."
mkdir -p "$HOME/.claude/watchdog/drives"
mkdir -p "$HOME/.claude/watchdog/project_memory"
echo "  ~/.claude/watchdog/ ready"
echo ""

# --- 4. Configure hooks ---
echo "[4/5] Configuring Claude Code hooks..."

SETTINGS="$HOME/.claude/settings.json"
if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

# Back up existing settings
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"

python3 << 'PYEOF'
import json, sys

port = "${PORT}" if "${PORT}" else "7888"
settings_path = "${SETTINGS}" if "${SETTINGS}" else ""

# Re-read env vars properly
import os
port = os.environ.get("PORT", "7888")
home = os.path.expanduser("~")
settings_path = os.path.join(home, ".claude", "settings.json")

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})

# Define the hooks we need
required_hooks = {
    "SessionStart": [
        {
            "hooks": [{
                "type": "command",
                "command": f"curl -s -X POST http://localhost:{port}/api/memory/inject -H 'Content-Type: application/json' -d @- 2>/dev/null",
                "timeout": 10,
            }]
        }
    ],
    "PreCompact": [
        {
            "hooks": [{
                "type": "command",
                "command": f"curl -s -X POST http://localhost:{port}/api/memory/extract -H 'Content-Type: application/json' -d @- 2>/dev/null",
                "timeout": 60,
            }]
        }
    ],
    "Stop": [
        {
            "hooks": [{
                "type": "command",
                "command": f"curl -s -X POST http://localhost:{port}/api/drive/hook -H 'Content-Type: application/json' -d @- 2>/dev/null",
                "timeout": 120,
            }]
        },
        {
            "hooks": [{
                "type": "command",
                "command": f"curl -s -X POST http://localhost:{port}/api/memory/extract -H 'Content-Type: application/json' -d @- 2>/dev/null",
                "timeout": 10,
            }]
        },
    ],
}

# Merge: add hooks that don't already exist (check by command substring)
for event, hook_list in required_hooks.items():
    existing = hooks.get(event, [])
    for new_hook in hook_list:
        new_cmd = new_hook["hooks"][0].get("command", "")
        # Extract the API path for dedup (e.g., /api/drive/hook)
        import re
        match = re.search(r"/api/\S+", new_cmd)
        api_path = match.group() if match else new_cmd
        already = False
        for eh in existing:
            if isinstance(eh, dict) and "hooks" in eh:
                for h in eh["hooks"]:
                    if api_path in h.get("command", ""):
                        already = True
                        break
        if not already:
            existing.append(new_hook)
    hooks[event] = existing

settings["hooks"] = hooks

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)

print("  Hooks configured (SessionStart, PreCompact, Stop)")
PYEOF

echo ""

# --- 5. Create launchd plist (optional auto-start) ---
echo "[5/5] Optional: auto-start on login"

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.claude.watchdog.plist"
mkdir -p "$PLIST_DIR"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v python3)</string>
        <string>$INSTALL_DIR/claude-watchdog</string>
        <string>--web</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-watchdog.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-watchdog.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "  Created: $PLIST"
echo "  To enable auto-start:"
echo "    launchctl load $PLIST"
echo "  To disable:"
echo "    launchctl unload $PLIST"
echo ""

echo "=== Installation Complete ==="
echo ""
echo "Quick start:"
echo "  claude-watchdog --web --port $PORT    # start watchdog + dashboard"
echo "  claude-session sessions               # list active Claude sessions"
echo "  ollama-cli ask 'hello'                # talk to Ollama"
echo ""
echo "Dashboard: http://localhost:$PORT"
echo "Manual:    $SCRIPT_DIR/MANUAL.md"
