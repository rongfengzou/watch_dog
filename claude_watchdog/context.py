"""Context extraction from session entries."""

import json


def extract_context(entries: list[dict], max_messages: int = 30) -> str:
    """Format the last N meaningful messages for the summarizer."""
    meaningful = [
        e for e in entries if e.get("type") in ("user", "assistant")
    ]
    tail = meaningful[-max_messages:]
    lines = []
    for entry in tail:
        msg = entry.get("message", {})
        role = msg.get("role", entry.get("type", "unknown"))
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role.upper()}: {content[:500]}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")[:500]
                    lines.append(f"{role.upper()}: {text}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    inp_summary = json.dumps(inp, ensure_ascii=False)[:200]
                    lines.append(f"TOOL_CALL: {name}({inp_summary})")
                elif btype == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, str):
                        result = result[:300]
                    elif isinstance(result, list):
                        result = json.dumps(result, ensure_ascii=False)[:300]
                    lines.append(f"TOOL_RESULT: {result}")
    return "\n".join(lines)


def extract_session_metadata(entries: list[dict]) -> dict:
    """Extract useful metadata from session entries."""
    meta = {"slug": "", "cwd": "", "model": "", "version": ""}
    for entry in entries:
        if not meta["slug"] and entry.get("slug"):
            meta["slug"] = entry["slug"]
        if not meta["cwd"] and entry.get("cwd"):
            meta["cwd"] = entry["cwd"]
        if not meta["version"] and entry.get("version"):
            meta["version"] = entry["version"]
        msg = entry.get("message", {})
        if not meta["model"] and msg.get("model"):
            meta["model"] = msg["model"]
    return meta


def extract_last_messages(entries: list[dict], n: int = 20) -> list[dict]:
    """Extract last N meaningful messages for display, terminal-style."""
    meaningful = [e for e in entries if e.get("type") in ("user", "assistant")]
    tail = meaningful[-n:]
    messages = []
    for entry in tail:
        msg = entry.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            if content.strip():
                messages.append({"role": role, "text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    t = block.get("text", "")
                    if t.strip():
                        messages.append({"role": role, "type": "text", "text": t})
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    detail = (
                        inp.get("command")
                        or inp.get("file_path")
                        or inp.get("pattern")
                        or inp.get("query")
                        or inp.get("prompt")
                        or ""
                    )
                    if detail:
                        detail = detail[:200]
                    messages.append({"role": role, "type": "tool_use", "text": f"{name}: {detail}" if detail else name})
                elif bt == "tool_result":
                    r = block.get("content", "")
                    if isinstance(r, list):
                        parts = []
                        for sub in r:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                parts.append(sub.get("text", ""))
                        r = "\n".join(parts)
                    if isinstance(r, str) and r.strip():
                        messages.append({"role": role, "type": "tool_result", "text": r[:2000]})
    return messages
