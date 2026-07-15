from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from yueyue_v3.observations import ObservationService


class _StaticImage:
    size = (64, 32)

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(b"same-screen-pixels")


def test_capture_screen_uses_absolute_artifact_paths_and_content_revision(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(screenshot=lambda: _StaticImage()))
    service = ObservationService(Path("relative-state"))

    first = service.capture_screen()
    second = service.capture_screen()

    assert Path(first.artifacts[0]).is_absolute()
    assert first.facts["revision"] == second.facts["revision"]
    assert first.facts["screenshot_id"] != second.facts["screenshot_id"]


def test_click_screen_rejects_coordinates_outside_foreground_window(tmp_path, monkeypatch) -> None:
    clicks = []
    monkeypatch.setitem(
        sys.modules,
        "pyautogui",
        SimpleNamespace(
            size=lambda: SimpleNamespace(width=2560, height=1440),
            moveTo=lambda x, y, duration=0: clicks.append((x, y)),
            click=lambda: clicks.append("click"),
            doubleClick=lambda: clicks.append("double"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(GetForegroundWindow=lambda: 42, GetWindowRect=lambda _hwnd: (200, 40, 1224, 718)),
    )
    service = ObservationService(tmp_path / "state")
    service.screen_store.save(
        {
            "schema_version": 3,
            "screenshot_id": "screen-1",
            "created_at": __import__("time").time(),
            "width": 2560,
            "height": 1440,
        }
    )

    result = service.click_screen("screen-1", 2, 955)

    assert result.status == "error"
    assert result.error_category == "outside_active_window"
    assert clicks == []
