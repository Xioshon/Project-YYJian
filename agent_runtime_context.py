from __future__ import annotations

from typing import Any


CHAT_INTENTS = {"chat", "idle", "social_sticker"}
TASK_INTENTS = {
    "task",
    "tool_task",
    "active_task",
    "task_continuation",
    "task_followup",
    "permission_reply",
    "permission_granted",
    "screen_observe",
    "vision_task",
    "vision",
}


def worker_context(items: list[dict[str, Any]] | None) -> str:
    if not items:
        return ""
    lines = []
    for item in items[-5:]:
        lines.append(f"{item.get('step_id', 'step')} worker={item.get('status', 'unknown')} job={item.get('job_id', '')}")
    return "\n".join(lines)


def should_include_task_context(
    turn_intent: str,
    *,
    pending_permission: bool = False,
    active_task: bool = False,
    grant: str = "none",
    worker_results: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> bool:
    if force:
        return True
    if pending_permission or grant in {"single", "turn", "deny"}:
        return True
    if active_task:
        return True
    if worker_results:
        return True
    normalized = (turn_intent or "").casefold()
    if normalized in TASK_INTENTS:
        return True
    if normalized in CHAT_INTENTS:
        return False
    return normalized not in {"", "chat"}


def build_runtime_context(
    user_input: str,
    *,
    turn_intent: str,
    session_summary: str = "",
    task_summary: str = "",
    worker_results: list[dict[str, Any]] | None = None,
    include_task_context: bool = False,
) -> str:
    enriched = user_input
    if include_task_context:
        enriched += f"\n\n[SessionBrain]\n{session_summary}\n\n[TaskGraph]\n{task_summary}\nturn_intent: {turn_intent}"
    worker_summary = worker_context(worker_results)
    if worker_summary:
        enriched += f"\n\n[WorkerEvidence]\n{worker_summary}"
    return enriched
