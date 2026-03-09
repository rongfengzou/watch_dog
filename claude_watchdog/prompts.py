"""All prompt template strings for Ollama calls."""

SUMMARY_PROMPT_TEMPLATE = """\
You are analyzing a Claude Code session that has stalled.

Stall type: {stall_type} — {stall_description}
Session has been inactive for {age_minutes} minutes.

Here is the recent conversation context:
---
{context}
---

Based on the conversation above, provide a structured analysis:

## PROGRESS
- Bullet points summarizing what was accomplished so far

## CURRENT_TASK
- What was being worked on when the stall occurred

## STALL_REASON
- Why the session likely stopped (based on the stall type and context)

## RESUME_PROMPT
Provide a ready-to-paste prompt that the user can send to Claude Code to resume
where it left off. The prompt should:
1. Briefly state what was being done
2. Reference the last action taken
3. Ask to continue from that point
"""

DRIVE_EVAL_PROMPT_TEMPLATE = """\
You are a task watchdog. Evaluate progress toward the target and decide the next step.
IMPORTANT: You must ONLY focus on the TARGET. Ignore anything unrelated.

TARGET (fixed, never changes):
{target}

YOUR MEMORY (goal status from previous evaluations):
{memory}

RECENT CLAUDE CONVERSATION (last few exchanges):
{context}

MEMORY RULES — only add memory items in these formats:
- Goal status: "goal X: achieved/not achieved, metric=value"
- Current blocker: "blocked: reason"
- Approach: "using approach: description"
Do NOT add: implementation details, what Claude did, file names, debug steps.
Remove memory items that are superseded by newer status.

Respond in STRICT JSON (no markdown, no explanation outside JSON):
{{
  "status": "done" | "not_done" | "blocked",
  "progress_pct": <0-100>,
  "memory_add": ["goal X: achieved, accuracy=95%"],
  "memory_remove": ["outdated status"],
  "next_instruction": "what to tell Claude next (empty if done)",
  "reasoning": "1-2 sentence explanation"
}}
"""

FACT_EXTRACT_PROMPT_TEMPLATE = """\
You extract facts into a JSON object with EXACTLY these keys: add_results, add_decisions, add_constraints, add_working_config, remove_results, remove_decisions, remove_constraints, remove_working_config. All values are arrays of strings. No other keys or format.

Categories:
- constraints: rules, invariants, FAILED APPROACHES (e.g. "do not use X because Y"), dead ends
- results: numeric outcomes, metrics, benchmarks
- decisions: why approach A chosen over B
- working_config: paths, commands, versions confirmed working

IMPORTANT: Always extract failed attempts as constraints (e.g. "do not try X: fails because Y"). This prevents repeating the same mistakes.

Rules: keep items under 80 chars. Max 5 items per category. If new fact supersedes existing, put exact old string in remove_* key.

EXISTING:
{existing}

MESSAGE:
{message}

Output ONLY a JSON object like this example, nothing else:
{{"add_results":["test accuracy improved to 95%"],"add_decisions":[],"add_constraints":["do not use method X: fails on edge case Y"],"add_working_config":[],"remove_results":["accuracy was 88%"],"remove_decisions":[],"remove_constraints":[],"remove_working_config":[]}}
"""

SIGNIFICANCE_PROMPT = """\
Did this message contain any: new metrics/results, failed approaches, key decisions, or important configs? Answer YES or NO only."""

SELF_SUMMARIZE_PROMPT = """\
Read {path} and update it with findings from our conversation. \
JSON categories: constraints (rules + FAILED approaches), results (metrics), decisions (why A over B), working_config (paths/commands). \
Rules: read existing, merge new findings, remove outdated, keep items <80 chars, limits: constraints 20, others 10. CRITICAL: save failed approaches as constraints. \
Write updated JSON back to {path}."""
