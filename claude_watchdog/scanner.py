"""Session scanning and stall detection."""

import glob
import json
import os
import time
from pathlib import Path
from typing import Optional

from .config import CLAUDE_PROJECTS_DIR, TAIL_BYTES

STALL_TYPES = {
    "tool_hung": "Assistant sent tool_use but no tool_result came back",
    "no_response_after_tool_result": "Tool result received but assistant never replied",
    "stream_interrupted": "Assistant message without stop_reason == end_turn",
    "no_response_after_user": "User sent a message but no assistant reply",
    "user_idle": "Normal turn end, user hasn't typed",
}


def scan_sessions() -> list[Path]:
    """Find active JSONL session files, ranked by mtime, skip >24h old."""
    pattern = str(CLAUDE_PROJECTS_DIR / "*" / "*.jsonl")
    paths = glob.glob(pattern)
    now = time.time()
    cutoff = now - 86400  # 24 hours
    active = []
    for p in paths:
        try:
            st = os.stat(p)
            if st.st_mtime > cutoff and st.st_size > 0:
                active.append((st.st_mtime, Path(p)))
        except OSError:
            continue
    active.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in active]


def read_tail_entries(path: Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """Read the last tail_bytes of JSONL and parse entries."""
    size = path.stat().st_size
    offset = max(0, size - tail_bytes)
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if offset > 0:
            f.seek(offset)
            f.readline()  # discard partial first line
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def classify_stall(entries: list[dict]) -> Optional[str]:
    """Classify stall type from the tail entries."""
    meaningful = [
        e for e in entries if e.get("type") in ("user", "assistant")
    ]
    if not meaningful:
        return None

    last = meaningful[-1]
    last_type = last.get("type")
    msg = last.get("message", {})
    content = msg.get("content", [])
    stop_reason = msg.get("stop_reason")

    if last_type == "assistant":
        if stop_reason == "tool_use":
            has_tool_use = False
            if isinstance(content, list):
                has_tool_use = any(
                    b.get("type") == "tool_use"
                    for b in content
                    if isinstance(b, dict)
                )
            if has_tool_use:
                # Check for progress after = tool is executing, not hung
                last_idx = entries.index(last) if last in entries else -1
                if last_idx >= 0:
                    after = entries[last_idx + 1:]
                    if any(e.get("type") in ("progress", "user") for e in after):
                        return None  # tool is running
                return "tool_hung"
        if stop_reason not in ("end_turn", "tool_use"):
            return "stream_interrupted"
        if stop_reason == "end_turn":
            return "user_idle"

    if last_type == "user":
        # Check if there are progress entries AFTER the last user message,
        # which means Claude is actively thinking/streaming — not stalled.
        last_meaningful_idx = entries.index(last) if last in entries else -1
        if last_meaningful_idx >= 0:
            after_entries = entries[last_meaningful_idx + 1:]
            has_progress_after = any(
                e.get("type") in ("progress", "assistant") for e in after_entries
            )
            if has_progress_after:
                return None  # Claude is actively responding

        is_tool_result = False
        is_user_text = False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        is_tool_result = True
                    elif block.get("type") == "text":
                        is_user_text = True
        elif isinstance(content, str):
            is_user_text = True
        if is_tool_result:
            return "no_response_after_tool_result"
        if is_user_text:
            return "no_response_after_user"

    return None


def detect_stall(path: Path, threshold_minutes: float) -> Optional[dict]:
    """Check if a session is stalled. Returns stall info dict or None."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    age_minutes = (time.time() - mtime) / 60.0
    if age_minutes < threshold_minutes:
        return None
    entries = read_tail_entries(path)
    if not entries:
        return None
    stall_type = classify_stall(entries)
    if stall_type is None or stall_type == "user_idle":
        return None
    session_id = path.stem
    return {
        "session_id": session_id,
        "path": str(path),
        "stall_type": stall_type,
        "stall_description": STALL_TYPES.get(stall_type, "Unknown"),
        "age_minutes": round(age_minutes, 1),
        "entries": entries,
    }
