"""Project memory CRUD, categories, limits, enrichment."""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_MEMORY_DIR, logger
from .prompts import SELF_SUMMARIZE_PROMPT

# Categories with different retention rules:
#   constraints  — permanent (never auto-pruned)
#   results      — keep last 10 (numeric outcomes, metrics)
#   decisions    — keep last 10 (superseded when updated)
#   working_config — keep last 10 (paths, commands, versions)
MEMORY_CATEGORIES = ("constraints", "results", "decisions", "working_config")
MEMORY_CAT_LIMITS = {
    "constraints": 30,
    "results": 15,
    "decisions": 10,
    "working_config": 15,
}

_last_summarize: dict[str, float] = {}   # project -> timestamp
SUMMARIZE_COOLDOWN = 300                   # 5 min between triggers per project


def trigger_self_summarize(short_id: str, project: str) -> bool:
    """Inject self-summarize prompt into Claude's terminal."""
    # Lazy import to avoid circular dependency: memory -> terminal -> snapshot -> memory
    from .terminal import (
        discover_claude_processes,
        match_session_to_process,
        send_keys_to_target,
    )

    now = time.time()
    last = _last_summarize.get(project, 0)
    if now - last < SUMMARIZE_COOLDOWN:
        logger.info(
            "Self-summarize: cooldown active for %s (%.0fs remaining)",
            project, SUMMARIZE_COOLDOWN - (now - last),
        )
        return False

    path = _project_memory_path(project)
    prompt = SELF_SUMMARIZE_PROMPT.format(path=path)

    procs = discover_claude_processes()
    target = match_session_to_process(short_id, procs)
    if not target:
        logger.warning("Self-summarize: no process found for session %s", short_id)
        return False

    ok, msg = send_keys_to_target(target, prompt)
    if ok:
        _last_summarize[project] = now
        logger.info("Self-summarize triggered for %s: %s", project, msg)
    else:
        logger.error("Self-summarize send failed for %s: %s", project, msg)
    return ok


def _project_memory_path(project: str) -> Path:
    """Return the JSON file path for a project's memory."""
    slug = project.strip("/").replace("/", "-")
    return PROJECT_MEMORY_DIR / f"{slug}.json"


def _empty_memory(project: str) -> dict:
    return {
        "project": project,
        "constraints": [],
        "results": [],
        "decisions": [],
        "working_config": [],
        "updated_at": None,
    }


def load_project_memory(project: str) -> dict:
    """Load structured project memory."""
    path = _project_memory_path(project)
    if not path.exists():
        return _empty_memory(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("project", project)
        # Migrate from old flat format
        if "facts" in data and not any(
            data.get(c) for c in MEMORY_CATEGORIES
        ):
            data["results"] = data.pop("facts", [])
        for cat in MEMORY_CATEGORIES:
            data.setdefault(cat, [])
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_memory(project)


def save_project_memory(project: str, mem: dict) -> None:
    """Save project memory to disk."""
    PROJECT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    mem["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Remove legacy 'facts' key if present
    mem.pop("facts", None)
    path = _project_memory_path(project)
    path.write_text(
        json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def add_project_memory_items(
    project: str, items: dict[str, list[str]],
) -> dict:
    """Add items to structured project memory.

    *items* maps category -> list of strings, e.g.
    {"results": ["ecoul MAE=0.85"], "constraints": ["do not change flag X"]}
    """
    mem = load_project_memory(project)
    for cat, entries in items.items():
        if cat not in MEMORY_CATEGORIES:
            continue
        for item in entries:
            item = item.strip()
            if item and item not in mem[cat]:
                mem[cat].append(item)
        # Enforce per-category cap (drop oldest)
        limit = MEMORY_CAT_LIMITS.get(cat, 10)
        if len(mem[cat]) > limit:
            dropped = len(mem[cat]) - limit
            mem[cat] = mem[cat][dropped:]
            logger.info(
                "Project memory [%s] %s: pruned %d oldest",
                project, cat, dropped,
            )
    save_project_memory(project, mem)
    return mem


def remove_project_memory_items(
    project: str, items: dict[str, list[str]],
) -> dict:
    """Remove items from structured project memory.

    Uses exact match first, then falls back to substring containment
    so that Ollama's slightly rephrased removal strings still work.
    """
    mem = load_project_memory(project)
    for cat, entries in items.items():
        if cat not in MEMORY_CATEGORIES:
            continue
        for item in entries:
            item = item.strip()
            if not item:
                continue
            if item in mem[cat]:
                mem[cat].remove(item)
            else:
                # Fuzzy: remove if existing item contains the removal string
                # or the removal string contains the existing item
                for existing in list(mem[cat]):
                    if item in existing or existing in item:
                        mem[cat].remove(existing)
                        break
    save_project_memory(project, mem)
    return mem


# Backward-compatible wrappers for flat fact API (used by drive loop)
def add_project_facts(project: str, facts: list[str]) -> dict:
    """Add facts as 'results' category (backward compat)."""
    return add_project_memory_items(project, {"results": facts})


def remove_project_facts(project: str, facts: list[str]) -> dict:
    """Remove facts from 'results' category (backward compat)."""
    return remove_project_memory_items(project, {"results": facts})


def get_all_project_facts(mem: dict) -> list[str]:
    """Flatten all categories into a single list for display."""
    all_facts = []
    for cat in MEMORY_CATEGORIES:
        for item in mem.get(cat, []):
            all_facts.append(f"[{cat}] {item}")
    return all_facts


def _relevance_score(item: str, keywords: set[str]) -> tuple[float, int]:
    """Score a memory item by keyword overlap with context.

    Returns (overlap_ratio, original_index_placeholder) for sorting.
    Higher overlap = more relevant to current context.
    """
    item_words = set(re.findall(r"[a-zA-Z0-9_]{3,}", item.lower()))
    if not item_words:
        return (0.0, 0)
    overlap = len(item_words & keywords)
    return (overlap / len(item_words), overlap)


def _select_relevant(
    items: list[str], limit: int, keywords: set[str],
) -> list[str]:
    """Select most relevant items using keyword scoring.

    Strategy: score each item against context keywords, take top-N by
    relevance. If no context, fall back to last-N (recency).
    Items that score 0 are still included if slots remain, preserving
    recency order for those.
    """
    if not items or limit <= 0:
        return []
    if not keywords:
        return items[-limit:]

    scored = []
    for i, item in enumerate(items):
        score = _relevance_score(item, keywords)
        scored.append((score[0], score[1], i, item))

    # Sort: primary by overlap ratio desc, secondary by recency (index) desc
    scored.sort(key=lambda x: (x[0], x[2]), reverse=True)
    selected = [s[3] for s in scored[:limit]]
    return selected


def get_enriched_context_prefix(
    drive_memory: list[str], project: str,
    context: str = "",
) -> str:
    """Build a context prefix combining project memory + drive memory.

    When *context* is provided (recent transcript text), memory items are
    ranked by keyword relevance so the most pertinent items are injected.
    """
    # Build keyword set from context for relevance scoring
    keywords: set[str] = set()
    if context:
        keywords = set(re.findall(r"[a-zA-Z0-9_]{3,}", context.lower()))

    parts: list[str] = []
    pmem = load_project_memory(project)
    # Constraints always included (they're rules)
    if pmem["constraints"]:
        selected = _select_relevant(pmem["constraints"], 8, keywords)
        parts.append("Constraints: " + "; ".join(selected))
    # Results (recent metrics/outcomes)
    if pmem["results"]:
        selected = _select_relevant(pmem["results"], 5, keywords)
        parts.append("Known results: " + "; ".join(selected))
    # Decisions
    if pmem["decisions"]:
        selected = _select_relevant(pmem["decisions"], 5, keywords)
        parts.append("Decisions: " + "; ".join(selected))
    # Working config
    if pmem["working_config"]:
        selected = _select_relevant(pmem["working_config"], 5, keywords)
        parts.append("Config: " + "; ".join(selected))
    # Drive memory (recent, session-specific — always use recency)
    if drive_memory:
        parts.append(
            "Drive state: " + "; ".join(drive_memory[-5:])
        )
    if not parts:
        return ""
    combined = " | ".join(parts)
    return f"[Last known state (verify before assuming): {combined}]\n"
