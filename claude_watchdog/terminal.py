"""Terminal process discovery and keystroke injection."""

import glob as glob_mod
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from .config import TAIL_BYTES, decode_project_path, logger
from .scanner import read_tail_entries, scan_sessions


def _copy_to_clipboard(text: str) -> None:
    """Copy text to macOS clipboard via pbcopy."""
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=3)
    except (subprocess.SubprocessError, OSError):
        pass


def discover_claude_processes() -> list[dict]:
    """Find all running claude CLI processes and how to reach them.

    Returns list of dicts:
      {pid, cwd, type: "tmux"|"tty", target: pane_spec|tty_path, socket: str|None}
    """
    # 1. Find all claude PIDs (CLI only, skip Claude.app)
    try:
        out = subprocess.run(
            ["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return []
    if not out:
        return []
    pids = [int(p) for p in out.split("\n") if p.strip()]

    # 2. Build tmux pane map: shell_pid -> {socket, pane_id}
    tmux_map = {}  # shell_pid -> {socket, pane}
    # Check default tmux server
    for socket in [None]:  # default server
        try:
            args = ["tmux", "list-panes", "-a", "-F",
                    "#{pane_pid}\t#{pane_id}"]
            res = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) == 2:
                        tmux_map[int(parts[0])] = {"socket": None, "pane": parts[1]}
        except (subprocess.SubprocessError, OSError):
            pass
    # Check named tmux sockets (claude-swarm-*)
    try:
        tmp_sockets = glob_mod.glob("/tmp/tmux-*/claude-swarm-*")
        for sock_path in tmp_sockets:
            sock_name = os.path.basename(sock_path)
            try:
                args = ["tmux", "-L", sock_name, "list-panes", "-a", "-F",
                        "#{pane_pid}\t#{pane_id}"]
                res = subprocess.run(args, capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    for line in res.stdout.strip().split("\n"):
                        parts = line.split("\t")
                        if len(parts) == 2:
                            tmux_map[int(parts[0])] = {
                                "socket": sock_name, "pane": parts[1],
                            }
            except (subprocess.SubprocessError, OSError):
                pass
    except Exception:
        pass

    # 3. For each claude PID, determine type and target
    result = []
    for pid in pids:
        try:
            info = subprocess.run(
                ["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            continue
        if not info:
            continue
        parts = info.split()
        if len(parts) < 2:
            continue
        ppid = int(parts[0])
        tty = parts[1]

        # Get CWD via lsof
        cwd = None
        try:
            lsof_out = subprocess.run(
                ["lsof", "-p", str(pid), "-Fn"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in lsof_out.split("\n"):
                if line.startswith("n") and "/Users/" in line:
                    # The 'cwd' entry is the first 'n' line after a 'f' line for cwd
                    pass
            # Simpler: use pwdx equivalent
            lsof_out = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in lsof_out.split("\n"):
                if line.startswith("n/"):
                    cwd = line[1:]
                    break
        except (subprocess.SubprocessError, OSError):
            pass

        # Check if parent shell is in a tmux pane
        if ppid in tmux_map:
            result.append({
                "pid": pid, "cwd": cwd,
                "type": "tmux",
                "target": tmux_map[ppid]["pane"],
                "socket": tmux_map[ppid]["socket"],
            })
        elif tty and tty != "??":
            result.append({
                "pid": pid, "cwd": cwd,
                "type": "tty",
                "target": f"/dev/{tty}",
                "socket": None,
            })

    return result


def match_session_to_process(
    short_id: str, processes: list[dict],
) -> Optional[dict]:
    """Match a watchdog session to a running claude process by CWD."""
    # Get session CWD from JSONL tail
    session_cwd = None
    session_path = None
    for path in scan_sessions():
        if path.stem.startswith(short_id):
            session_path = path
            entries = read_tail_entries(path, tail_bytes=TAIL_BYTES)
            for e in entries:
                if e.get("cwd"):
                    session_cwd = e["cwd"]
                    break
            break

    # Fallback: read CWD from JSONL head (for large files where tail
    # no longer contains the initial metadata entries)
    if not session_cwd and session_path:
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        entry = json.loads(line)
                        if entry.get("cwd"):
                            session_cwd = entry["cwd"]
                            break
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            pass

    # Last resort: derive CWD from project directory name
    if not session_cwd and session_path:
        project = decode_project_path(session_path.parent.name)
        if project:
            session_cwd = project

    if not session_cwd:
        return None

    # Exact match first
    matches = [p for p in processes if p.get("cwd") == session_cwd]
    if matches:
        return matches[0]
    # Fuzzy match: session CWD is child of process CWD or vice versa
    for p in processes:
        p_cwd = p.get("cwd", "")
        if not p_cwd:
            continue
        if session_cwd.startswith(p_cwd + "/") or p_cwd.startswith(session_cwd + "/"):
            return p
    return None


def send_keys_to_target(target: dict, text: str) -> tuple[bool, str]:
    """Send keystrokes to a tmux pane or TTY. Returns (ok, message)."""
    if target["type"] == "tmux":
        base = ["tmux"]
        if target["socket"]:
            base += ["-L", target["socket"]]
        pane = target["target"]
        try:
            # Send text literally (-l prevents key name interpretation)
            res = subprocess.run(
                base + ["send-keys", "-t", pane, "-l", text],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode != 0:
                return False, f"tmux text error: {res.stderr}"
            # Send Enter separately
            res = subprocess.run(
                base + ["send-keys", "-t", pane, "Enter"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                return True, f"Sent to tmux pane {pane}"
            return False, f"tmux Enter error: {res.stderr}"
        except Exception as e:
            return False, str(e)

    elif target["type"] == "tty":
        # Strategy: copy text to clipboard, activate the Terminal tab,
        # then use System Events to paste (Cmd+V) and press Enter.
        tty_path = target["target"]

        # Step 1: copy text to clipboard
        _copy_to_clipboard(text)

        # Step 2: activate Terminal and select the correct tab
        activate_script = f'''
tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{tty_path}" then
        set selected of t to true
        set index of w to 1
        return "ok"
      end if
    end repeat
  end repeat
  return "tab not found"
end tell
'''
        try:
            res = subprocess.run(
                ["osascript", "-e", activate_script],
                capture_output=True, text=True, timeout=10,
            )
            if "tab not found" in res.stdout:
                return False, f"Terminal tab not found for {tty_path}"
        except Exception as e:
            return False, f"Failed to activate Terminal: {e}"

        # Step 3: paste via System Events Cmd+V
        paste_script = '''
tell application "System Events"
  tell process "Terminal"
    keystroke "v" using command down
  end tell
end tell
'''
        try:
            res = subprocess.run(
                ["osascript", "-e", paste_script],
                capture_output=True, text=True, timeout=10,
            )
            if res.returncode == 0:
                return True, f"Pasted to Terminal {tty_path} — press Enter to submit"
            # System Events blocked — clipboard-only
            logger.warning("System Events blocked: %s", res.stderr.strip())
            return (
                True,
                f"Copied to clipboard & Terminal activated. "
                f"Press Cmd+V then Enter ({tty_path})",
            )
        except Exception as e:
            logger.warning("System Events error: %s", e)
            return (
                True,
                f"Copied to clipboard & Terminal activated. "
                f"Press Cmd+V then Enter ({tty_path})",
            )

    return False, "unknown target type"
