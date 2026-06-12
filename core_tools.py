import base64
import fnmatch
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import requests
from openai import OpenAI

from agent_latency import DEFAULT_MEDIA_CACHE, media_type_for, summarize_vision_text
from agent_social import DEFAULT_SOCIAL_STICKER_INDEX, is_safe_sticker

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")
HISTORY_DIR = os.path.join(WORKSPACE_DIR, "chat_history")
ASSETS_DIR = os.path.join(WORKSPACE_DIR, "assets")
TG_IMAGES_DIR = os.path.join(ASSETS_DIR, "tg_images")
STICKERS_DIR = os.path.join(ASSETS_DIR, "stickers")
STICKERS_INDEX_FILE = os.path.join(ASSETS_DIR, "stickers_index.json")
BRAIN_DIR = os.path.join(WORKSPACE_DIR, "brain")
MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")
TASKS_DIR = os.path.join(WORKSPACE_DIR, "tasks")
PROJECT_CACHE_DIR = os.path.join(WORKSPACE_DIR, "project_cache")
PROJECT_OPT_DIR = os.path.join(WORKSPACE_DIR, "project_opt")
ASYNC_LOGS_DIR = os.path.join(PROJECT_CACHE_DIR, "async_logs")

PERSONALITY_FILE = os.path.join(BRAIN_DIR, "personality.md")
RULES_FILE = os.path.join(BRAIN_DIR, "rules.md")
PROFILE_FILE = os.path.join(MEMORY_DIR, "profile.json")
MEMORY_FILE = os.path.join(MEMORY_DIR, "memory.md")
TASK_PLAN_FILE = os.path.join(TASKS_DIR, "task_plan.md")
CHAT_ID_FILE = os.path.join(WORKSPACE_DIR, "tg_chat_id.txt")
UI_MAP_FILE = os.path.join(PROJECT_CACHE_DIR, "ui_map.json")

for folder in [
    WORKSPACE_DIR,
    HISTORY_DIR,
    ASSETS_DIR,
    TG_IMAGES_DIR,
    STICKERS_DIR,
    BRAIN_DIR,
    MEMORY_DIR,
    TASKS_DIR,
    PROJECT_CACHE_DIR,
    PROJECT_OPT_DIR,
    ASYNC_LOGS_DIR,
]:
    os.makedirs(folder, exist_ok=True)


def _read_env_file() -> dict[str, str]:
    env_path = os.path.join(ROOT_DIR, ".env")
    values: dict[str, str] = {}
    if not os.path.exists(env_path):
        return values
    try:
        with open(env_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return values


_ENV_FILE = _read_env_file()
API_KEY = os.getenv("SILICONFLOW_API_KEY") or _ENV_FILE.get("SILICONFLOW_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or _ENV_FILE.get("TELEGRAM_BOT_TOKEN", "")


def safe_decode(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "utf-8-sig", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def truncate_text(text: str, limit: int = 6000) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def resolve_path(filename: str) -> str:
    if not filename:
        return WORKSPACE_DIR
    if os.path.isabs(filename):
        return os.path.abspath(filename)
    return os.path.abspath(os.path.join(WORKSPACE_DIR, filename))


def is_workspace_path(path: str) -> bool:
    try:
        return os.path.commonpath([WORKSPACE_DIR, os.path.abspath(path)]) == WORKSPACE_DIR
    except ValueError:
        return False


def _file_to_data_url(filepath: str) -> str:
    mime_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    with open(filepath, "rb") as file:
        encoded = base64.b64encode(file.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def find_sticker_file(name: str) -> str:
    if not os.path.isdir(STICKERS_DIR):
        return ""
    base = os.path.basename((name or "").strip())
    candidates: list[tuple[str, str]] = []
    for actual in os.listdir(STICKERS_DIR):
        actual_path = os.path.join(STICKERS_DIR, actual)
        if os.path.isfile(actual_path):
            candidates.append((actual, actual_path))
            if actual == base:
                return actual_path

    normalized_base = unicodedata.normalize("NFKC", base).casefold()
    for actual, actual_path in candidates:
        if unicodedata.normalize("NFKC", actual).casefold() == normalized_base:
            return actual_path

    if "*" in base or "?" in base:
        for actual, actual_path in candidates:
            if fnmatch.fnmatchcase(actual.casefold(), base.casefold()):
                return actual_path
    return ""


@dataclass
class ToolResult:
    status: str
    message: str
    data: Any = None
    error: str = ""
    requires_permission: bool = False

    def to_text(self) -> str:
        payload = {
            "status": self.status,
            "message": self.message,
            "requires_permission": self.requires_permission,
        }
        if self.error:
            payload["error"] = self.error
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False)


class AgentTool:
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        parameters: dict,
        requires_confirm: bool = False,
    ):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters
        self.requires_confirm = requires_confirm


TELEGRAM_CONTEXT = {"chat_id": "", "message_id": ""}


def set_telegram_context(chat_id: str | int = "", message_id: str | int = "") -> None:
    TELEGRAM_CONTEXT["chat_id"] = str(chat_id or "")
    TELEGRAM_CONTEXT["message_id"] = str(message_id or "")


def _ok(message: str, data: Any = None) -> ToolResult:
    return ToolResult("ok", message, data=data)


def _error(message: str, error: str = "") -> ToolResult:
    return ToolResult("error", message, error=error)


def resolve_command_cwd(cwd: str = "project") -> tuple[str, str]:
    requested = (cwd or "project").strip()
    normalized = requested.casefold()
    if normalized in {"project", "root", "."}:
        return ROOT_DIR, "project"
    if normalized in {"workspace", "work"}:
        return WORKSPACE_DIR, "workspace"
    return "", requested


def _cwd_retry_hint(command: str, resolved_cwd: str) -> str:
    command = command or ""
    if resolved_cwd == WORKSPACE_DIR and any(name in command for name in ("core_tools.py", "core_agent.py", "main.py", "self_test.py")):
        return "This looks like a project-root command. Retry with cwd='project'."
    if "No such file" in command or "cannot find" in command.casefold():
        return "Check cwd and file path, then retry with cwd='project' or cwd='workspace'."
    return ""


def _run_command(command: str, timeout: int = 60, cwd: str = "project") -> ToolResult:
    if not command or not command.strip():
        return _error("Command is empty.")
    resolved_cwd, cwd_label = resolve_command_cwd(cwd)
    if not resolved_cwd:
        return ToolResult(
            "error",
            "Invalid cwd.",
            data={"cwd": cwd, "allowed_cwd": ["project", "workspace"], "project_root": ROOT_DIR, "workspace": WORKSPACE_DIR},
            error="cwd must be 'project' or 'workspace'.",
        )
    try:
        timeout = max(1, min(int(timeout), 300))
    except Exception:
        timeout = 60
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=resolved_cwd,
            capture_output=True,
            timeout=timeout,
        )
        output = safe_decode(result.stdout)
        stderr = safe_decode(result.stderr)
        message = output.strip() or "(no stdout)"
        retry_hint = ""
        if result.returncode != 0 and re.search(r"No such file|cannot find|找不到|無法.*檔案", stderr + "\n" + output, flags=re.IGNORECASE):
            retry_hint = _cwd_retry_hint(command, resolved_cwd) or "Command could not find a file. Verify cwd and paths."
        data = {
            "returncode": result.returncode,
            "stdout": truncate_text(output),
            "stderr": truncate_text(stderr),
            "cwd": cwd_label,
            "resolved_cwd": resolved_cwd,
            "project_root": ROOT_DIR,
            "retry_hint": retry_hint,
        }
        if result.returncode == 0:
            return _ok("Command completed.", data)
        return ToolResult("error", "Command failed.", data=data, error=truncate_text(stderr or message))
    except subprocess.TimeoutExpired:
        return ToolResult("error", f"Command timed out after {timeout}s.", data={"cwd": cwd_label, "resolved_cwd": resolved_cwd})
    except Exception as exc:
        return ToolResult("error", "Command raised an exception.", data={"cwd": cwd_label, "resolved_cwd": resolved_cwd}, error=str(exc))


def real_get_screen_ui() -> ToolResult:
    try:
        import pywinauto
        import win32gui
    except ImportError:
        return _error("Missing dependencies. Install pywinauto pyautogui pywin32.")
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return _error("No active foreground window.")
        title = win32gui.GetWindowText(hwnd)
        window = pywinauto.Desktop(backend="uia").window(handle=hwnd)
        ui_map = {}
        lines = [f"Active window: {title}", "Clickable/input elements:"]
        element_id = 1
        valid_types = {"Button", "Edit", "MenuItem", "TabItem", "ListItem", "Document"}
        for wrapper in window.descendants():
            try:
                ctrl_type = wrapper.element_info.control_type
                name = wrapper.window_text() or wrapper.element_info.name
                if ctrl_type not in valid_types or not name or not wrapper.is_visible():
                    continue
                rect = wrapper.rectangle()
                ui_map[str(element_id)] = {
                    "name": name,
                    "type": ctrl_type,
                    "x": (rect.left + rect.right) // 2,
                    "y": (rect.top + rect.bottom) // 2,
                }
                lines.append(f"[{element_id}] {ctrl_type}: {name}")
                element_id += 1
                if element_id > 100:
                    lines.append("...(truncated)")
                    break
            except Exception:
                continue
        with open(UI_MAP_FILE, "w", encoding="utf-8") as file:
            json.dump(ui_map, file, ensure_ascii=False, indent=2)
        return _ok("\n".join(lines), {"count": len(ui_map)})
    except Exception as exc:
        return _error("Failed to inspect screen UI.", str(exc))


def real_click_ui_element(element_id: str, double_click: bool = False) -> ToolResult:
    try:
        import pyautogui
    except ImportError:
        return _error("Missing pyautogui.")
    if not os.path.exists(UI_MAP_FILE):
        return _error("UI map not found. Call get_screen_ui first.")
    try:
        with open(UI_MAP_FILE, "r", encoding="utf-8") as file:
            ui_map = json.load(file)
        target = ui_map.get(str(element_id))
        if not target:
            return _error(f"Element id {element_id} was not found.")
        pyautogui.moveTo(target["x"], target["y"], duration=0.4)
        pyautogui.doubleClick() if double_click else pyautogui.click()
        return _ok(f"Clicked {target['type']} {target['name']}.")
    except Exception as exc:
        return _error("Click failed.", str(exc))


def real_type_keyboard(text: str, press_enter: bool = False) -> ToolResult:
    try:
        import pyautogui
        import pyperclip
    except ImportError:
        return _error("Missing pyautogui or pyperclip.")
    try:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        if press_enter:
            pyautogui.press("enter")
        return _ok("Typed text through clipboard paste.")
    except Exception as exc:
        return _error("Typing failed.", str(exc))


def real_press_hotkey(keys: str) -> ToolResult:
    try:
        import pyautogui
        key_list = [key.strip() for key in keys.split(",") if key.strip()]
        if not key_list:
            return _error("No hotkey was provided.")
        pyautogui.hotkey(*key_list)
        return _ok(f"Pressed hotkey: {', '.join(key_list)}")
    except Exception as exc:
        return _error("Hotkey failed.", str(exc))


def _background_worker(command: str, task_name: str, output_file: str) -> None:
    result = _run_command(command, timeout=300)
    with open(output_file, "w", encoding="utf-8") as file:
        file.write(result.to_text())
    if os.path.exists(CHAT_ID_FILE) and TG_TOKEN:
        try:
            with open(CHAT_ID_FILE, "r", encoding="utf-8") as file:
                chat_id = file.read().strip()
            if chat_id:
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": f"Background task finished: {task_name}\nLog: {output_file}"},
                    timeout=15,
                )
        except Exception:
            pass


def real_execute_async_command(command: str, task_name: str) -> ToolResult:
    task_id = int(time.time())
    output_file = os.path.join(ASYNC_LOGS_DIR, f"task_result_{task_id}.txt")
    thread = threading.Thread(target=_background_worker, args=(command, task_name, output_file), daemon=True)
    thread.start()
    return _ok(f"Background task started: {task_name}", {"log_file": output_file})


def real_create_plan(objective: str, steps: list) -> ToolResult:
    try:
        lines = [f"# Task Plan: {objective}", ""]
        for index, step in enumerate(steps, 1):
            lines.append(f"{index}. [ ] {step}")
        with open(TASK_PLAN_FILE, "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
        return _ok("Task plan created.", {"file": TASK_PLAN_FILE})
    except Exception as exc:
        return _error("Failed to create plan.", str(exc))


def real_update_plan(step_number: int, status: str, notes: str = "") -> ToolResult:
    if not os.path.exists(TASK_PLAN_FILE):
        return _error("Task plan file not found.")
    try:
        with open(TASK_PLAN_FILE, "r", encoding="utf-8") as file:
            lines = file.readlines()
        done_values = {"done", "complete", "completed", "完成", "瀹屾垚", "x", "yes", "true"}
        mark = "x" if str(status).strip().casefold() in done_values else " "
        updated = False
        for index, line in enumerate(lines):
            if line.startswith(f"{step_number}. ["):
                body = line.split("] ", 1)[1].split(" - ", 1)[0].strip()
                suffix = f" - {notes}" if notes else ""
                lines[index] = f"{step_number}. [{mark}] {body}{suffix}\n"
                updated = True
                break
        if not updated:
            return _error(f"Step {step_number} was not found.")
        with open(TASK_PLAN_FILE, "w", encoding="utf-8") as file:
            file.writelines(lines)
        return _ok(f"Step {step_number} updated.")
    except Exception as exc:
        return _error("Failed to update plan.", str(exc))


def real_execute_python(code: str, timeout: int = 30) -> ToolResult:
    if not code or not code.strip():
        return _error("Python code is empty.")
    filepath = os.path.join(PROJECT_CACHE_DIR, "_temp_script.py")
    try:
        timeout = max(1, min(int(timeout), 120))
    except Exception:
        timeout = 30
    try:
        with open(filepath, "w", encoding="utf-8") as file:
            file.write(code)
        result = subprocess.run([sys.executable, filepath], cwd=WORKSPACE_DIR, capture_output=True, timeout=timeout)
        stdout = safe_decode(result.stdout)
        stderr = safe_decode(result.stderr)
        data = {"returncode": result.returncode, "stdout": truncate_text(stdout), "stderr": truncate_text(stderr)}
        if result.returncode == 0:
            return _ok("Python completed.", data)
        return ToolResult("error", "Python failed.", data=data, error=truncate_text(stderr))
    except subprocess.TimeoutExpired:
        return _error(f"Python timed out after {timeout}s.")
    except Exception as exc:
        return _error("Python raised an exception.", str(exc))


def real_search_in_files(keyword: str, directory: str = ".") -> ToolResult:
    target_dir = resolve_path(directory)
    if not os.path.exists(target_dir):
        return _error("Directory not found.")
    results = []
    valid_extensions = {".py", ".txt", ".md", ".json", ".js", ".html", ".css", ".yml", ".yaml"}
    try:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for filename in files:
                if os.path.splitext(filename)[1].lower() not in valid_extensions:
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as file:
                        for number, line in enumerate(file, 1):
                            if keyword.casefold() in line.casefold():
                                results.append(f"{os.path.relpath(path, WORKSPACE_DIR)}:{number}: {line.strip()}")
                                if len(results) >= 100:
                                    return _ok("Search results truncated.", results)
                except Exception:
                    continue
        return _ok("Search completed.", results)
    except Exception as exc:
        return _error("Search failed.", str(exc))


def real_list_files(directory: str = ".", recursive: bool = False, max_results: int = 200) -> ToolResult:
    target_dir = resolve_path(directory)
    if not os.path.exists(target_dir):
        return _error("Path not found.")
    if not os.path.isdir(target_dir):
        return _error("Path is not a directory.")
    try:
        max_results = max(1, min(int(max_results), 1000))
    except Exception:
        max_results = 200
    results = []
    try:
        if recursive:
            for root, dirs, files in os.walk(target_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                for name in sorted(dirs + files):
                    path = os.path.join(root, name)
                    results.append(os.path.relpath(path, WORKSPACE_DIR) + ("/" if os.path.isdir(path) else ""))
                    if len(results) >= max_results:
                        return _ok("File list truncated.", results)
        else:
            for name in sorted(os.listdir(target_dir)):
                if name.startswith(".") or name == "__pycache__":
                    continue
                path = os.path.join(target_dir, name)
                results.append(os.path.relpath(path, WORKSPACE_DIR) + ("/" if os.path.isdir(path) else ""))
                if len(results) >= max_results:
                    return _ok("File list truncated.", results)
        return _ok("File list completed.", results)
    except Exception as exc:
        return _error("Failed to list files.", str(exc))


def real_execute_command(command: str, timeout: int = 60, cwd: str = "project") -> ToolResult:
    return _run_command(command, timeout, cwd)


def real_web_search(query: str) -> ToolResult:
    try:
        results = list(DDGS().text(query, max_results=3))
        return _ok("Search completed.", results)
    except Exception as exc:
        return _error("Web search failed.", str(exc))


def real_read_webpage(url: str) -> ToolResult:
    if not re.match(r"^https?://", url or "", flags=re.IGNORECASE):
        return _error("URL must start with http:// or https://.")
    try:
        response = requests.get("https://r.jina.ai/" + url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if response.ok and response.text.strip():
            return _ok("Webpage read.", truncate_text(response.text, 8000))
        return _error(f"Reader returned HTTP {response.status_code}.", response.text[:500])
    except Exception as exc:
        return _error("Failed to read webpage.", str(exc))


def real_download_file(url: str, filename: str) -> ToolResult:
    if not re.match(r"^https?://", url or "", flags=re.IGNORECASE):
        return _error("URL must start with http:// or https://.")
    filepath = resolve_path(filename)
    if not is_workspace_path(filepath):
        return _error("download_file can only save inside workspace.")
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        total = 0
        with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            with open(filepath, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        raise ValueError("File exceeds 50MB limit.")
                    file.write(chunk)
        return _ok("File downloaded.", {"path": filepath, "bytes": total})
    except Exception as exc:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        return _error("Download failed.", str(exc))


def real_analyze_media(file_path: str, prompt: str = "Describe this image.") -> ToolResult:
    filepath = resolve_path(file_path)
    if not os.path.exists(filepath):
        return _error(f"File not found: {filepath}")
    media_type = media_type_for(filepath)
    if media_type in {"video_sticker", "video"}:
        entry = DEFAULT_MEDIA_CACHE.remember(filepath, media_type=media_type, short_caption="dynamic sticker/video; vision skipped")
        return _ok("Dynamic media recorded without image analysis.", {"media_type": media_type, "cache_key": entry.file_hash, "summary": entry.short_caption})
    cached = DEFAULT_MEDIA_CACHE.get_by_path(filepath)
    if cached and cached.vision_summary:
        return _ok("Image analysis cache hit.", {"summary": cached.vision_summary, "cache_key": cached.file_hash})
    if os.path.getsize(filepath) > 8 * 1024 * 1024:
        return _error("File exceeds 8MB image-analysis limit.")
    mime_type = mimetypes.guess_type(filepath)[0] or ""
    if not mime_type.startswith("image/"):
        return _error(f"analyze_media currently supports images only; got {mime_type or 'unknown'}.")
    if not API_KEY or len(API_KEY) < 10:
        return _error("SILICONFLOW_API_KEY is not configured.")
    model_candidates = [
        os.getenv("VISION_MODEL", "").strip(),
        "Qwen/Qwen3-VL-8B-Instruct",
        "Qwen/Qwen3-VL-32B-Instruct",
        "zai-org/GLM-4.5V",
    ]
    model_candidates = [m for index, m in enumerate(model_candidates) if m and m not in model_candidates[:index]]
    image_url = _file_to_data_url(filepath)
    errors = []
    for model in model_candidates:
        try:
            client = OpenAI(api_key=API_KEY, base_url="https://api.siliconflow.cn/v1", http_client=httpx.Client(timeout=90.0))
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                max_tokens=800,
            )
            content = response.choices[0].message.content.strip()
            entry = DEFAULT_MEDIA_CACHE.set_vision_summary(filepath, content)
            return _ok("Image analyzed.", {"summary": entry.vision_summary, "cache_key": entry.file_hash})
        except Exception as exc:
            errors.append(f"{model}: {str(exc)[:220]}")
    return _error("All vision models failed.", "\n".join(errors))


def real_read_file(filename: str) -> ToolResult:
    filepath = resolve_path(filename)
    if not os.path.exists(filepath):
        return _error(f"File not found: {filepath}")
    try:
        mime_type = mimetypes.guess_type(filepath)[0] or ""
        if mime_type.startswith("image/"):
            return _ok("This is an image file. Use analyze_media for vision content.", {"path": filepath})
        if os.path.getsize(filepath) > 2 * 1024 * 1024:
            return _error("File exceeds 2MB text read limit.")
        for encoding in ("utf-8", "utf-8-sig", "mbcs"):
            try:
                with open(filepath, "r", encoding=encoding) as file:
                    return _ok("File read.", file.read())
            except UnicodeDecodeError:
                continue
        return _error("File does not appear to be plain text.")
    except Exception as exc:
        return _error("Read failed.", str(exc))


def real_delete_file(filename: str) -> ToolResult:
    filepath = resolve_path(filename)
    if not is_workspace_path(filepath):
        return _error("delete_file can only delete files inside workspace.")
    if not os.path.exists(filepath):
        return _error(f"File not found: {filepath}")
    if os.path.isdir(filepath):
        return _error("delete_file refuses to delete directories.")
    try:
        os.remove(filepath)
        return _ok("File deleted.", {"path": filepath})
    except Exception as exc:
        return _error("Delete failed.", str(exc))


def real_send_telegram_media(file_path: str, caption: str = "") -> ToolResult:
    filepath = resolve_path(file_path)
    if not os.path.exists(filepath):
        sticker_candidate = find_sticker_file(file_path)
        if sticker_candidate:
            filepath = sticker_candidate
        else:
            return _error(f"File not found: {filepath}")
    if not TG_TOKEN:
        return _error("TELEGRAM_BOT_TOKEN is not configured.")
    if not os.path.exists(CHAT_ID_FILE):
        return _error("Telegram chat id is not recorded yet. Send /start to the bot first.")
    try:
        with open(CHAT_ID_FILE, "r", encoding="utf-8") as file:
            chat_id = file.read().strip()
        if not chat_id:
            return _error("Telegram chat id is empty.")
        ext = os.path.splitext(filepath)[1].lower()
        endpoint = "sendAnimation" if ext in {".gif", ".tgs", ".webm", ".mp4"} else "sendPhoto"
        field_name = "animation" if endpoint == "sendAnimation" else "photo"
        with open(filepath, "rb") as media:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/{endpoint}",
                data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                files={field_name: media},
                timeout=30,
            )
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.ok and data.get("ok", response.ok):
            return _ok("Telegram media sent.", {"file": filepath})
        fallback = _send_telegram_media_text_fallback(chat_id, filepath, caption, f"HTTP {response.status_code}: {response.text[:300]}")
        if fallback.status == "ok":
            return ToolResult(
                "ok",
                "Telegram media upload failed, but a text fallback was sent.",
                data={"file": filepath, "fallback": fallback.data, "original_error": response.text[:500]},
            )
        return _error(f"Telegram send failed with HTTP {response.status_code}.", response.text[:500])
    except Exception as exc:
        try:
            chat_id = ""
            if os.path.exists(CHAT_ID_FILE):
                with open(CHAT_ID_FILE, "r", encoding="utf-8") as file:
                    chat_id = file.read().strip()
            fallback = _send_telegram_media_text_fallback(chat_id, filepath, caption, str(exc))
            if fallback.status == "ok":
                return ToolResult(
                    "ok",
                    "Telegram media upload failed, but a text fallback was sent.",
                    data={"file": filepath, "fallback": fallback.data, "original_error": str(exc)[:500]},
                )
        except Exception:
            pass
        return _error("Telegram media send failed.", str(exc))


def _send_telegram_media_text_fallback(chat_id: str, filepath: str, caption: str = "", reason: str = "") -> ToolResult:
    if not TG_TOKEN or not chat_id:
        return _error("Telegram text fallback is not configured.")
    try:
        safe_name = os.path.basename(filepath)
        text = (
            "媒體暫時發不出去，我先把結果位置留給你：\n"
            f"{safe_name}\n"
            f"{filepath}"
        )
        if caption:
            text = f"{caption[:500]}\n\n{text}"
        if reason:
            text += f"\n\n原因：{truncate_text(reason, 300)}"
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text[:3900]},
            timeout=15,
        )
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.ok and data.get("ok", response.ok):
            return _ok("Telegram text fallback sent.", {"chat_id": chat_id, "file": filepath})
        return _error("Telegram text fallback failed.", response.text[:500])
    except Exception as exc:
        return _error("Telegram text fallback raised an exception.", str(exc))


def real_react_to_message(emoji: str, chat_id: str = "", message_id: int | str = "") -> ToolResult:
    chat_id = str(chat_id or TELEGRAM_CONTEXT.get("chat_id") or "").strip()
    message_id = str(message_id or TELEGRAM_CONTEXT.get("message_id") or "").strip()
    emoji = (emoji or "").strip()
    if not chat_id or not message_id:
        return _error("Missing Telegram chat_id or message_id for reaction.")
    if not emoji:
        return _error("Reaction emoji is empty.")
    if not TG_TOKEN:
        return _error("TELEGRAM_BOT_TOKEN is not configured.")
    payload = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}], ensure_ascii=False),
        "is_big": False,
    }
    try:
        response = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/setMessageReaction", data=payload, timeout=15)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.ok and data.get("ok", response.ok):
            return _ok("Reaction sent.", {"emoji": emoji, "message_id": message_id})
        return _error(f"Reaction failed with HTTP {response.status_code}.", response.text[:500])
    except Exception as exc:
        return _error("Reaction request failed.", str(exc))


def real_write_file(filename: str, content: str) -> ToolResult:
    filepath = resolve_path(filename)
    if not is_workspace_path(filepath):
        return _error("write_file can only write inside workspace.")
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as file:
            file.write(content)
        return _ok("File written.", {"path": filepath})
    except Exception as exc:
        return _error("Write failed.", str(exc))


def _memory_quality_error(text: str, *, allow_replace: bool = False) -> str:
    value = (text or "").strip()
    if len(value) < 3:
        return "Memory content is too short to be useful."
    if len(value) > 1600:
        return "Memory content is too long; summarize it before saving."
    mojibake_hits = sum(value.count(marker) for marker in ["锛", "绂", "涓", "讳", "鍙", "鐨", "鈥", "€"])
    if mojibake_hits >= 2:
        return "Memory content looks mojibake/broken encoded; refusing to save it."
    if len(set(value)) <= 3 and len(value) > 20:
        return "Memory content looks like repeated junk."
    lowered = value.casefold()
    if not allow_replace and any(term in lowered for term in ["ignore previous personality", "replace personality", "system prompt", "developer message"]):
        return "Memory content looks like prompt/personality override, not a stable memory."
    return ""


def real_update_profile(key: str, value: str, category: str = "important_facts") -> ToolResult:
    valid_categories = {"basic_info", "preferences", "important_facts"}
    category = category if category in valid_categories else "important_facts"
    key = (key or "").strip()
    value = (value or "").strip()
    bad = _memory_quality_error(value)
    if bad:
        return _error(bad)
    try:
        if os.path.exists(PROFILE_FILE) and os.path.getsize(PROFILE_FILE) > 0:
            with open(PROFILE_FILE, "r", encoding="utf-8") as file:
                profile = json.load(file)
        else:
            profile = {"basic_info": {}, "preferences": [], "important_facts": []}
        profile.setdefault("basic_info", {})
        profile.setdefault("preferences", [])
        profile.setdefault("important_facts", [])
        if category == "basic_info":
            profile["basic_info"][key] = value
        else:
            item = f"{key}: {value}" if key else value
            if item not in profile[category]:
                profile[category].append(item)
        with open(PROFILE_FILE, "w", encoding="utf-8") as file:
            json.dump(profile, file, ensure_ascii=False, indent=2)
        _refresh_compiled_memory()
        return _ok("Profile updated.", {"category": category, "key": key})
    except Exception as exc:
        return _error("Profile update failed.", str(exc))


def real_update_memory(content: str, mode: str = "append") -> ToolResult:
    content = (content or "").strip()
    mode = mode if mode in {"append", "replace"} else "append"
    bad = _memory_quality_error(content, allow_replace=(mode == "replace"))
    if bad:
        return _error(bad)
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if mode == "replace":
            text = content + "\n"
        else:
            old = ""
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r", encoding="utf-8") as file:
                    old = file.read()
            text = old + f"\n- [{timestamp}] {content}\n"
        with open(MEMORY_FILE, "w", encoding="utf-8") as file:
            file.write(text)
        _refresh_compiled_memory()
        return _ok("Memory updated.", {"mode": mode})
    except Exception as exc:
        return _error("Memory update failed.", str(exc))


def _refresh_compiled_memory() -> None:
    try:
        from agent_memory import compile_memory, memory_health_check

        memory_health_check()
        compile_memory("chat", "")
    except Exception:
        pass


def real_search_sticker(emotion_or_keyword: str) -> ToolResult:
    if not os.path.isdir(STICKERS_DIR):
        return _error("Sticker directory not found.")
    if not is_safe_sticker(emotion_or_keyword or ""):
        return _ok("No matching sticker selected.", [])
    actual_files = [f for f in os.listdir(STICKERS_DIR) if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and is_safe_sticker(f)]
    social_matches = [name for name in DEFAULT_SOCIAL_STICKER_INDEX.choose(emotion_or_keyword, limit=8) if name in actual_files]
    if social_matches:
        return _ok("Social sticker matches found.", social_matches[:8])
    index_data = {}
    if os.path.exists(STICKERS_INDEX_FILE):
        try:
            with open(STICKERS_INDEX_FILE, "r", encoding="utf-8") as file:
                index_data = json.load(file)
        except Exception:
            pass
    keyword = (emotion_or_keyword or "").casefold()
    matches = [f for f in actual_files if keyword in f.casefold() or keyword in str(index_data.get(f, "")).casefold()]
    if matches:
        return _ok("Sticker matches found.", matches[:8])
    if API_KEY and len(API_KEY) > 10 and actual_files:
        try:
            options = "\n".join([f"- {f} {index_data.get(f, '')}" for f in actual_files[:120]])
            client = OpenAI(api_key=API_KEY, base_url="https://api.siliconflow.cn/v1", http_client=httpx.Client(timeout=10.0))
            response = client.chat.completions.create(
                model="deepseek-ai/DeepSeek-V3",
                messages=[{"role": "user", "content": f"Pick 1-3 sticker filenames for: {emotion_or_keyword}. Reply filenames only.\n{options}"}],
                max_tokens=80,
                temperature=0.2,
            )
            picked = []
            for line in response.choices[0].message.content.splitlines():
                candidate = line.strip().lstrip("- ").strip()
                if candidate in actual_files:
                    picked.append(candidate)
            if picked:
                return _ok("Semantic sticker matches found.", picked[:3])
        except Exception:
            pass
    return _ok("No matching sticker found.", [])


def real_search_knowledge(query: str, limit: int = 5) -> ToolResult:
    try:
        from agent_knowledge import search_knowledge
        from agent_hooks import emit_trace

        hits = search_knowledge(query, limit=limit)
        emit_trace("KnowledgeSearch", query=(query or "")[:160], hit_count=len(hits), source="tool")
        return _ok("Knowledge search completed.", {"query": query, "hits": hits})
    except Exception as exc:
        return _error("Knowledge search failed.", str(exc))


def real_read_knowledge(chunk_id: str) -> ToolResult:
    try:
        from agent_knowledge import read_knowledge

        chunk = read_knowledge(chunk_id)
        if not chunk:
            return _ok("Knowledge chunk not found.", {"chunk_id": chunk_id})
        return _ok("Knowledge chunk loaded.", chunk)
    except Exception as exc:
        return _error("Knowledge read failed.", str(exc))


def real_reindex_workspace() -> ToolResult:
    try:
        from agent_knowledge import reindex_workspace

        manifest = reindex_workspace()
        return _ok("Knowledge index rebuilt.", manifest)
    except Exception as exc:
        return _error("Knowledge reindex failed.", str(exc))


get_screen_ui_tool = AgentTool("get_screen_ui", "Inspect visible UI controls on the active window.", real_get_screen_ui, {"type": "object", "properties": {}}, False)
click_ui_element_tool = AgentTool("click_ui_element", "Click an element id returned by get_screen_ui.", real_click_ui_element, {"type": "object", "properties": {"element_id": {"type": "string"}, "double_click": {"type": "boolean"}}, "required": ["element_id"]}, True)
type_keyboard_tool = AgentTool("type_keyboard", "Type text into the active UI using clipboard paste.", real_type_keyboard, {"type": "object", "properties": {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, "required": ["text"]}, True)
press_hotkey_tool = AgentTool("press_hotkey", "Press a system hotkey, e.g. ctrl,c or win,d.", real_press_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True)
create_plan_tool = AgentTool("create_plan", "Create a task plan file.", real_create_plan, {"type": "object", "properties": {"objective": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}}, "required": ["objective", "steps"]}, False)
update_plan_tool = AgentTool("update_plan", "Update one step in the task plan.", real_update_plan, {"type": "object", "properties": {"step_number": {"type": "integer"}, "status": {"type": "string"}, "notes": {"type": "string"}}, "required": ["step_number", "status"]}, False)
list_files_tool = AgentTool("list_files", "List files under the workspace.", real_list_files, {"type": "object", "properties": {"directory": {"type": "string"}, "recursive": {"type": "boolean"}, "max_results": {"type": "integer"}}}, False)
search_in_files_tool = AgentTool("search_in_files", "Search text files under the workspace.", real_search_in_files, {"type": "object", "properties": {"keyword": {"type": "string"}, "directory": {"type": "string"}}, "required": ["keyword"]}, False)
execute_async_command_tool = AgentTool("execute_async_command", "Run a shell command in the background and save its output.", real_execute_async_command, {"type": "object", "properties": {"command": {"type": "string"}, "task_name": {"type": "string"}}, "required": ["command", "task_name"]}, True)
search_tool = AgentTool("web_search", "Search the web.", real_web_search, {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, False)
read_webpage_tool = AgentTool("read_webpage", "Read a webpage as text.", real_read_webpage, {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, False)
download_file_tool = AgentTool("download_file", "Download a URL into the workspace.", real_download_file, {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}}, "required": ["url", "filename"]}, True)
analyze_media_tool = AgentTool("analyze_media", "Analyze a local image file.", real_analyze_media, {"type": "object", "properties": {"file_path": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["file_path"]}, False)
read_file_tool = AgentTool("read_file", "Read a text file.", real_read_file, {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}, False)
write_file_tool = AgentTool("write_file", "Write a file under the workspace.", real_write_file, {"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["filename", "content"]}, True)
delete_file_tool = AgentTool("delete_file", "Delete a single file under the workspace.", real_delete_file, {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}, True)
send_telegram_media_tool = AgentTool("send_telegram_media", "Send a local image/GIF/sticker file to the recorded Telegram chat.", real_send_telegram_media, {"type": "object", "properties": {"file_path": {"type": "string"}, "caption": {"type": "string"}}, "required": ["file_path"]}, True)
react_to_message_tool = AgentTool("react_to_message", "Add an emoji reaction to the current or specified Telegram message.", real_react_to_message, {"type": "object", "properties": {"emoji": {"type": "string"}, "chat_id": {"type": "string"}, "message_id": {"type": "integer"}}, "required": ["emoji"]}, False)
update_profile_tool = AgentTool("update_profile", "Update long-term structured profile memory with quality checks.", real_update_profile, {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "category": {"type": "string", "enum": ["basic_info", "preferences", "important_facts"]}}, "required": ["key", "value"]}, False)
update_memory_tool = AgentTool("update_memory", "Update long-term narrative memory with quality checks.", real_update_memory, {"type": "object", "properties": {"content": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]}}, "required": ["content"]}, False)
execute_python_tool = AgentTool("execute_python", "Run Python code once with timeout and structured output.", real_execute_python, {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["code"]}, True)
execute_command_tool = AgentTool("execute_command", "Run a shell command with timeout and structured output.", real_execute_command, {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}, "cwd": {"type": "string", "enum": ["project", "workspace"]}}, "required": ["command"]}, True)
search_sticker_tool = AgentTool("search_sticker", "Search local sticker filenames by emotion or keyword.", real_search_sticker, {"type": "object", "properties": {"emotion_or_keyword": {"type": "string"}}, "required": ["emotion_or_keyword"]}, False)
search_knowledge_tool = AgentTool("search_knowledge", "Search the local engineering knowledge index.", real_search_knowledge, {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, False)
read_knowledge_tool = AgentTool("read_knowledge", "Read one local engineering knowledge chunk by id.", real_read_knowledge, {"type": "object", "properties": {"chunk_id": {"type": "string"}}, "required": ["chunk_id"]}, False)
reindex_workspace_tool = AgentTool("reindex_workspace", "Rebuild the local engineering knowledge index.", real_reindex_workspace, {"type": "object", "properties": {}}, False)

ALL_TOOLS = [
    get_screen_ui_tool,
    click_ui_element_tool,
    type_keyboard_tool,
    press_hotkey_tool,
    create_plan_tool,
    update_plan_tool,
    list_files_tool,
    search_in_files_tool,
    execute_async_command_tool,
    search_tool,
    read_webpage_tool,
    download_file_tool,
    analyze_media_tool,
    read_file_tool,
    write_file_tool,
    delete_file_tool,
    send_telegram_media_tool,
    react_to_message_tool,
    update_profile_tool,
    update_memory_tool,
    execute_python_tool,
    execute_command_tool,
    search_sticker_tool,
    search_knowledge_tool,
    read_knowledge_tool,
    reindex_workspace_tool,
]
