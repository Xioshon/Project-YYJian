from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.system_audit import build_report


def test_full_system_audit_passes_for_current_source_assets_and_dependencies() -> None:
    report = build_report()
    assert report["status"] == "pass", report["failures"]
    assert report["counts"]["tools"] == 30
    assert report["counts"]["assets"] >= 100


def test_system_audit_cli_runs_from_project_root() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/system_audit.py", "--no-write"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Status: pass" in result.stdout
