"""Constants, paths, logger setup."""

import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watchdog")

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
WATCHDOG_DIR = Path.home() / ".claude" / "watchdog"
STATE_FILE = WATCHDOG_DIR / "state.json"
TAIL_BYTES = 50 * 1024  # 50KB tail read
DRIVES_DIR = WATCHDOG_DIR / "drives"
PROJECT_MEMORY_DIR = WATCHDOG_DIR / "project_memory"

# Shared mutable config for web/drive to access threshold/model at runtime
_web_config: dict = {}


def decode_project_path(dirname: str) -> str:
    """Decode '-Users-foo-bar' back to '/Users/foo/bar'."""
    return "/" + dirname.lstrip("-").replace("-", "/")
