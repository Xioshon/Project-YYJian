from dataclasses import dataclass, field


DEFAULT_COMPILE_COMMAND = [
    "python",
    "-m",
    "py_compile",
    "core_tools.py",
    "core_agent.py",
    "main.py",
    "self_test.py",
    "agent_turns.py",
    "agent_session.py",
    "agent_context.py",
    "agent_memory.py",
    "agent_knowledge.py",
    "agent_eval.py",
    "agent_task_graph.py",
    "agent_worker.py",
    "agent_planner.py",
    "agent_replay.py",
    "agent_subagents.py",
    "agent_verification.py",
    "agent_action_verification.py",
    "agent_transactions.py",
]


@dataclass
class VerificationCommand:
    name: str
    command: list[str]
    reason: str
    required: bool = True


@dataclass
class VerificationPlan:
    objective: str = ""
    commands: list[VerificationCommand] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.commands and not self.notes:
            return "verification: no deterministic checks selected"
        lines = []
        for item in self.commands:
            flag = "required" if item.required else "optional"
            lines.append(f"{item.name} ({flag}): {' '.join(item.command)} -- {item.reason}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


class VerificationPlanner:
    def plan(self, objective: str = "", changed_files: list[str] | None = None, evidence: list[str] | None = None) -> VerificationPlan:
        changed_files = changed_files or []
        evidence = evidence or []
        plan = VerificationPlan(objective=objective)
        normalized = " ".join([objective, *changed_files, *evidence]).casefold()
        py_files = [path for path in changed_files if path.endswith(".py")]
        docs_only = bool(changed_files) and all(path.endswith((".md", ".txt")) for path in changed_files)

        if py_files or any(marker in normalized for marker in ["python", "runtime", "agent", "tool", "session", "permission", "telegram"]):
            plan.commands.append(VerificationCommand("py_compile", DEFAULT_COMPILE_COMMAND, "Python/runtime files changed or runtime behavior is affected."))
            plan.commands.append(VerificationCommand("self_test", ["python", "self_test.py"], "Run deterministic regression suite."))
        elif docs_only:
            plan.notes.append("Docs-only change: review rendered Markdown and keep self_test optional.")
            plan.commands.append(VerificationCommand("self_test_optional", ["python", "self_test.py"], "Optional safety check for documentation-adjacent runtime notes.", required=False))
        else:
            plan.commands.append(VerificationCommand("self_test", ["python", "self_test.py"], "Default regression check."))

        return plan


DEFAULT_VERIFICATION_PLANNER = VerificationPlanner()
