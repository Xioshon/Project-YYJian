import json
import os
import re
import time
from dataclasses import dataclass, field

from agent_hooks import emit_trace
from agent_memory import compile_memory
from agent_skills import SkillSpec
from core_tools import PROJECT_CACHE_DIR, TASK_PLAN_FILE


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
ARCHITECTURE_FILE = os.path.join(ROOT_DIR, "ARCHITECTURE.md")
RUNBOOK_FILE = os.path.join(ROOT_DIR, "RUNBOOK.md")
CONTEXT_BUDGET_REPORT_FILE = os.path.join(PROJECT_CACHE_DIR, "context_budget_report.json")


def _read_text(path: str, limit: int = 4000) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as file:
            text = file.read().strip()
        return text[-limit:]
    except Exception:
        return ""


@dataclass
class ContextPack:
    sections: list[tuple[str, str]] = field(default_factory=list)
    budget_report: dict[str, int] = field(default_factory=dict)

    def add(self, title: str, body: str) -> None:
        if body:
            self.sections.append((title, body))

    def render(self, max_chars: int = 12000) -> str:
        chunks = []
        total_before = 0
        total_after = 0
        for title, body in self.sections:
            if not body:
                continue
            body = str(body)
            total_before += len(body)
            limited = _limit_section(title, body)
            total_after += len(limited)
            self.budget_report[title] = len(limited)
            chunks.append(f"### {title}\n{limited}")
        rendered = "\n\n".join(chunks)
        if len(rendered) > max_chars:
            rendered = rendered[-max_chars:]
        self.budget_report["total_before"] = total_before
        self.budget_report["total_after"] = len(rendered)
        self.budget_report["max_chars"] = max_chars
        return rendered


class ContextPackBuilder:
    def build(self, selected_skills: list[SkillSpec] | None = None, base_prompt: str = "", mode: str = "chat", user_input: str = "") -> str:
        pack = ContextPack()
        pack.add("Base Prompt", base_prompt)
        compiled_memory = compile_memory(mode=mode, user_input=user_input)
        pack.add("Compiled Memory", compiled_memory.render())
        if mode in {"task", "tool_task", "vision_task"}:
            pack.add("Task Plan", _read_text(TASK_PLAN_FILE, 2000))
        for skill in selected_skills or []:
            pack.add(f"Skill: {skill.name}", f"{skill.description}\nAllowed tools: {', '.join(skill.allowed_tools)}\n{skill.instruction_body}")
        rendered = pack.render(max_chars=_max_context_chars(mode))
        _write_budget_report(mode, user_input, pack.budget_report)
        return rendered


DEFAULT_CONTEXT_BUILDER = ContextPackBuilder()


def _max_context_chars(mode: str) -> int:
    if mode in {"chat", "social_sticker"}:
        return 9000
    if mode == "vision_task":
        return 11000
    return 14000


def _limit_section(title: str, body: str) -> str:
    limits = {
        "Base Prompt": 6000,
        "Compiled Memory": 5500,
        "Task Plan": 1800,
    }
    limit = limits.get(title, 2500 if title.startswith("Skill:") else 3000)
    if len(body) <= limit:
        return body
    if title == "Compiled Memory":
        return _limit_compiled_memory(body, limit)
    return body[-limit:]


def _limit_compiled_memory(body: str, limit: int) -> str:
    section_limits = {
        "Personality Core": 2200,
        "Owner Profile": 700,
        "Owner Preferences": 700,
        "Rolling Chat Summary": 700,
        "Session Brain": 700,
        "Engineering Context": 900,
        "Style Samples": 900,
        "Memory Health Warnings": 400,
    }
    matches = list(re.finditer(r"^### (.+)$", body, flags=re.MULTILINE))
    if not matches:
        head = body[: int(limit * 0.55)]
        tail = body[-int(limit * 0.45) :]
        return head.rstrip() + "\n\n...[middle truncated by context budget]...\n\n" + tail.lstrip()

    chunks: list[str] = []
    for index, match in enumerate(matches):
        section_title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        section_limit = section_limits.get(section_title, 600)
        if len(content) > section_limit:
            content = content[:section_limit].rstrip() + "\n...[section clipped]..."
        if content:
            chunks.append(f"### {section_title}\n{content}")

    rendered = "\n\n".join(chunks)
    if len(rendered) <= limit:
        return rendered

    keep: list[str] = []
    used = 0
    for chunk in chunks:
        extra = len(chunk) + (2 if keep else 0)
        if used + extra > limit:
            continue
        keep.append(chunk)
        used += extra
    if not keep:
        return rendered[:limit].rstrip()
    return "\n\n".join(keep)


def _write_budget_report(mode: str, user_input: str, report: dict[str, int]) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "user_input_preview": (user_input or "")[:160],
        "sections": dict(report),
    }
    try:
        os.makedirs(os.path.dirname(CONTEXT_BUDGET_REPORT_FILE), exist_ok=True)
        with open(CONTEXT_BUDGET_REPORT_FILE, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        emit_trace("context.budget", mode=mode, total_after=report.get("total_after", 0), max_chars=report.get("max_chars", 0))
    except Exception as exc:
        emit_trace("context.budget_failed", mode=mode, error=str(exc))
