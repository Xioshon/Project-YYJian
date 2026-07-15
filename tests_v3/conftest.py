from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "memory").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("親近、自然、可靠。", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("只根據證據回報結果。", encoding="utf-8")
    (tmp_path / "workspace" / "memory" / "profile.json").write_text("{}", encoding="utf-8")
    return tmp_path
