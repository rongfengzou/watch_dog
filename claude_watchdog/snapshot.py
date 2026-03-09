"""Session snapshot building for the dashboard."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import WATCHDOG_DIR, decode_project_path, logger
from .context import extract_last_messages, extract_session_metadata
from .drive_state import load_drive
from .memory import MEMORY_CATEGORIES, load_project_memory
from .scanner import STALL_TYPES, classify_stall, read_tail_entries, scan_sessions
from .terminal import discover_claude_processes


def build_session_snapshot(path: Path, threshold_minutes: float) -> Optional[dict]:
    """Build a rich status snapshot for a single session."""
    try:
        st = path.stat()
    except OSError:
        return None
    session_id = path.stem
    project_dir = path.parent.name
    project = decode_project_path(project_dir)
    age_minutes = (time.time() - st.st_mtime) / 60.0

    entries = read_tail_entries(path)
    meta = extract_session_metadata(entries) if entries else {}
    messages = extract_last_messages(entries) if entries else []
    stall_type = classify_stall(entries) if entries else None

    # For large files the tail may not contain metadata (cwd, slug, etc.)
    # that lives at the head. Read a few lines from the start as fallback.
    if not meta.get("cwd"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        entry = json.loads(line)
                        if not meta.get("cwd") and entry.get("cwd"):
                            meta["cwd"] = entry["cwd"]
                        if not meta.get("slug") and entry.get("slug"):
                            meta["slug"] = entry["slug"]
                        if not meta.get("model") and entry.get("message", {}).get("model"):
                            meta["model"] = entry["message"]["model"]
                        if not meta.get("version") and entry.get("version"):
                            meta["version"] = entry["version"]
                        if meta.get("cwd"):
                            break
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            pass
        # Last resort: derive CWD from project directory
        if not meta.get("cwd") and project:
            meta["cwd"] = project

    # Determine detailed status based on last entry
    # "waiting" = Claude finished, waiting for user input
    # "working" = Claude is actively processing (tools, thinking)
    # "stalled" = inactive too long with abnormal last state
    # "idle"    = inactive, normal end_turn
    last_role = None
    last_stop = None
    meaningful = [e for e in entries if e.get("type") in ("user", "assistant")]
    if meaningful:
        last_entry = meaningful[-1]
        last_role = last_entry.get("type")
        last_stop = last_entry.get("message", {}).get("stop_reason")
    # Check if there are recent progress entries (streaming/tool execution)
    progress_entries = [e for e in entries if e.get("type") == "progress"]
    has_recent_progress = False
    if progress_entries:
        last_prog = progress_entries[-1]
        # progress after the last meaningful entry = still working
        if entries.index(last_prog) > entries.index(meaningful[-1]) if meaningful else True:
            has_recent_progress = True

    if age_minutes < threshold_minutes:
        if last_role == "assistant" and last_stop == "end_turn" and not has_recent_progress:
            status = "waiting"  # Claude done, needs user input
        else:
            status = "working"  # Claude is actively doing something
    elif stall_type is None or stall_type == "user_idle":
        status = "idle"
    else:
        status = "stalled"

    # Check for existing resume file
    resume_content = None
    resume_files = sorted(
        WATCHDOG_DIR.glob(f"resume-{session_id[:8]}-*.md"), reverse=True
    )
    if resume_files:
        try:
            resume_content = resume_files[0].read_text(encoding="utf-8")
        except OSError:
            pass

    snapshot = {
        "session_id": session_id,
        "short_id": session_id[:8],
        "project": project,
        "slug": meta.get("slug", ""),
        "cwd": meta.get("cwd", ""),
        "model": meta.get("model", ""),
        "version": meta.get("version", ""),
        "status": status,
        "stall_type": stall_type if status == "stalled" else None,
        "stall_description": (
            STALL_TYPES.get(stall_type, "") if status == "stalled" else ""
        ),
        "age_minutes": round(age_minutes, 1),
        "last_activity": datetime.fromtimestamp(st.st_mtime).strftime(
            "%H:%M:%S"
        ),
        "size_kb": round(st.st_size / 1024, 1),
        "messages": messages,
        "resume_content": resume_content,
    }

    # Embed drive state if active
    drive = load_drive(session_id[:8])
    if drive and drive.get("state") in ("driving", "paused", "done"):
        snapshot["drive_active"] = drive.get("state") == "driving"
        snapshot["drive_target"] = drive.get("target", "")[:200]
        snapshot["drive_progress_pct"] = (
            drive["log"][-1].get("progress_pct", 0)
            if drive.get("log") else 0
        )
        snapshot["drive_memory"] = drive.get("memory", [])
        snapshot["drive_log"] = drive.get("log", [])[-20:]
        snapshot["drive_iteration"] = drive.get("iteration", 0)
        snapshot["drive_max_iterations"] = drive.get("max_iterations", 50)
        snapshot["drive_state"] = drive.get("state", "")

    # Embed project memory (always, independent of drive)
    pmem = load_project_memory(project)
    has_items = any(pmem.get(c) for c in MEMORY_CATEGORIES)
    if has_items:
        snapshot["project_memory"] = {
            c: pmem.get(c, []) for c in MEMORY_CATEGORIES
        }
        snapshot["project_memory_updated"] = pmem.get("updated_at", "")

    return snapshot


def get_all_snapshots(threshold_minutes: float) -> list[dict]:
    """Build snapshots for all active sessions."""
    sessions = scan_sessions()
    processes = discover_claude_processes()
    # Build CWD -> process map
    cwd_procs: dict[str, dict] = {}
    for p in processes:
        if p.get("cwd"):
            cwd_procs[p["cwd"]] = p

    snapshots = []
    seen_cwds: set[str] = set()
    for path in sessions:
        snap = build_session_snapshot(path, threshold_minutes)
        if not snap:
            continue
        cwd = snap.get("cwd", "")
        # Check if a live process matches this session (exact or subdirectory)
        proc = cwd_procs.get(cwd)
        if not proc and cwd:
            for p_cwd, p in cwd_procs.items():
                if cwd.startswith(p_cwd + "/") or p_cwd.startswith(cwd + "/"):
                    proc = p
                    break
        if proc:
            snap["has_process"] = True
            snap["process_type"] = proc["type"]
            # Only keep the most recent session per CWD that has a process
            if cwd in seen_cwds:
                # Duplicate — older session for same project, skip if
                # the newer one already claimed the live process
                snap["has_process"] = False
                snap["process_type"] = None
                if snap["status"] in ("working", "waiting"):
                    snap["status"] = "idle"
            else:
                seen_cwds.add(cwd)
        else:
            snap["has_process"] = False
            snap["process_type"] = None
            # No live process — downgrade working/waiting to idle
            if snap["status"] in ("working", "waiting"):
                snap["status"] = "idle"
        snapshots.append(snap)
    return snapshots
