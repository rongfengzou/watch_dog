"""Autonomous drive mode: Ollama-driven Claude orchestration."""

import threading
import time
from datetime import datetime, timezone

from .config import _web_config, decode_project_path, logger
from .context import extract_context
from .drive_state import load_drive, list_active_drives, save_drive
from .memory import add_project_facts, get_enriched_context_prefix
from .notify import notify_macos
from .ollama import drive_evaluate
from .scanner import read_tail_entries, scan_sessions
from .snapshot import build_session_snapshot
from .terminal import (
    discover_claude_processes,
    match_session_to_process,
    send_keys_to_target,
)

_active_drives: dict[str, threading.Thread] = {}


def drive_session_loop(
    short_id: str, check_interval: float, model: str,
) -> None:
    """Drive loop for a single session. Runs in a thread."""
    logger.info(
        "Drive loop started for [%s] (interval=%ds)", short_id, check_interval,
    )
    while True:
        time.sleep(check_interval)
        drive = load_drive(short_id)
        if not drive or drive.get("state") != "driving":
            logger.info("Drive [%s] no longer active, exiting loop.", short_id)
            break

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
            notify_macos(
                "Claude Drive",
                f"[{short_id}] Max iterations reached, paused.",
            )
            break

        # Find the session path
        session_path = None
        for path in scan_sessions():
            if path.stem.startswith(short_id):
                session_path = path
                break
        if not session_path:
            logger.warning("Drive [%s]: session not found, skipping.", short_id)
            continue

        # Check session status
        threshold = _web_config.get("threshold", 5.0)
        snap = build_session_snapshot(session_path, threshold)
        if not snap:
            continue
        status = snap.get("status")
        if status not in ("waiting", "idle", "stalled"):
            # Claude is still working, skip
            continue

        # Read recent context
        entries = read_tail_entries(session_path)
        context = extract_context(entries, max_messages=10)

        # Evaluate via Ollama
        target = drive.get("target", "")
        memory = drive.get("memory", [])
        logger.info(
            "Drive [%s]: evaluating progress (iteration %d)...",
            short_id, drive.get("iteration", 0),
        )
        result = drive_evaluate(target, memory, context, model)
        if result is None:
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "eval_failed",
                "reasoning": "Ollama evaluation failed",
            })
            save_drive(short_id, drive)
            continue

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
            logger.info("Drive [%s]: TARGET DONE.", short_id)
            break

        if eval_status == "blocked":
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "blocked",
                "status": "blocked",
                "progress_pct": result.get("progress_pct", 0),
                "reasoning": result.get("reasoning", ""),
                "next_instruction": result.get("next_instruction", ""),
            })
            save_drive(short_id, drive)
            notify_macos(
                "Claude Drive",
                f"[{short_id}] BLOCKED: {result.get('reasoning', '')[:80]}",
            )
            continue

        # status == "not_done": update memory and inject
        for item in result.get("memory_add", []):
            if item and item not in memory:
                memory.append(item)
        for item in result.get("memory_remove", []):
            if item in memory:
                memory.remove(item)
        drive["memory"] = memory

        next_instruction = result.get("next_instruction", "")
        if next_instruction:
            # Inject into terminal with project + drive memory context
            project = decode_project_path(session_path.parent.name)
            prefix = get_enriched_context_prefix(memory, project, context)
            target_line = f"[DRIVE TARGET: {target}]\n" if target else ""
            enriched = prefix + target_line + next_instruction
            # Also persist new drive memory facts to project memory
            new_facts = result.get("memory_add", [])
            if new_facts:
                add_project_facts(project, new_facts)
            processes = discover_claude_processes()
            proc_target = match_session_to_process(short_id, processes)
            if proc_target:
                ok, msg = send_keys_to_target(proc_target, enriched)
                logger.info(
                    "Drive [%s]: injected -> %s %s: %s",
                    short_id, proc_target["type"], proc_target["target"], msg,
                )
                drive["log"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": "inject",
                    "status": "not_done",
                    "progress_pct": result.get("progress_pct", 0),
                    "instruction": next_instruction[:200],
                    "reasoning": result.get("reasoning", ""),
                    "send_ok": ok,
                    "send_msg": msg,
                })
            else:
                logger.warning(
                    "Drive [%s]: no terminal found for injection.", short_id,
                )
                drive["log"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": "inject_failed",
                    "status": "not_done",
                    "progress_pct": result.get("progress_pct", 0),
                    "instruction": next_instruction[:200],
                    "reasoning": result.get("reasoning", ""),
                    "error": "No terminal found",
                })
        else:
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "eval",
                "status": "not_done",
                "progress_pct": result.get("progress_pct", 0),
                "reasoning": result.get("reasoning", ""),
            })

        drive["iteration"] = drive.get("iteration", 0) + 1

        # Auto-pause after 5 consecutive inject failures
        recent = drive["log"][-5:]
        if len(recent) >= 5 and all(
            e.get("action") in ("inject_failed", "blocked") for e in recent
        ):
            drive["state"] = "paused"
            drive["log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "auto_paused",
                "reasoning": "5 consecutive failures — no terminal or blocked",
            })
            save_drive(short_id, drive)
            notify_macos(
                "Claude Drive",
                f"[{short_id}] Auto-paused: repeated failures",
            )
            break

        save_drive(short_id, drive)

    # Clean up thread reference
    _active_drives.pop(short_id, None)
    logger.info("Drive loop ended for [%s].", short_id)


def _delayed_drive_inject(
    short_id: str, transcript_path: str, model: str,
) -> None:
    """Background: wait for Claude to settle, then evaluate + inject via terminal.

    Called when stop_hook_active=true to prevent the drive from stalling.
    Cannot use hook "block" response (already returned {}), so injects via
    terminal send-keys instead.
    """
    time.sleep(5)
    drive = load_drive(short_id)
    if not drive or drive.get("state") != "driving":
        return
    if drive.get("iteration", 0) >= drive.get("max_iterations", 50):
        return

    # Find session path
    from pathlib import Path as _Path

    session_path = None
    if transcript_path:
        tp = _Path(transcript_path)
        if tp.exists():
            session_path = tp
    if not session_path:
        for p in scan_sessions():
            if p.stem.startswith(short_id):
                session_path = p
                break
    if not session_path:
        logger.warning("Delayed drive inject [%s]: session not found", short_id)
        return

    # Only inject if Claude is idle/waiting — never paste while working
    threshold = _web_config.get("threshold", 5.0)
    snap = build_session_snapshot(session_path, threshold)
    if snap and snap.get("status") not in ("waiting", "idle", "stalled"):
        logger.info(
            "Delayed drive inject [%s]: skipped, session is %s",
            short_id, snap.get("status"),
        )
        return

    entries = read_tail_entries(session_path)
    context = extract_context(entries, max_messages=10)
    target = drive.get("target", "")
    memory = drive.get("memory", [])

    logger.info(
        "Delayed drive inject [%s]: evaluating (iteration %d)...",
        short_id, drive.get("iteration", 0),
    )
    result = drive_evaluate(target, memory, context, model)
    if result is None:
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "eval_failed",
            "reasoning": "Ollama evaluation failed (delayed inject)",
        })
        save_drive(short_id, drive)
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
        logger.info("Delayed drive inject [%s]: TARGET DONE.", short_id)
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
        return

    # not_done: update memory and inject via terminal
    for item in result.get("memory_add", []):
        if item and item not in memory:
            memory.append(item)
    for item in result.get("memory_remove", []):
        if item in memory:
            memory.remove(item)
    drive["memory"] = memory

    next_instruction = result.get("next_instruction", "")
    if not next_instruction:
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "eval",
            "status": "not_done",
            "progress_pct": result.get("progress_pct", 0),
            "reasoning": result.get("reasoning", ""),
        })
        drive["iteration"] = drive.get("iteration", 0) + 1
        save_drive(short_id, drive)
        return

    project = decode_project_path(session_path.parent.name)
    prefix = get_enriched_context_prefix(memory, project, context)
    target_line = f"[DRIVE TARGET: {target}]\n" if target else ""
    enriched = prefix + target_line + next_instruction

    new_facts = result.get("memory_add", [])
    if new_facts and project:
        add_project_facts(project, new_facts)

    processes = discover_claude_processes()
    proc_target = match_session_to_process(short_id, processes)
    if proc_target:
        ok, msg = send_keys_to_target(proc_target, enriched)
        logger.info(
            "Delayed drive inject [%s]: injected -> %s %s: %s",
            short_id, proc_target["type"], proc_target["target"], msg,
        )
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "inject",
            "status": "not_done",
            "progress_pct": result.get("progress_pct", 0),
            "instruction": next_instruction[:200],
            "reasoning": result.get("reasoning", ""),
            "send_ok": ok,
            "send_msg": msg,
        })
    else:
        logger.warning(
            "Delayed drive inject [%s]: no terminal found.", short_id,
        )
        drive["log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "inject_failed",
            "status": "not_done",
            "progress_pct": result.get("progress_pct", 0),
            "instruction": next_instruction[:200],
            "reasoning": result.get("reasoning", ""),
            "error": "No terminal found (delayed inject)",
        })

    drive["iteration"] = drive.get("iteration", 0) + 1
    save_drive(short_id, drive)


def start_drive(
    short_id: str, target: str, model: str,
    check_interval: float = 30, max_iterations: int = 50,
) -> dict:
    """Start driving a session toward a target."""
    if short_id in _active_drives and _active_drives[short_id].is_alive():
        return {"ok": False, "error": "Drive already active for this session"}

    # Fresh drive — reset memory, iteration, log.
    # Project memory persists separately for cross-drive continuity.
    drive = {
        "target": target,
        "state": "driving",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_eval_at": None,
        "check_interval": check_interval,
        "max_iterations": max_iterations,
        "iteration": 0,
        "memory": [],
        "log": [],
    }
    drive["log"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "started",
        "reasoning": f"Drive started with target: {target[:100]}",
    })
    save_drive(short_id, drive)

    t = threading.Thread(
        target=drive_session_loop,
        args=(short_id, check_interval, model),
        daemon=True,
    )
    t.start()
    _active_drives[short_id] = t
    logger.info("Drive started for [%s]: %s", short_id, target[:80])
    return {"ok": True, "message": "Drive started"}


def stop_drive(short_id: str) -> dict:
    """Stop driving a session."""
    drive = load_drive(short_id)
    if not drive:
        return {"ok": False, "error": "No drive found for this session"}
    drive["state"] = "paused"
    drive["log"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "stopped",
        "reasoning": "Drive stopped by user",
    })
    save_drive(short_id, drive)
    logger.info("Drive stopped for [%s].", short_id)
    return {"ok": True}


def _restart_active_drives(model: str) -> None:
    """Restart drive threads for any drives with state=='driving'."""
    for drive in list_active_drives():
        sid = drive.get("_short_id", "")
        if not sid:
            continue
        if sid in _active_drives and _active_drives[sid].is_alive():
            continue
        interval = drive.get("check_interval", 30)
        t = threading.Thread(
            target=drive_session_loop,
            args=(sid, interval, model),
            daemon=True,
        )
        t.start()
        _active_drives[sid] = t
        logger.info("Auto-restarted drive for [%s].", sid)
