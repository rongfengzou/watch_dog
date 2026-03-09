"""Ollama LLM integration: summarization, drive evaluation, fact extraction."""

import json
import urllib.error
import urllib.request
from typing import Optional

from .config import logger
from .context import extract_context
from .prompts import (
    DRIVE_EVAL_PROMPT_TEMPLATE,
    FACT_EXTRACT_PROMPT_TEMPLATE,
    SIGNIFICANCE_PROMPT,
    SUMMARY_PROMPT_TEMPLATE,
)


def call_ollama(prompt: str, model: str) -> Optional[str]:
    """Call Ollama generate API. Returns the response text or None on error."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_predict": 1024},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.error("Ollama call failed: %s", e)
        return None


def summarize_stall(stall: dict, model: str) -> Optional[str]:
    """Generate a summary and resume prompt via Ollama."""
    context = extract_context(stall["entries"])
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        stall_type=stall["stall_type"],
        stall_description=stall["stall_description"],
        age_minutes=stall["age_minutes"],
        context=context,
    )
    return call_ollama(prompt, model)


def drive_evaluate(
    target: str, memory: list[str], context: str, model: str,
) -> Optional[dict]:
    """Evaluate drive progress via Ollama. Returns parsed JSON dict or None."""
    memory_text = "\n".join(f"- {m}" for m in memory) if memory else "(none yet)"
    prompt = DRIVE_EVAL_PROMPT_TEMPLATE.format(
        target=target,
        memory=memory_text,
        context=context,
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            raw = body.get("response", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.error("Drive eval Ollama call failed: %s", e)
        return None

    # Parse JSON from response, handling markdown-wrapped JSON
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        logger.error("Drive eval: failed to parse JSON from Ollama response")
        return None


def check_significance(context: str, model: str) -> bool:
    """Quick YES/NO via Ollama. Returns True if significant."""
    prompt = SIGNIFICANCE_PROMPT + "\n\nMESSAGE:\n" + context[:2000]
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "think": False, "options": {"num_predict": 10},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            raw = body.get("response", "").strip().upper()
            return "YES" in raw
    except Exception as e:
        logger.warning("Significance check failed: %s", e)
        return False


def extract_facts_via_ollama(
    message: str, model: str, existing_memory: dict | None = None,
) -> dict:
    """Extract facts and superseded removals from Claude's message via Ollama.

    Returns {"add": {"results": [...]}, "remove": {"results": [...]}}
    or empty dict on failure.
    """
    from .memory import MEMORY_CATEGORIES

    if not message or len(message.strip()) < 50:
        return {}
    truncated = message[:4000]

    # Format existing memory for the prompt
    if existing_memory:
        existing_lines = []
        for cat in MEMORY_CATEGORIES:
            items = existing_memory.get(cat, [])
            if items:
                existing_lines.append(f"  {cat}: {json.dumps(items)}")
        existing_str = "\n".join(existing_lines) if existing_lines else "(empty)"
    else:
        existing_str = "(empty)"

    prompt = FACT_EXTRACT_PROMPT_TEMPLATE.format(
        message=truncated, existing=existing_str,
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    result = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                raw = body.get("response", "").strip()
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.error("Fact extraction Ollama call failed: %s", e)
            return {}

        # Parse JSON object
        text = raw
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    result = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    result = None
            else:
                result = None
        if isinstance(result, dict):
            break
        logger.warning(
            "Fact extraction: attempt %d failed to parse JSON: %s",
            attempt + 1, repr(raw[:200]) if raw else "(empty)",
        )
        # Rebuild request for retry
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    if not isinstance(result, dict):
        logger.error("Fact extraction: all attempts failed")
        return {}

    # Parse flat format: add_results, remove_results, etc.
    out = {"add": {}, "remove": {}}
    for cat in MEMORY_CATEGORIES:
        for action in ("add", "remove"):
            key = f"{action}_{cat}"
            items = result.get(key, [])
            if isinstance(items, list):
                clean = [str(f).strip() for f in items if f]
                if clean:
                    out[action][cat] = clean
    if out["add"] or out["remove"]:
        return out

    # Fallback: nested {"add": {...}, "remove": {...}} format
    if "add" in result:
        for section in ("add", "remove"):
            blob = result.get(section, {})
            if not isinstance(blob, dict):
                continue
            for cat in MEMORY_CATEGORIES:
                items = blob.get(cat, [])
                if isinstance(items, list):
                    clean = [str(f).strip() for f in items if f]
                    if clean:
                        out[section][cat] = clean
        return out if out["add"] or out["remove"] else {}

    # Fallback: plain categories (add-only, no remove)
    add = {}
    for cat in MEMORY_CATEGORIES:
        items = result.get(cat, [])
        if isinstance(items, list):
            clean = [str(f).strip() for f in items if f]
            if clean:
                add[cat] = clean
    return {"add": add} if add else {}
