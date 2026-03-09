"""CLI entry point, argparse, main loop."""

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .config import WATCHDOG_DIR, _web_config, decode_project_path, logger
from .drive import _active_drives, _restart_active_drives, start_drive, stop_drive
from .notify import notify_macos
from .ollama import summarize_stall
from .scanner import STALL_TYPES, detect_stall, scan_sessions
from .state import already_alerted, load_state, mark_alerted, save_state
from .notify import write_resume_file


def process_sessions(threshold_minutes: float, model: str) -> int:
    """Scan sessions and process any stalls. Returns number of alerts sent."""
    state = load_state()
    sessions = scan_sessions()
    alerts_sent = 0
    if not sessions:
        logger.info("No active sessions found.")
        return 0
    logger.info("Found %d session(s) within 24h.", len(sessions))
    for path in sessions:
        session_id = path.stem[:8]
        stall = detect_stall(path, threshold_minutes)
        if stall is None:
            age = (time.time() - path.stat().st_mtime) / 60.0
            logger.info(
                "  [%s] No stall (last write %.1f min ago)", session_id, age
            )
            continue
        if already_alerted(state, stall):
            logger.info(
                "  [%s] Stall (%s) — already alerted, skipping.",
                session_id, stall["stall_type"],
            )
            continue
        logger.info(
            "  [%s] STALL DETECTED: %s (%.1f min)",
            session_id, stall["stall_type"], stall["age_minutes"],
        )
        logger.info("  [%s] Calling Ollama (%s) for summary...", session_id, model)
        summary = summarize_stall(stall, model)
        if summary is None:
            summary = (
                f"## PROGRESS\n- (Ollama unavailable — manual review needed)\n\n"
                f"## CURRENT_TASK\n- Review JSONL: `{stall['path']}`\n\n"
                f"## STALL_REASON\n- {stall['stall_description']}\n\n"
                f"## RESUME_PROMPT\n"
                f"Continue from where you left off. The session stalled due to: "
                f"{stall['stall_description']}.\n"
            )
            logger.warning("  [%s] Ollama failed, using fallback summary.", session_id)
        out_path = write_resume_file(stall, summary)
        logger.info("  [%s] Resume file: %s", session_id, out_path)
        notify_macos(
            "Claude Watchdog",
            f"Session {session_id} stalled: {stall['stall_type']} "
            f"({stall['age_minutes']} min)",
        )
        mark_alerted(state, stall)
        alerts_sent += 1
    save_state(state)
    return alerts_sent


def watchdog_loop(threshold: float, interval: float, model: str):
    """Background poll loop for stall detection."""
    cycle = 0
    while True:
        try:
            process_sessions(threshold, model)
        except Exception:
            logger.exception("Error in poll cycle")
        cycle += 1
        if cycle % 10 == 0:  # every ~5 min at 30s interval
            try:
                _restart_active_drives(model)
            except Exception:
                logger.exception("Error restarting drives")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code Watchdog — detect stalled sessions"
    )
    parser.add_argument(
        "--threshold", type=float, default=5.0,
        help="Minutes of inactivity before flagging a stall (default: 5)",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0,
        help="Seconds between poll cycles (default: 30)",
    )
    parser.add_argument(
        "--model", default="qwen3:14b",
        help="Ollama model for summarization (default: qwen3:14b)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit (don't loop)",
    )
    parser.add_argument(
        "--foreground", action="store_true",
        help="Run in foreground with verbose output",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="Start web dashboard",
    )
    parser.add_argument(
        "--port", type=int, default=7888,
        help="Web dashboard port (default: 7888)",
    )
    parser.add_argument(
        "--drive", action="store_true",
        help="Drive mode: autonomously drive a Claude session toward a target",
    )
    parser.add_argument(
        "--target", type=str, default="",
        help="Target description for drive mode",
    )
    parser.add_argument(
        "--target-file", type=str, default="",
        help="Path to file containing target description for drive mode",
    )
    parser.add_argument(
        "--session", type=str, default="",
        help="Session identifier (short_id, index like '1', or project name match)",
    )
    parser.add_argument(
        "--check-interval", type=float, default=30.0,
        help="Seconds between drive evaluations (default: 30)",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Max drive iterations before pausing (default: 50)",
    )
    args = parser.parse_args()

    if args.foreground:
        logging.getLogger().setLevel(logging.DEBUG)

    def handle_signal(sig, _frame):
        logger.info("Received signal %s, exiting.", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)

    # --- Drive mode (headless, blocking) ---
    if args.drive:
        # Resolve target
        target_text = args.target
        if args.target_file:
            tf = Path(args.target_file)
            if not tf.exists():
                logger.error("Target file not found: %s", args.target_file)
                sys.exit(1)
            target_text = tf.read_text(encoding="utf-8").strip()
        if not target_text:
            logger.error("No target specified. Use --target or --target-file.")
            sys.exit(1)

        # Resolve session
        sessions_list = scan_sessions()
        if not sessions_list:
            logger.error("No active sessions found.")
            sys.exit(1)

        resolved_sid = None
        sess_arg = args.session
        if not sess_arg:
            # Default to most recent session
            resolved_sid = sessions_list[0].stem[:8]
            logger.info("No --session specified, using most recent: %s", resolved_sid)
        elif sess_arg.isdigit():
            idx = int(sess_arg) - 1
            if 0 <= idx < len(sessions_list):
                resolved_sid = sessions_list[idx].stem[:8]
            else:
                logger.error("Session index %s out of range (1-%d).", sess_arg, len(sessions_list))
                sys.exit(1)
        else:
            # Try as short_id or project name match
            for p in sessions_list:
                if p.stem.startswith(sess_arg):
                    resolved_sid = p.stem[:8]
                    break
            if not resolved_sid:
                # Try project name match
                for p in sessions_list:
                    project = decode_project_path(p.parent.name)
                    if sess_arg.lower() in project.lower():
                        resolved_sid = p.stem[:8]
                        break
            if not resolved_sid:
                logger.error("Could not resolve session: %s", sess_arg)
                sys.exit(1)

        logger.info(
            "Drive mode: session=%s target=%s interval=%ds max=%d",
            resolved_sid, target_text[:80], args.check_interval, args.max_iterations,
        )
        _web_config["threshold"] = args.threshold
        _web_config["model"] = args.model
        result = start_drive(
            resolved_sid, target_text, args.model,
            args.check_interval, args.max_iterations,
        )
        if not result.get("ok"):
            logger.error("Failed to start drive: %s", result.get("error"))
            sys.exit(1)

        # Block until drive thread exits
        t = _active_drives.get(resolved_sid)
        if t:
            try:
                t.join()
            except (KeyboardInterrupt, SystemExit):
                logger.info("Stopping drive...")
                stop_drive(resolved_sid)
                t.join(timeout=5)
        return

    # --- Single run mode ---
    if args.once and not args.web:
        logger.info(
            "Watchdog started (threshold=%.1f min, model=%s)",
            args.threshold, args.model,
        )
        alerts = process_sessions(args.threshold, args.model)
        logger.info("Single run complete. Alerts sent: %d", alerts)
        return

    # --- Web dashboard mode ---
    if args.web:
        from http.server import ThreadingHTTPServer
        from .web.server import DashboardHandler

        _web_config["threshold"] = args.threshold
        _web_config["model"] = args.model

        # Start background watchdog poll loop
        t = threading.Thread(
            target=watchdog_loop,
            args=(args.threshold, args.interval, args.model),
            daemon=True,
        )
        t.start()
        logger.info(
            "Watchdog poll loop started in background (threshold=%.1f min, interval=%.0f s)",
            args.threshold, args.interval,
        )
        # Auto-restart any drives that were active before restart
        _restart_active_drives(args.model)

        server = ThreadingHTTPServer(("0.0.0.0", args.port), DashboardHandler)
        logger.info("Dashboard: http://localhost:%d", args.port)
        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            server.shutdown()
        return

    # --- Daemon poll mode (no web) ---
    logger.info(
        "Watchdog started (threshold=%.1f min, interval=%.0f s, model=%s)",
        args.threshold, args.interval, args.model,
    )
    watchdog_loop(args.threshold, args.interval, args.model)
