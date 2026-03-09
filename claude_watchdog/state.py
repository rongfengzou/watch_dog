"""Alert state persistence."""

import json

from .config import STATE_FILE, WATCHDOG_DIR


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def already_alerted(state: dict, stall: dict) -> bool:
    entries = stall["entries"]
    if not entries:
        return False
    last_uuid = entries[-1].get("uuid", "")
    return state.get(stall["path"]) == last_uuid


def mark_alerted(state: dict, stall: dict) -> None:
    entries = stall["entries"]
    if not entries:
        return
    state[stall["path"]] = entries[-1].get("uuid", "")
