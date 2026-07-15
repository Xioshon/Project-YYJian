from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, UiElement, UiSnapshot, V3ToolResult
from .storage import AtomicJsonStore

VALID_CONTROL_TYPES = {
    "Button",
    "Edit",
    "MenuItem",
    "TabItem",
    "ListItem",
    "Document",
    "Text",
    "TreeItem",
    "CheckBox",
    "RadioButton",
}


def snapshot_from_dict(raw: dict[str, Any]) -> UiSnapshot:
    return UiSnapshot(
        snapshot_id=str(raw.get("snapshot_id") or ""),
        window_id=str(raw.get("window_id") or ""),
        window_title=str(raw.get("window_title") or ""),
        revision=str(raw.get("revision") or ""),
        elements=[UiElement(**item) for item in raw.get("elements") or []],
        created_at=float(raw.get("created_at") or 0.0),
    )


class ObservationService:
    def __init__(
        self,
        state_dir: str | Path,
        snapshot_ttl_seconds: float = 45.0,
        max_ui_elements: int = 1200,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self.max_ui_elements = max(100, min(int(max_ui_elements), 5000))
        self.snapshot_store = AtomicJsonStore(
            self.state_dir / "latest_ui_snapshot.json",
            lambda: UiSnapshot("", "", "", "", [], 0.0),
            snapshot_from_dict,
        )
        self.screen_store = AtomicJsonStore(self.state_dir / "latest_screen.json", dict, lambda raw: dict(raw or {}))

    def inspect_ui(self) -> V3ToolResult:
        try:
            import pywinauto
            import win32gui
        except ImportError as exc:
            return V3ToolResult(
                "error",
                "UI inspection dependencies are unavailable.",
                error=str(exc),
                error_category="dependency_missing",
            )
        try:
            hwnd = int(win32gui.GetForegroundWindow())
            if not hwnd:
                return V3ToolResult(
                    "error", "No foreground window is available.", error_category="window_missing", retryable=True
                )
            title = str(win32gui.GetWindowText(hwnd) or "")
            window = pywinauto.Desktop(backend="uia").window(handle=hwnd)
            elements: list[UiElement] = []
            for wrapper in window.descendants():
                try:
                    info = wrapper.element_info
                    control_type = str(info.control_type or "")
                    name = " ".join(str(wrapper.window_text() or info.name or "").split())
                    if control_type not in VALID_CONTROL_TYPES or not name or not wrapper.is_visible():
                        continue
                    rect = wrapper.rectangle()
                    elements.append(
                        UiElement(
                            element_id=str(len(elements) + 1),
                            name=name[:500],
                            control_type=control_type,
                            bounds={
                                "left": int(rect.left),
                                "top": int(rect.top),
                                "right": int(rect.right),
                                "bottom": int(rect.bottom),
                            },
                            enabled=bool(wrapper.is_enabled()),
                            visible=True,
                        )
                    )
                    if len(elements) >= self.max_ui_elements:
                        break
                except Exception:
                    continue
            revision_payload = json.dumps(
                [(item.name, item.control_type, item.bounds) for item in elements],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot = UiSnapshot(
                snapshot_id=f"ui_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
                window_id=str(hwnd),
                window_title=title,
                revision=hashlib.sha256(revision_payload.encode("utf-8")).hexdigest()[:20],
                elements=elements,
            )
            self.snapshot_store.save({"schema_version": SCHEMA_VERSION, **asdict(snapshot)})
            lines = [f"Active window: {title}", "Clickable/input elements:"]
            lines.extend(f"[{item.element_id}] {item.control_type}: {item.name}" for item in elements)
            facts = {
                "ui_snapshot": asdict(snapshot),
                "snapshot_id": snapshot.snapshot_id,
                "window_id": snapshot.window_id,
                "window_title": snapshot.window_title,
                "revision": snapshot.revision,
                "ui_elements": [asdict(item) for item in elements],
                "visible_text": [item.name for item in elements],
            }
            return V3ToolResult("ok", "\n".join(lines), data={"count": len(elements), **facts}, facts=facts)
        except Exception as exc:
            return V3ToolResult(
                "error",
                "Failed to inspect the active window.",
                error=str(exc),
                error_category="ui_inspection_failed",
                retryable=True,
            )

    def click_element(self, element_id: str, snapshot_id: str = "", double_click: bool = False) -> V3ToolResult:
        try:
            import pyautogui
            import win32gui
        except ImportError as exc:
            return V3ToolResult(
                "error", "UI click dependencies are unavailable.", error=str(exc), error_category="dependency_missing"
            )
        snapshot = self.snapshot_store.load()
        if not snapshot.snapshot_id:
            return V3ToolResult(
                "error",
                "No UI snapshot exists. Observe the screen again.",
                error_category="snapshot_missing",
                retryable=True,
            )
        if snapshot_id and snapshot_id != snapshot.snapshot_id:
            return V3ToolResult(
                "error",
                "The UI snapshot is stale. Observe the screen again.",
                error_category="stale_snapshot",
                retryable=True,
            )
        if time.time() - snapshot.created_at > self.snapshot_ttl_seconds:
            return V3ToolResult(
                "error",
                "The UI snapshot expired. Observe the screen again.",
                error_category="stale_snapshot",
                retryable=True,
            )
        current_hwnd = str(int(win32gui.GetForegroundWindow()))
        if current_hwnd != snapshot.window_id:
            return V3ToolResult(
                "error",
                "The active window changed after observation.",
                facts={"expected_window_id": snapshot.window_id, "actual_window_id": current_hwnd},
                error_category="window_changed",
                retryable=True,
            )
        target = snapshot.element(str(element_id))
        if not target or not target.visible or not target.enabled:
            return V3ToolResult(
                "error",
                "The requested UI element is no longer actionable.",
                error_category="element_missing",
                retryable=True,
            )
        x, y = target.center
        try:
            pyautogui.moveTo(x, y, duration=0.25)
            pyautogui.doubleClick() if double_click else pyautogui.click()
            return V3ToolResult(
                "ok",
                f"Clicked {target.control_type} {target.name}.",
                data={"snapshot_id": snapshot.snapshot_id, "element_id": target.element_id, "observe_needed": True},
                facts={
                    "action": "click",
                    "element": asdict(target),
                    "source_revision": snapshot.revision,
                    "observe_needed": True,
                },
            )
        except Exception as exc:
            return V3ToolResult(
                "error", "UI click failed.", error=str(exc), error_category="ui_click_failed", retryable=True
            )

    def capture_screen(self) -> V3ToolResult:
        try:
            import pyautogui
        except ImportError as exc:
            return V3ToolResult(
                "error",
                "Screen capture dependency is unavailable.",
                error=str(exc),
                error_category="dependency_missing",
            )
        try:
            screenshot_id = f"screen_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            path = self.state_dir / f"{screenshot_id}.png"
            image = pyautogui.screenshot()
            image.save(path)
            revision = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
            window_facts: dict[str, Any] = {}
            try:
                import win32gui

                hwnd = int(win32gui.GetForegroundWindow())
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                window_facts = {
                    "window_id": str(hwnd),
                    "window_title": str(win32gui.GetWindowText(hwnd) or ""),
                    "window_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
                }
            except Exception:
                window_facts = {}
            facts = {
                "screenshot_id": screenshot_id,
                "revision": revision,
                "width": int(image.size[0]),
                "height": int(image.size[1]),
                "created_at": time.time(),
                "internal_artifact": True,
                **window_facts,
            }
            self.screen_store.save({"schema_version": SCHEMA_VERSION, "path": str(path), **facts})
            return V3ToolResult(
                "ok",
                "Screen captured for internal observation.",
                data={"path": str(path), "file_path": str(path), **facts},
                facts=facts,
                artifacts=[str(path)],
            )
        except Exception as exc:
            return V3ToolResult(
                "error",
                "Screen capture failed.",
                error=str(exc),
                error_category="screen_capture_failed",
                retryable=True,
            )

    def click_screen(self, screenshot_id: str, x: int, y: int, double_click: bool = False) -> V3ToolResult:
        try:
            import pyautogui
            import win32gui
        except ImportError as exc:
            return V3ToolResult(
                "error", "Screen click dependency is unavailable.", error=str(exc), error_category="dependency_missing"
            )
        state = self.screen_store.load()
        if not state or str(state.get("screenshot_id") or "") != str(screenshot_id or ""):
            return V3ToolResult(
                "error",
                "The screenshot is stale. Capture the screen again.",
                error_category="stale_snapshot",
                retryable=True,
            )
        if time.time() - float(state.get("created_at") or 0.0) > self.snapshot_ttl_seconds:
            return V3ToolResult(
                "error",
                "The screenshot expired. Capture the screen again.",
                error_category="stale_snapshot",
                retryable=True,
            )
        width, height = int(state.get("width") or 0), int(state.get("height") or 0)
        px, py = int(x), int(y)
        if px < 0 or py < 0 or px >= width or py >= height:
            return V3ToolResult(
                "error", "Click coordinates are outside the captured screen.", error_category="invalid_arguments"
            )
        current = pyautogui.size()
        if current.width != width or current.height != height:
            return V3ToolResult(
                "error", "Screen dimensions changed after capture.", error_category="stale_snapshot", retryable=True
            )
        current_hwnd = int(win32gui.GetForegroundWindow())
        captured_window_id = str(state.get("window_id") or "")
        if captured_window_id and str(current_hwnd) != captured_window_id:
            return V3ToolResult(
                "error",
                "The active window changed after the screenshot was captured.",
                error_category="window_changed",
                retryable=True,
            )
        left, top, right, bottom = win32gui.GetWindowRect(current_hwnd)
        if right - left >= 40 and bottom - top >= 30 and not (left <= px < right and top <= py < bottom):
            return V3ToolResult(
                "error",
                "The requested coordinates are outside the active window.",
                facts={"active_window_bounds": {"left": left, "top": top, "right": right, "bottom": bottom}},
                error_category="outside_active_window",
            )
        try:
            pyautogui.moveTo(px, py, duration=0.25)
            pyautogui.doubleClick() if double_click else pyautogui.click()
            return V3ToolResult(
                "ok",
                "Screen position clicked.",
                data={"screenshot_id": screenshot_id, "x": px, "y": py, "observe_needed": True},
                facts={"action": "click_screen", "source_revision": screenshot_id, "observe_needed": True},
            )
        except Exception as exc:
            return V3ToolResult(
                "error", "Screen click failed.", error=str(exc), error_category="screen_click_failed", retryable=True
            )

    def list_windows(self, max_results: int = 50) -> V3ToolResult:
        try:
            import win32gui
        except ImportError as exc:
            return V3ToolResult(
                "error",
                "Window enumeration dependency is unavailable.",
                error=str(exc),
                error_category="dependency_missing",
            )
        windows: list[dict[str, Any]] = []
        try:
            limit = max(1, min(int(max_results or 50), 100))

            def collect(hwnd: int, _extra: Any) -> None:
                if len(windows) >= limit or not win32gui.IsWindowVisible(hwnd):
                    return
                title = " ".join(str(win32gui.GetWindowText(hwnd) or "").split())
                if not title:
                    return
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if right - left >= 40 and bottom - top >= 30:
                    windows.append(
                        {
                            "window_id": str(hwnd),
                            "title": title[:240],
                            "bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
                        }
                    )

            win32gui.EnumWindows(collect, None)
            return V3ToolResult(
                "ok",
                "Visible windows listed.",
                data={"windows": windows, "count": len(windows)},
                facts={"windows": windows},
            )
        except Exception as exc:
            return V3ToolResult(
                "error", "Failed to list windows.", error=str(exc), error_category="window_list_failed", retryable=True
            )

    def focus_window(self, window_id: str) -> V3ToolResult:
        try:
            import win32con
            import win32gui
        except ImportError as exc:
            return V3ToolResult(
                "error", "Window focus dependency is unavailable.", error=str(exc), error_category="dependency_missing"
            )
        try:
            hwnd = int(str(window_id).strip())
            if not win32gui.IsWindow(hwnd):
                return V3ToolResult(
                    "error", "Window id is no longer valid.", error_category="window_missing", retryable=True
                )
            title = str(win32gui.GetWindowText(hwnd) or "target window")
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                from pywinauto import Desktop

                Desktop(backend="uia").window(handle=hwnd).set_focus()
            time.sleep(0.3)
            if int(win32gui.GetForegroundWindow()) != hwnd:
                return V3ToolResult(
                    "error", "Window focus could not be verified.", error_category="focus_failed", retryable=True
                )
            return V3ToolResult(
                "ok",
                "Window focused.",
                data={"window_id": str(hwnd), "window_title": title},
                facts={"window_id": str(hwnd), "window_title": title},
            )
        except (TypeError, ValueError):
            return V3ToolResult("error", "Invalid window id.", error_category="invalid_arguments")
        except Exception as exc:
            return V3ToolResult(
                "error", "Failed to focus window.", error=str(exc), error_category="focus_failed", retryable=True
            )
