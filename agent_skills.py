import os
import re
from dataclasses import dataclass

from core_tools import WORKSPACE_DIR


SKILLS_DIR = os.path.join(WORKSPACE_DIR, "skills")


BUILTIN_SKILLS = {
    "debug": {
        "description": "Diagnose failures with traces, tests, and focused reproduction before proposing fixes.",
        "triggers": ["bug", "error", "fail", "debug", "修", "壞", "錯"],
        "allowed_tools": ["read_file", "search_in_files", "execute_python", "execute_command"],
        "body": "Use trace evidence first. Reproduce narrowly. Prefer tests before edits. Summarize root cause and verification.",
    },
    "vision": {
        "description": "Analyze images and screenshots before responding.",
        "triggers": ["image", "photo", "screenshot", "圖片", "照片", "截圖"],
        "allowed_tools": ["analyze_media", "read_file"],
        "body": "Use analyze_media for images. State uncertainty when the image is unclear. Avoid inventing visual details.",
    },
    "telegram": {
        "description": "Handle Telegram messages, stickers, reactions, and reply rendering.",
        "triggers": ["telegram", "sticker", "reaction", "表情包", "貼圖"],
        "allowed_tools": ["search_sticker", "send_telegram_media", "react_to_message"],
        "body": "Model-chosen stickers should use reply markers, not send_telegram_media. Use reactions only when requested or natural.",
    },
    "safe-computer-use": {
        "description": "Operate the local UI conservatively with explicit checkpoints.",
        "triggers": ["click", "screen", "computer", "電腦", "點擊", "螢幕"],
        "allowed_tools": ["get_screen_ui", "click_ui_element", "type_keyboard", "press_hotkey"],
        "body": "Inspect UI before clicking. Prefer hotkeys for deterministic actions. Stop on unexpected state.",
    },
    "code-review-lite": {
        "description": "Review changes for bugs, risks, and missing tests.",
        "triggers": ["review", "code review", "審查", "檢查"],
        "allowed_tools": ["read_file", "search_in_files", "execute_command"],
        "body": "Lead with findings. Cite files and evidence. Mention test gaps and residual risk.",
    },
}


@dataclass
class SkillSpec:
    name: str
    description: str
    triggers: list[str]
    allowed_tools: list[str]
    instruction_body: str
    path: str = ""


class SkillRegistry:
    def __init__(self, skills_dir: str = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.skills: dict[str, SkillSpec] = {}

    def ensure_builtin_files(self) -> None:
        os.makedirs(self.skills_dir, exist_ok=True)
        for name, spec in BUILTIN_SKILLS.items():
            folder = os.path.join(self.skills_dir, name)
            path = os.path.join(folder, "SKILL.md")
            os.makedirs(folder, exist_ok=True)
            if os.path.exists(path):
                continue
            content = [
                "---",
                f"name: {name}",
                f"description: {spec['description']}",
                f"triggers: {', '.join(spec['triggers'])}",
                f"allowed_tools: {', '.join(spec['allowed_tools'])}",
                "---",
                "",
                spec["body"],
                "",
            ]
            with open(path, "w", encoding="utf-8") as file:
                file.write("\n".join(content))

    def load(self) -> dict[str, SkillSpec]:
        self.ensure_builtin_files()
        loaded: dict[str, SkillSpec] = {}
        for root, _, files in os.walk(self.skills_dir):
            if "SKILL.md" not in files:
                continue
            path = os.path.join(root, "SKILL.md")
            spec = self._parse_skill(path)
            loaded[spec.name] = spec
        self.skills = loaded
        return loaded

    def select(self, text: str, max_skills: int = 2) -> list[SkillSpec]:
        if not self.skills:
            self.load()
        normalized = (text or "").casefold()
        matches = []
        for skill in self.skills.values():
            score = sum(1 for trigger in skill.triggers if trigger.casefold() in normalized)
            explicit = f"/{skill.name}" in normalized or f"@{skill.name}" in normalized
            if explicit:
                score += 10
            if score:
                matches.append((score, skill.name, skill))
        matches.sort(reverse=True)
        return [skill for _, _, skill in matches[:max_skills]]

    def _parse_skill(self, path: str) -> SkillSpec:
        with open(path, "r", encoding="utf-8") as file:
            text = file.read()
        meta = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        meta[key.strip()] = value.strip()
                body = parts[2].strip()
        name = meta.get("name") or os.path.basename(os.path.dirname(path))
        triggers = [item.strip() for item in re.split(r"[,;]", meta.get("triggers", "")) if item.strip()]
        allowed_tools = [item.strip() for item in re.split(r"[,;]", meta.get("allowed_tools", "")) if item.strip()]
        return SkillSpec(
            name=name,
            description=meta.get("description", ""),
            triggers=triggers,
            allowed_tools=allowed_tools,
            instruction_body=body,
            path=path,
        )


DEFAULT_SKILL_REGISTRY = SkillRegistry()
