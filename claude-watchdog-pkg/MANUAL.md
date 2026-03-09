# Claude Watchdog — Manual

A monitoring and autonomous orchestration system for Claude Code sessions.
Three CLI tools, zero external Python dependencies.

## Components

| Command | Purpose |
|---------|---------|
| `claude-watchdog` | Session monitor, web dashboard, drive mode, memory system |
| `claude-session` | CLI client to interact with sessions |
| `ollama-cli` | Talk to local Ollama models |

## Requirements

- **Python 3.8+** (stdlib only, no pip packages)
- **Ollama** with `qwen3:14b` model (for LLM features)
- **Claude Code CLI** (the thing being monitored)
- **tmux** (recommended, for terminal injection)
- **macOS** (notifications, Terminal.app integration)

## Installation

```bash
cd claude-watchdog-pkg
./install.sh
```

This will:
1. Copy `claude-watchdog`, `claude-session`, `ollama-cli` to `/usr/local/bin`
2. Create `~/.claude/watchdog/` data directories
3. Configure Claude Code hooks in `~/.claude/settings.json`
4. Create a launchd plist for auto-start (optional)

Custom install dir and port:
```bash
INSTALL_DIR=~/bin PORT=9000 ./install.sh
```

### Ollama Setup

```bash
# Install Ollama (if not installed)
brew install ollama        # or https://ollama.com/download

# Start Ollama server
ollama serve

# Pull the model used by watchdog
ollama pull qwen3:14b
```

### Auto-Start on Login

```bash
launchctl load ~/Library/LaunchAgents/com.claude.watchdog.plist
```

To disable:
```bash
launchctl unload ~/Library/LaunchAgents/com.claude.watchdog.plist
```

## Usage

### 1. Start the Watchdog

```bash
claude-watchdog --web --port 7888
```

Opens web dashboard at `http://localhost:7888`. Monitors all Claude Code
sessions, detects stalls, manages project memory.

Flags:
```
--web              Start web dashboard + API server
--port N           Dashboard port (default: 7888)
--threshold N      Minutes before stall alert (default: 5)
--interval N       Poll interval in seconds (default: 30)
--model M          Ollama model (default: qwen3:14b)
--once             Single check, then exit
--foreground       Verbose debug logging
```

### 2. Session Management (claude-session)

```bash
claude-session sessions          # list active sessions (alias: ls)
claude-session status            # all sessions including stalled
claude-session watch             # live-refresh every 5s
```

#### Talk to a Session

```bash
claude-session talk "continue the work"       # send to most recent waiting
claude-session talk 1 "fix the bug"           # send to session #1
claude-session talk abc12345 "do this"        # send by short_id
```

#### Live Talk (stream response)

```bash
claude-session lt 1 "implement the feature"   # send + stream Claude's reply
```

#### History & Summarize

```bash
claude-session history 1         # last exchange (alias: h)
claude-session summarize 1       # Ollama-generated summary (alias: sum)
```

### 3. Ollama CLI

```bash
ollama-cli ask "what is pKa?"                 # one-shot question
ollama-cli ask -m qwen3:14b "quick q"         # specific model
ollama-cli ask -f "fast answer"               # no-think mode (faster)
ollama-cli chat                               # interactive multi-turn
ollama-cli chat -s "You are a chemist"        # with system prompt
ollama-cli models                             # list available models
ollama-cli run prompt.txt                     # file as prompt
echo "text" | ollama-cli pipe                 # stdin mode
```

Flags (all commands):
```
-m, --model <name>     Model (default: qwen3:32b)
-s, --system <text>    System prompt
-f, --no-think         Disable thinking mode (faster)
```

## Drive Mode

Autonomous goal-directed orchestration. The watchdog uses Ollama to evaluate
Claude's progress and inject the next instruction automatically.

### Start a Drive

From the web dashboard, or CLI:
```bash
claude-watchdog --drive --session 1 --target "fix all test failures"
```

Or via API:
```bash
curl -X POST http://localhost:7888/api/drive/start/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"target": "make all tests pass", "max_iterations": 50}'
```

### How It Works

1. Claude finishes a turn -> Stop hook fires
2. Watchdog evaluates progress via Ollama (is the target met?)
3. If not done: returns `{"decision":"block","reason":"<next instruction>"}`
4. Claude receives the instruction as a new prompt and continues
5. Repeat until done, blocked, or max iterations

### Drive States

| State | Meaning |
|-------|---------|
| `driving` | Active, evaluating + injecting |
| `done` | Target achieved |
| `paused` | Max iterations or repeated failures |
| `stopped` | Manually stopped |

### Configuration

```
--check-interval N    Seconds between eval cycles (default: 30)
--max-iterations N    Max eval cycles before pause (default: 50)
```

## Project Memory

Persistent memory per project, survives across sessions. Four categories:

| Category | Purpose | Limit |
|----------|---------|-------|
| `constraints` | Failed approaches, rules, "do not" items | 30 |
| `results` | Metrics, benchmarks, outcomes | 15 |
| `decisions` | Why A over B | 10 |
| `working_config` | Paths, commands, formulas | 15 |

### How Memory Flows

```
SessionStart hook  ->  Inject existing memory as context prefix
Stop hook          ->  Extract new facts from conversation
PreCompact hook    ->  Extract facts before context window compacts
Drive inject       ->  Include relevant memory in each instruction
```

### Smart Injection

Memory items are ranked by **keyword relevance** to the current transcript.
If Claude is working on "ecoul for 3ert", constraints about Coulomb errors
are prioritized over unrelated items. Falls back to recency if no context.

### Manage via API

```bash
# Add items
curl -X POST http://localhost:7888/api/project_memory \
  -H 'Content-Type: application/json' \
  -d '{"project":"/path/to/project","action":"add","items":{"constraints":["do not use X"]}}'

# Remove items
curl -X POST http://localhost:7888/api/project_memory \
  -H 'Content-Type: application/json' \
  -d '{"project":"/path/to/project","action":"remove","items":{"results":["old metric"]}}'
```

Or use the web dashboard memory panel.

## Web Dashboard

`http://localhost:7888` — real-time session monitoring.

Features:
- Session list with status badges (working/waiting/idle/stalled)
- Last messages preview
- Terminal injection (send text to any session)
- Drive control panel (start/stop, progress log)
- Project memory editor (add/remove by category)
- Stall analysis with Ollama-generated resume prompts

## Hooks Reference

The installer configures these Claude Code hooks:

| Hook | Endpoint | Purpose |
|------|----------|---------|
| SessionStart | `/api/memory/inject` | Load project memory into new session |
| PreCompact | `/api/memory/extract` | Save facts before context compaction |
| Stop (1) | `/api/drive/hook` | Drive mode evaluation + instruction |
| Stop (2) | `/api/memory/extract` | Extract facts after each turn |

## Data Storage

```
~/.claude/watchdog/
  state.json                          # stall alert tracking
  drives/{short_id}.json              # drive state per session
  project_memory/{project_slug}.json  # persistent memory per project
  resume-{id}-{ts}.md                 # generated resume prompts
```

## Troubleshooting

### Watchdog not detecting sessions
- Sessions live in `~/.claude/projects/*/`. Check with `ls ~/.claude/projects/`
- Sessions older than 24h are ignored

### Drive not injecting
- Check `has_process` on dashboard — needs tmux or Terminal.app process
- For large session files (>50MB), CWD detection reads from file head
- Subdirectory CWD matching is supported (project/build/ matches project/)

### "Pasted text" appears in terminal
- Watchdog injected text while Claude was still working
- Fixed: delayed inject now checks session status before pasting

### No Ollama connection
- Start Ollama: `ollama serve`
- Check: `curl http://localhost:11434/api/tags`
- Pull model: `ollama pull qwen3:14b`

### Port conflict
- Change port: `claude-watchdog --web --port 9000`
- Update hooks in `~/.claude/settings.json` to match

### Hooks not firing
- Verify: `cat ~/.claude/settings.json | python3 -m json.tool`
- Hooks need the watchdog running before Claude Code starts the session

## Architecture

```
                    ┌─────────────┐
                    │  Ollama     │
                    │ (qwen3:14b) │
                    └──────┬──────┘
                           │ evaluate / extract
                           │
┌──────────┐  hooks   ┌────┴──────────┐  send-keys   ┌─────────────┐
│  Claude  │ -------> │  Watchdog     │ ----------->  │  Terminal   │
│  Code    │ <------- │  (port 7888)  │               │  (tmux)     │
└──────────┘  block   └────┬──────────┘               └─────────────┘
                           │ HTTP API
                    ┌──────┴──────┐
                    │  Dashboard  │
                    │  + CLI      │
                    └─────────────┘
```

- **Claude Code** sends hook events (SessionStart, Stop, PreCompact) to watchdog
- **Watchdog** evaluates via Ollama, returns block/allow, manages memory
- **Terminal injection** via tmux `send-keys` for drive mode
- **Dashboard** provides web UI for monitoring and control
- **claude-session** provides CLI access to the same API
