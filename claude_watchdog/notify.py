"""macOS notifications and resume file generation."""

import subprocess
from datetime import datetime
from pathlib import Path

from .config import WATCHDOG_DIR


def notify_macos(title: str, message: str) -> None:
    """Send a macOS notification via osascript."""
    script = (
        f'display notification "{message}" '
        f'with title "{title}" sound name "Glass"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        pass


def write_resume_file(stall: dict, summary: str) -> Path:
    """Write the resume markdown to ~/.claude/watchdog/."""
    WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"resume-{stall['session_id'][:8]}-{ts}.md"
    out_path = WATCHDOG_DIR / filename
    content = (
        f"# Watchdog Alert: Session Stalled\n\n"
        f"- **Session**: `{stall['session_id']}`\n"
        f"- **Stall type**: `{stall['stall_type']}` — {stall['stall_description']}\n"
        f"- **Inactive**: {stall['age_minutes']} minutes\n"
        f"- **Detected**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- **JSONL**: `{stall['path']}`\n\n"
        f"---\n\n{summary}\n"
    )
    out_path.write_text(content, encoding="utf-8")
    return out_path
