"""Web dashboard HTTP handler."""

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from ..config import _web_config, decode_project_path, logger
from ..context import extract_context
from ..drive import _delayed_drive_inject, start_drive, stop_drive
from ..drive_state import load_drive, save_drive
from ..memory import (
    MEMORY_CATEGORIES,
    add_project_memory_items,
    load_project_memory,
    remove_project_memory_items,
    trigger_self_summarize,
)
from ..notify import notify_macos, write_resume_file
from ..ollama import (
    check_significance,
    drive_evaluate,
    extract_facts_via_ollama,
    summarize_stall,
)
from ..scanner import STALL_TYPES, classify_stall, read_tail_entries, scan_sessions
from ..snapshot import build_session_snapshot, get_all_snapshots
from ..terminal import (
    _copy_to_clipboard,
    discover_claude_processes,
    match_session_to_process,
    send_keys_to_target,
)
from ..memory import add_project_facts

_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_STATIC_DIR = Path(__file__).parent
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
}


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the watchdog web dashboard."""

    def log_message(self, format, *args):
        # Suppress default access logging to reduce noise
        pass

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html_str, status=200):
        body = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _serve_static(self, url_path: str):
        """Serve static files from the web/ directory."""
        filename = url_path.split("/static/", 1)[-1]
        file_path = _STATIC_DIR / filename
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        # Security: block path traversal
        try:
            file_path.resolve().relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        content_type = _CONTENT_TYPES.get(
            file_path.suffix, "application/octet-stream",
        )
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._html_response(_DASHBOARD_HTML)
        elif self.path == "/api/sessions":
            threshold = _web_config.get("threshold", 5.0)
            snapshots = get_all_snapshots(threshold)
            self._json_response(snapshots)
        elif self.path == "/api/events":
            self._handle_sse()
        elif self.path.startswith("/static/"):
            self._serve_static(self.path)
        elif self.path.startswith("/api/drive/"):
            short_id = self.path.split("/")[-1]
            drive = load_drive(short_id)
            if drive:
                self._json_response(drive)
            else:
                self._json_response({"error": "no drive found"}, 404)
        else:
            self.send_error(404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        prev_hash = None
        threshold = _web_config.get("threshold", 5.0)
        while True:
            try:
                snapshots = get_all_snapshots(threshold)
                payload = json.dumps(snapshots, ensure_ascii=False)
                cur_hash = hash(payload)
                if cur_hash != prev_hash:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    prev_hash = cur_hash
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(3)
            except (BrokenPipeError, ConnectionError, OSError):
                break

    def do_POST(self):
        if self.path.startswith("/api/summarize/"):
            short_id = self.path.split("/")[-1]
            self._handle_summarize(short_id)
        elif self.path.startswith("/api/send/"):
            short_id = self.path.split("/")[-1]
            self._handle_send(short_id)
        elif self.path == "/api/drive/hook":
            self._handle_drive_hook()
        elif self.path.startswith("/api/drive/start/"):
            short_id = self.path.split("/")[-1]
            self._handle_drive_start(short_id)
        elif self.path.startswith("/api/drive/stop/"):
            short_id = self.path.split("/")[-1]
            self._handle_drive_stop(short_id)
        elif self.path.startswith("/api/drive/target/"):
            short_id = self.path.split("/")[-1]
            self._handle_drive_target(short_id)
        elif self.path == "/api/project_memory":
            self._handle_project_memory_update()
        elif self.path.startswith("/api/memory/summarize/"):
            short_id = self.path.split("/")[-1]
            self._handle_memory_summarize(short_id)
        elif self.path == "/api/memory/extract":
            self._handle_memory_extract()
        elif self.path == "/api/memory/inject":
            self._handle_memory_inject()
        elif self.path == "/api/inject":
            self._handle_inject()
        elif self.path == "/api/inject/waiting":
            self._handle_inject_waiting()
        elif self.path == "/api/copy":
            body = self._read_json_body()
            text = body.get("text", "")
            if text:
                _copy_to_clipboard(text)
                self._json_response({"ok": True})
            else:
                self._json_response({"ok": False, "error": "empty"}, 400)
        else:
            self.send_error(404)

    def _handle_send(self, short_id: str):
        """Send text directly to the session's terminal (tmux or TTY)."""
        body = self._read_json_body()
        text = body.get("text", "").strip()
        if not text:
            self._json_response({"ok": False, "error": "empty text"}, 400)
            return
        processes = discover_claude_processes()
        target = match_session_to_process(short_id, processes)
        if not target:
            self._json_response({
                "ok": False,
                "error": f"No running terminal found for session {short_id}",
                "processes": len(processes),
            }, 404)
            return
        ok, msg = send_keys_to_target(target, text)
        logger.info("Send [%s] -> %s %s: %s", short_id, target["type"], target["target"], msg)
        if ok:
            self._json_response({"ok": True, "method": target["type"], "message": msg})
        else:
            self._json_response({"ok": False, "error": msg}, 500)

    def _resolve_and_send(self, short_id: str, text: str):
        """Resolve a session by short_id and send text to its terminal."""
        processes = discover_claude_processes()
        target = match_session_to_process(short_id, processes)
        if not target:
            self._json_response({
                "ok": False,
                "error": f"No running terminal found for session {short_id}",
            }, 404)
            return
        ok, msg = send_keys_to_target(target, text)
        logger.info("Inject [%s] -> %s %s: %s", short_id, target["type"], target["target"], msg)
        if ok:
            self._json_response({"ok": True, "method": target["type"], "message": msg})
        else:
            self._json_response({"ok": False, "error": msg}, 500)

    def _find_waiting_session(self) -> Optional[str]:
        """Find the most recent session with status 'waiting'."""
        threshold = _web_config.get("threshold", 5.0)
        snapshots = get_all_snapshots(threshold)
        waiting = [s for s in snapshots if s["status"] == "waiting"]
        if not waiting:
            return None
        # Most recent by last_activity
        waiting.sort(key=lambda s: s.get("last_activity", 0), reverse=True)
        return waiting[0]["session_id"][:8]

    def _handle_inject(self):
        """External prompt injection API.

        POST /api/inject
        Body: {"text": "...", "target": "1" | "short_id" | "waiting"}
        """
        body = self._read_json_body()
        text = body.get("text", "").strip()
        if not text:
            self._json_response({"ok": False, "error": "empty text"}, 400)
            return
        target = body.get("target", "waiting")
        if target == "waiting":
            sid = self._find_waiting_session()
            if not sid:
                self._json_response({"ok": False, "error": "No waiting session found"}, 404)
                return
            self._resolve_and_send(sid, text)
        else:
            self._resolve_and_send(target, text)

    def _handle_inject_waiting(self):
        """Shortcut: inject prompt into the most recent waiting session.

        POST /api/inject/waiting
        Body: {"text": "..."}
        """
        body = self._read_json_body()
        text = body.get("text", "").strip()
        if not text:
            self._json_response({"ok": False, "error": "empty text"}, 400)
            return
        sid = self._find_waiting_session()
        if not sid:
            self._json_response({"ok": False, "error": "No waiting session found"}, 404)
            return
        self._resolve_and_send(sid, text)

    def _handle_summarize(self, short_id: str):
        """On-demand summarization for a session."""
        threshold = _web_config.get("threshold", 5.0)
        model = _web_config.get("model", "qwen3:14b")
        # Find the session
        sessions = scan_sessions()
        target = None
        for path in sessions:
            if path.stem.startswith(short_id):
                target = path
                break
        if target is None:
            self._json_response({"ok": False, "error": "session not found"}, 404)
            return

        entries = read_tail_entries(target)
        stall_type = classify_stall(entries)
        if stall_type is None:
            stall_type = "user_idle"
        age_minutes = round((time.time() - target.stat().st_mtime) / 60.0, 1)
        stall = {
            "session_id": target.stem,
            "path": str(target),
            "stall_type": stall_type,
            "stall_description": STALL_TYPES.get(stall_type, "Unknown"),
            "age_minutes": age_minutes,
            "entries": entries,
        }
        summary = summarize_stall(stall, model)
        if summary is None:
            self._json_response({"ok": False, "error": "Ollama unavailable"}, 502)
            return
        out_path = write_resume_file(stall, summary)
        logger.info("Web summarize [%s] -> %s", short_id, out_path)
        self._json_response({"ok": True, "file": str(out_path)})

    def _handle_drive_start(self, short_id: str):
        """Start driving a session toward a target."""
        body = self._read_json_body()
        target = body.get("target", "").strip()
        if not target:
            self._json_response(
                {"ok": False, "error": "empty target"}, 400,
            )
            return
        check_interval = body.get("check_interval", 30)
        max_iterations = body.get("max_iterations", 50)
        model = _web_config.get("model", "qwen3:14b")
        result = start_drive(
            short_id, target, model, check_interval, max_iterations,
        )
        status_code = 200 if result["ok"] else 409
        self._json_response(result, status_code)

    def _handle_drive_stop(self, short_id: str):
        """Stop driving a session."""
        result = stop_drive(short_id)
        self._json_response(result)

    def _handle_drive_hook(self):
        """Handle Claude Code Stop hook for drive mode.

        When Claude Code finishes a turn, its Stop hook POSTs the hook JSON
        here.  If the session has an active drive we evaluate via Ollama and
        return {"decision":"block","reason":"<next instruction>"} so Claude
        continues without stopping.
        """
        body = self._read_json_body()
        session_id = body.get("session_id", "")
        stop_hook_active = body.get("stop_hook_active", False)

        if not session_id:
            self._json_response({})
            return

        short_id = session_id[:8]

        # Prevent infinite loop: if this stop was caused by a previous
        # hook block, don't evaluate again immediately.
        # But schedule a delayed eval+inject so the drive doesn't stall.
        if stop_hook_active:
            self._json_response({})
            drive = load_drive(short_id)
            if drive and drive.get("state") == "driving":
                transcript_path = body.get("transcript_path", "")
                model = _web_config.get("model", "qwen3:14b")
                threading.Thread(
                    target=_delayed_drive_inject,
                    args=(short_id, transcript_path, model),
                    daemon=True,
                ).start()
            return

        # Check if drive is active
        drive = load_drive(short_id)
        if not drive or drive.get("state") != "driving":
            self._json_response({})
            return

        # Check iteration limit
        if drive.get("iteration", 0) >= drive.get("max_iterations", 50):
            drive["state"] = "paused"
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "max_iterations",
                "reasoning": (
                    f"Reached max iterations ({drive['max_iterations']})"
                ),
            })
            save_drive(short_id, drive)
            self._json_response({})
            return

        # Get context from transcript or session file
        transcript_path = body.get("transcript_path", "")
        entries = []
        if transcript_path:
            try:
                entries = read_tail_entries(Path(transcript_path))
            except OSError:
                pass
        if not entries:
            for path in scan_sessions():
                if path.stem.startswith(short_id):
                    entries = read_tail_entries(path)
                    break

        context = extract_context(entries, max_messages=10)
        target = drive.get("target", "")
        memory = drive.get("memory", [])
        model = _web_config.get("model", "qwen3:14b")

        logger.info(
            "Drive hook [%s]: evaluating (iteration %d)...",
            short_id, drive.get("iteration", 0),
        )
        result = drive_evaluate(target, memory, context, model)

        if result is None:
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "eval_failed",
                "reasoning": "Ollama evaluation failed (hook)",
            })
            save_drive(short_id, drive)
            self._json_response({})
            return

        eval_status = result.get("status", "not_done")
        drive["last_eval_at"] = datetime.now(timezone.utc).isoformat()

        if eval_status == "done":
            drive["state"] = "done"
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "done",
                "status": "done",
                "progress_pct": result.get("progress_pct", 100),
                "reasoning": result.get("reasoning", ""),
            })
            save_drive(short_id, drive)
            notify_macos("Claude Drive", f"[{short_id}] Target completed!")
            self._json_response({})
            return

        if eval_status == "blocked":
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "blocked",
                "status": "blocked",
                "progress_pct": result.get("progress_pct", 0),
                "reasoning": result.get("reasoning", ""),
            })
            save_drive(short_id, drive)
            notify_macos(
                "Claude Drive",
                f"[{short_id}] BLOCKED: {result.get('reasoning', '')[:80]}",
            )
            self._json_response({})
            return

        # not_done: update memory
        for item in result.get("memory_add", []):
            if item and item not in memory:
                memory.append(item)
        for item in result.get("memory_remove", []):
            if item in memory:
                memory.remove(item)
        drive["memory"] = memory

        next_instruction = result.get("next_instruction", "")
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "hook_inject",
            "status": "not_done",
            "progress_pct": result.get("progress_pct", 0),
            "instruction": next_instruction[:200],
            "reasoning": result.get("reasoning", ""),
        })
        drive["iteration"] = drive.get("iteration", 0) + 1
        save_drive(short_id, drive)

        if next_instruction:
            # Derive project from transcript path for project memory
            project = ""
            if transcript_path:
                project = decode_project_path(
                    Path(transcript_path).parent.name,
                )
            from ..memory import get_enriched_context_prefix
            prefix = get_enriched_context_prefix(memory, project, context)
            # Include drive target so Claude knows the overall goal
            target_line = f"[DRIVE TARGET: {target}]\n" if target else ""
            enriched = prefix + target_line + next_instruction
            # Persist new drive facts to project memory
            new_facts = result.get("memory_add", [])
            if new_facts and project:
                add_project_facts(project, new_facts)
            self._json_response({
                "decision": "block",
                "reason": enriched,
            })
        else:
            self._json_response({})

    def _handle_drive_target(self, short_id: str):
        """Update the target text for an existing drive."""
        body = self._read_json_body()
        target = body.get("target", "").strip()
        if not target:
            self._json_response(
                {"ok": False, "error": "empty target"}, 400,
            )
            return
        drive = load_drive(short_id)
        if not drive:
            self._json_response(
                {"ok": False, "error": "no drive found"}, 404,
            )
            return
        drive["target"] = target
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "target_updated",
            "reasoning": f"Target updated to: {target[:100]}",
        })
        save_drive(short_id, drive)
        self._json_response({"ok": True})

    def _handle_project_memory_update(self):
        """Add or remove items from structured project memory.

        Body: {"project": "/path/...", "category": "results",
               "add": ["fact"], "remove": ["fact"]}
        """
        body = self._read_json_body()
        project = body.get("project", "").strip()
        if not project:
            self._json_response(
                {"ok": False, "error": "missing project"}, 400,
            )
            return
        cat = body.get("category", "results")
        to_add = body.get("add", [])
        to_remove = body.get("remove", [])
        if to_remove:
            remove_project_memory_items(project, {cat: to_remove})
        mem = load_project_memory(project)
        if to_add:
            mem = add_project_memory_items(project, {cat: to_add})
        self._json_response({"ok": True, "memory": {
            c: mem.get(c, []) for c in MEMORY_CATEGORIES
        }})

    def _handle_memory_summarize(self, short_id: str):
        """Manual trigger: ask Claude to update its project memory."""
        for path in scan_sessions():
            if path.stem.startswith(short_id):
                project = decode_project_path(path.parent.name)
                if project:
                    ok = trigger_self_summarize(short_id, project)
                    self._json_response({"ok": ok})
                    return
        self._json_response({"ok": False, "error": "session not found"}, 404)

    def _handle_memory_extract(self):
        """Auto-extract facts from Claude's output (Stop / PreCompact hook).

        Stop: significance check -> self-summarize trigger (Claude updates its own memory).
        PreCompact: Ollama extraction fallback (can't inject during compaction).
        """
        body = self._read_json_body()
        transcript_path = body.get("transcript_path", "")
        last_message = body.get("last_assistant_message", "")
        hook_event = body.get("hook_event_name", "")
        session_id = body.get("session_id", "")
        logger.info(
            "Memory extract hook called: event=%s, has_last_msg=%s (%d chars), path=%s",
            hook_event, bool(last_message), len(last_message or ""),
            transcript_path[-60:] if transcript_path else "",
        )

        # Derive project from transcript path
        project = ""
        if transcript_path:
            project = decode_project_path(
                Path(transcript_path).parent.name,
            )
        if not project:
            self._json_response({})
            return

        # --- PreCompact: keep existing Ollama extraction (unchanged) ---
        if hook_event == "PreCompact":
            text = ""
            if last_message:
                text = last_message
            elif transcript_path:
                try:
                    entries = read_tail_entries(
                        Path(transcript_path), tail_bytes=200 * 1024,
                    )
                    text = extract_context(entries, max_messages=50)
                except OSError:
                    pass

            if not text or len(text.strip()) < 200:
                logger.info("Memory extract: skipped (text too short: %d chars)", len(text or ""))
                self._json_response({})
                return

            logger.info("Memory extract (PreCompact): proceeding with %d chars for project=%s", len(text), project)
            model = _web_config.get("model", "qwen3:14b")
            if len(text) > 20000:
                text = text[-20000:]
            existing = load_project_memory(project)

            def _do_extract():
                result = extract_facts_via_ollama(text, model, existing)
                if not result:
                    return
                to_remove = result.get("remove", {})
                to_add = result.get("add", {})
                if to_remove:
                    remove_project_memory_items(project, to_remove)
                    rm_summary = "; ".join(
                        f"{k}: {v}" for k, v in to_remove.items() if v
                    )
                    logger.info(
                        "Auto-removed superseded items for %s: %s",
                        project, rm_summary[:200],
                    )
                if to_add:
                    add_project_memory_items(project, to_add)
                    total = sum(len(v) for v in to_add.values())
                    summary = "; ".join(
                        f"{k}: {v}" for k, v in to_add.items() if v
                    )
                    logger.info(
                        "Auto-extracted %d items for %s via PreCompact: %s",
                        total, project, summary[:200],
                    )

            # Synchronous — memory must be saved BEFORE context is lost
            _do_extract()
            self._json_response({})
            return

        # --- Stop: significance check -> self-summarize trigger ---
        text = last_message or ""
        if not text or len(text.strip()) < 200:
            self._json_response({})
            return

        lower = text.lower()
        skip_phrases = [
            "ready for your next", "let me know", "want me to",
            "here's what", "done.", "fixed.", "cleaned up",
            "is there anything else",
        ]
        if len(text.strip()) < 500 and any(p in lower for p in skip_phrases):
            logger.info("Memory extract (Stop): skipped (routine phrase match)")
            self._json_response({})
            return

        short_id = Path(transcript_path).stem[:8] if transcript_path else session_id[:8]
        model = _web_config.get("model", "qwen3:14b")

        def _check_and_trigger():
            if check_significance(text, model):
                logger.info("Significance=YES for %s, triggering self-summarize", short_id)
                time.sleep(3)  # wait for Claude to settle into idle
                trigger_self_summarize(short_id, project)
            else:
                logger.info("Significance=NO for %s, skipping", short_id)

        threading.Thread(target=_check_and_trigger, daemon=True).start()
        self._json_response({})

    def _handle_memory_inject(self):
        """Inject project memory into a new/resumed session (SessionStart hook).

        Returns hookSpecificOutput.additionalContext with project facts
        so Claude starts the session with persistent memory.
        """
        body = self._read_json_body()
        transcript_path = body.get("transcript_path", "")

        project = ""
        if transcript_path:
            project = decode_project_path(
                Path(transcript_path).parent.name,
            )
        if not project:
            self._json_response({})
            return

        pmem = load_project_memory(project)
        has_items = any(pmem.get(c) for c in MEMORY_CATEGORIES)
        if not has_items:
            self._json_response({})
            return

        # Format structured memory for Claude
        lines = []
        labels = {
            "constraints": "Constraints & Failed Approaches (DO NOT retry these)",
            "results": "Known Results (verify — may be outdated)",
            "decisions": "Decisions Made",
            "working_config": "Working Config",
        }
        for cat in MEMORY_CATEGORIES:
            items = pmem.get(cat, [])
            if items:
                lines.append(f"{labels[cat]}:")
                for item in items:
                    lines.append(f"  - {item}")
        context = (
            "[Project Memory — from previous sessions]\n"
            + "\n".join(lines)
        )

        self._json_response({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        })
