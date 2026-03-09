"""Drive state storage."""

import json
from typing import Optional

from .config import DRIVES_DIR


def load_drive(short_id: str) -> Optional[dict]:
    """Load drive state for a session."""
    path = DRIVES_DIR / f"{short_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_drive(short_id: str, drive_state: dict) -> None:
    """Save drive state for a session."""
    DRIVES_DIR.mkdir(parents=True, exist_ok=True)
    path = DRIVES_DIR / f"{short_id}.json"
    path.write_text(
        json.dumps(drive_state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def list_active_drives() -> list[dict]:
    """List all drives with state=='driving'."""
    DRIVES_DIR.mkdir(parents=True, exist_ok=True)
    active = []
    for path in DRIVES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("state") == "driving":
                data["_short_id"] = path.stem
                active.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return active
