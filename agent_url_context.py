import hashlib
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from agent_hooks import emit_trace

ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")
PROJECT_CACHE_DIR = os.path.join(WORKSPACE_DIR, "project_cache")
URL_CACHE_FILE = os.path.join(PROJECT_CACHE_DIR, "url_context_cache.json")
URL_PREVIEW_DIR = os.path.join(PROJECT_CACHE_DIR, "url_previews")

URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", flags=re.IGNORECASE)
METADATA_TTL_SECONDS = 24 * 60 * 60
FAILED_TTL_SECONDS = 30 * 60
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) YueYueAgent/1.0"
MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
VIDEO_SOCIAL_PLATFORMS = {"youtube", "bilibili", "douyin", "tiktok", "instagram", "x"}


@dataclass
class URLContextEntry:
    url: str
    canonical_url: str = ""
    domain: str = ""
    platform: str = "unknown"
    content_type: str = "unknown"
    title: str = ""
    description: str = ""
    thumbnail_url: str = ""
    thumbnail_path: str = ""
    screenshot_path: str = ""
    transcript_summary: str = ""
    vision_summary: str = ""
    status: str = "new"
    failure_reason: str = ""
    depth: str = "metadata"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self, max_chars: int = 900) -> str:
        parts = [
            f"platform={self.platform}",
            f"type={self.content_type}",
            f"status={self.status}",
        ]
        if self.title:
            parts.append(f"title={self.title}")
        if self.description:
            parts.append(f"description={self.description}")
        if self.transcript_summary:
            parts.append(f"transcript={self.transcript_summary}")
        if self.vision_summary:
            parts.append(f"vision={self.vision_summary}")
        if self.thumbnail_path:
            parts.append(f"thumbnail={self.thumbnail_path}")
        if self.screenshot_path:
            parts.append(f"screenshot={self.screenshot_path}")
        if self.failure_reason:
            parts.append(f"limit={human_failure_reason(self.failure_reason)}")
        text = " | ".join(parts)
        return text[:max_chars]


class URLContextCache:
    # Every shared URL adds an entry; cap it so a long-lived deployment does not
    # accumulate an ever-growing cache in memory and on disk.
    MAX_ENTRIES = 300

    def __init__(self, path: str = URL_CACHE_FILE):
        self.path = path
        self.entries: dict[str, URLContextEntry] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = {}
            return
        try:
            with open(self.path, encoding="utf-8") as file:
                raw = json.load(file)
            self.entries = {key: URLContextEntry(**value) for key, value in raw.items() if isinstance(value, dict)}
        except Exception:
            self.entries = {}

    def save(self) -> None:
        if len(self.entries) > self.MAX_ENTRIES:
            kept = sorted(self.entries.items(), key=lambda item: item[1].updated_at)[-self.MAX_ENTRIES :]
            self.entries = dict(kept)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({key: value.to_dict() for key, value in self.entries.items()}, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")

    def get(self, url_or_id: str) -> URLContextEntry | None:
        key = url_cache_key(url_or_id)
        return self.entries.get(key) or next((entry for entry in self.entries.values() if entry.url == url_or_id or entry.canonical_url == url_or_id), None)

    def inspect(self, url: str, depth: str = "auto", force: bool = False) -> URLContextEntry:
        normalized = normalize_url(url)
        key = url_cache_key(normalized)
        cached = self.entries.get(key)
        if cached and not force and _cache_fresh(cached, depth):
            emit_trace("url_context.cache_hit", url=normalized[:220], platform=cached.platform, status=cached.status, depth=depth)
            return cached

        entry = cached or URLContextEntry(url=normalized)
        entry.url = normalized
        entry.canonical_url = normalized
        entry.domain = domain_for_url(normalized)
        entry.platform = classify_url_platform(normalized)
        entry.depth = depth
        entry.updated_at = time.time()
        entry.failure_reason = ""
        try:
            _inspect_metadata(entry)
            if depth in {"preview", "full"} or depth == "auto" and entry.content_type in {"image"}:
                _inspect_preview(entry)
        except Exception as exc:
            entry.status = "partial" if (entry.title or entry.description or entry.thumbnail_path) else "error"
            entry.failure_reason = _classify_failure(str(exc))
        self.entries[key] = entry
        self.save()
        emit_trace("url_context.inspected", url=normalized[:220], platform=entry.platform, status=entry.status, depth=depth, failure_reason=entry.failure_reason)
        return entry

    def reindex(self) -> dict[str, Any]:
        self.load()
        return {"status": "ok", "count": len(self.entries), "path": self.path}


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text or ""):
        cleaned = normalize_url(match)
        if cleaned and cleaned not in seen:
            urls.append(cleaned)
            seen.add(cleaned)
    return urls[:8]


def normalize_url(url: str) -> str:
    return re.sub(r"[。。，，、)]+$", "", (url or "").strip())


def url_cache_key(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8", errors="replace")).hexdigest()[:24]


def domain_for_url(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").casefold().removeprefix("www.")
    except Exception:
        return ""


def classify_url_platform(url: str) -> str:
    domain = domain_for_url(url)
    if "youtube.com" in domain or "youtu.be" in domain:
        return "youtube"
    if "bilibili.com" in domain or "b23.tv" in domain:
        return "bilibili"
    if "douyin.com" in domain or "iesdouyin.com" in domain:
        return "douyin"
    if "tiktok.com" in domain:
        return "tiktok"
    if "instagram.com" in domain:
        return "instagram"
    if domain in {"x.com", "twitter.com"} or domain.endswith(".twitter.com"):
        return "x"
    mime = mimetypes.guess_type(urlparse(url).path)[0] or ""
    if mime.startswith("image/"):
        return "direct_image"
    return "website" if domain else "unknown"


def should_preview_url(text: str) -> bool:
    lowered = (text or "").casefold()
    markers = [
        "这个",
        "這個",
        "看一下",
        "看一看",
        "看看",
        "影片",
        "视频",
        "視頻",
        "她怎么",
        "她怎麼",
        "他怎么",
        "他怎麼",
        "好抽象",
        "怎么样",
        "怎麼樣",
        "怎么樣",
        "你觉得",
        "你覺得",
        "吐槽",
        "分析",
        "what do you think",
        "watch",
        "video",
    ]
    return any(marker in lowered for marker in markers)


def human_failure_reason(reason: str) -> str:
    mapping = {
        "login_or_permission_required": "平台需要登录或权限，不能匿名读取完整内容",
        "reader_service_blocked": "网页读取服务被目标或网络信誉限制挡住了",
        "region_or_platform_restricted": "平台或地区限制，不能稳定读取完整内容",
        "timeout": "读取超时",
        "extractor_failed": "视频解析器没拿到可用信息",
        "network_error": "网络连接失败",
        "metadata_sparse": "只拿到很少的公开页面信息",
        "platform_restricted_or_login_required": "平台限制或需要登录",
        "preview_unavailable": "预览不可用",
        "empty_response": "页面没有返回可读内容",
        "invalid_url": "URL 格式不正确",
    }
    return mapping.get(reason or "", reason or "未知限制")


def inspect_url(url: str, depth: str = "auto", force: bool = False) -> URLContextEntry:
    return DEFAULT_URL_CONTEXT_CACHE.inspect(url, depth=depth, force=force)


def read_url_context(url_or_id: str) -> URLContextEntry | None:
    return DEFAULT_URL_CONTEXT_CACHE.get(url_or_id)


def reindex_url_cache() -> dict[str, Any]:
    return DEFAULT_URL_CONTEXT_CACHE.reindex()


def inspect_urls_for_text(text: str, *, depth: str = "metadata") -> list[URLContextEntry]:
    entries: list[URLContextEntry] = []
    for url in extract_urls(text):
        entries.append(inspect_url(url, depth=depth))
    return entries


def render_url_context(entries: list[URLContextEntry], max_entries: int = 3) -> str:
    if not entries:
        return ""
    lines = ["URL context:"]
    for entry in entries[:max_entries]:
        lines.append(f"- {entry.url}\n  {entry.summary()}")
    return "\n".join(lines)


def _cache_fresh(entry: URLContextEntry, depth: str) -> bool:
    if entry.platform == "douyin" and entry.status == "partial" and not (entry.title or entry.description or entry.thumbnail_url or entry.thumbnail_path):
        return False
    age = time.time() - float(entry.updated_at or entry.created_at or 0)
    ttl = FAILED_TTL_SECONDS if entry.status == "error" else METADATA_TTL_SECONDS
    if age > ttl:
        return False
    return not (depth in {"preview", "full"} and entry.depth not in {"preview", "full"})


def _inspect_metadata(entry: URLContextEntry) -> None:
    if not entry.url.startswith(("http://", "https://")):
        entry.status = "error"
        entry.failure_reason = "invalid_url"
        return
    guessed_mime = mimetypes.guess_type(urlparse(entry.url).path)[0] or ""
    if guessed_mime.startswith("image/"):
        entry.content_type = "image"
        entry.status = "ok"
        entry.thumbnail_url = entry.url
        entry.failure_reason = ""
        return

    result = _http_get(entry.url, timeout=6, max_bytes=2 * 1024 * 1024, mobile=entry.platform == "douyin")
    entry.canonical_url = result.get("url") or entry.url
    content_type = result.get("content_type", "")
    if content_type.startswith("image/"):
        entry.content_type = "image"
        entry.thumbnail_url = entry.canonical_url
        entry.status = "ok"
        entry.failure_reason = ""
        return
    body = result.get("text", "")
    if not body:
        entry.content_type = _content_type_for_platform(entry.platform, content_type)
        entry.status = "partial" if entry.platform in VIDEO_SOCIAL_PLATFORMS else "error"
        entry.failure_reason = _classify_failure(result.get("error", "empty_response"))
        return
    metadata = parse_html_metadata(body)
    if entry.platform == "douyin":
        metadata.update({key: value for key, value in parse_douyin_metadata(body).items() if value})
    entry.title = metadata.get("title", "")
    entry.description = metadata.get("description", "")
    entry.thumbnail_url = metadata.get("image", "")
    entry.transcript_summary = metadata.get("extra", "")
    entry.content_type = _content_type_for_platform(entry.platform, content_type)
    entry.status = "ok" if (entry.title or entry.description or entry.thumbnail_url) else "partial"
    if entry.status == "ok":
        entry.failure_reason = ""
    if entry.status == "partial":
        entry.failure_reason = "metadata_sparse"


def _inspect_preview(entry: URLContextEntry) -> None:
    if entry.thumbnail_path and os.path.exists(entry.thumbnail_path):
        _maybe_analyze_preview_image(entry)
        entry.status = "ok"
        entry.failure_reason = ""
        return
    if entry.thumbnail_url and not entry.thumbnail_path:
        entry.thumbnail_path = _download_preview_image(entry.thumbnail_url, entry.url)
        if entry.thumbnail_path:
            _maybe_analyze_preview_image(entry)
            entry.status = "ok"
            entry.failure_reason = ""
            return
    if entry.content_type == "image" and not entry.thumbnail_path:
        entry.thumbnail_path = _download_preview_image(entry.url, entry.url)
        if entry.thumbnail_path:
            _maybe_analyze_preview_image(entry)
            entry.status = "ok"
            entry.failure_reason = ""
            return
    if entry.platform in VIDEO_SOCIAL_PLATFORMS:
        _try_ytdlp_metadata(entry)
    if not (entry.thumbnail_path or entry.screenshot_path) and entry.platform not in {"douyin", "instagram"}:
        _try_playwright_screenshot(entry)
    if entry.platform in {"douyin", "instagram"} and not (entry.thumbnail_path or entry.screenshot_path or entry.title):
        entry.status = "partial"
        entry.failure_reason = entry.failure_reason or "platform_restricted_or_login_required"


def parse_html_metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    head = text[:300000]
    title_match = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        result["title"] = _clean_html(title_match.group(1))
    for match in re.finditer(r"<meta\s+([^>]+)>", head, flags=re.IGNORECASE | re.DOTALL):
        attrs = _parse_attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").casefold()
        content = attrs.get("content") or ""
        if not key or not content:
            continue
        if key in {"og:title", "twitter:title"}:
            result["title"] = _clean_html(content)
        elif key in {"description", "og:description", "twitter:description"}:
            result["description"] = _clean_html(content)
        elif key in {"og:image", "twitter:image", "twitter:image:src"}:
            result["image"] = html.unescape(content.strip())
        elif key in {"og:url"}:
            result["canonical_url"] = html.unescape(content.strip())
    return result


def parse_douyin_metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    body = text or ""
    image_match = re.search(r'<img\s+[^>]*src=(["\'])(.*?)\1', body, flags=re.IGNORECASE | re.DOTALL)
    if image_match:
        result["image"] = html.unescape(image_match.group(2).strip())

    desc_match = re.search(r'"desc"\s*:\s*"((?:\\.|[^"\\])*)"', body)
    nickname_match = re.search(r'"nickname"\s*:\s*"((?:\\.|[^"\\])*)"', body)
    aweme_match = re.search(r'"aweme_id"\s*:\s*"([^"]+)"', body)
    digg_match = re.search(r'"digg_count"\s*:\s*(\d+)', body)
    create_match = re.search(r'"create_time"\s*:\s*(\d+)', body)

    desc = _json_string_value(desc_match.group(1)) if desc_match else ""
    nickname = _json_string_value(nickname_match.group(1)) if nickname_match else ""
    if desc:
        result["title"] = desc[:120]
        result["description"] = desc
    if nickname:
        result["author"] = nickname

    extras = []
    if nickname:
        extras.append(f"author={nickname}")
    if aweme_match:
        extras.append(f"video_id={aweme_match.group(1)}")
    if digg_match:
        extras.append(f"likes={digg_match.group(1)}")
    if create_match:
        extras.append(f"create_time={create_match.group(1)}")
    if extras:
        result["extra"] = " | ".join(extras)
    return result


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, _quote, value in re.findall(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", raw, flags=re.DOTALL):
        attrs[key.casefold()] = value
    return attrs


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _json_string_value(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return html.unescape(value.replace("\\/", "/").replace("\\n", "\n")).strip()


def _http_get(url: str, timeout: int = 6, max_bytes: int = 2 * 1024 * 1024, mobile: bool = False) -> dict[str, str]:
    try:
        headers = {
            "User-Agent": MOBILE_USER_AGENT if mobile else USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as response:
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            data = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) >= max_bytes:
                    break
            text = ""
            if content_type.startswith("text/") or "html" in content_type or "json" in content_type:
                text = bytes(data).decode(response.encoding or "utf-8", errors="replace")
            error = "" if response.ok else f"http_{response.status_code}: {bytes(data[:600]).decode(response.encoding or 'utf-8', errors='replace')}"
            return {"status_code": str(response.status_code), "url": response.url, "content_type": content_type, "text": text, "error": error}
    except Exception as exc:
        return {"status_code": "", "url": url, "content_type": "", "text": "", "error": str(exc)}


def _content_type_for_platform(platform: str, content_type: str) -> str:
    if platform in {"youtube", "bilibili", "douyin", "tiktok", "instagram"}:
        return "video"
    if platform == "x":
        return "post"
    if "html" in content_type or content_type.startswith("text/"):
        return "webpage"
    return "unknown"


def _download_preview_image(url: str, source_url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    os.makedirs(URL_PREVIEW_DIR, exist_ok=True)
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    filename = f"url_{url_cache_key(source_url)}{ext}"
    path = os.path.join(URL_PREVIEW_DIR, filename)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        with requests.get(url, stream=True, timeout=8, headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            total = 0
            with open(path, "wb") as file:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise ValueError("preview image exceeds 8MB")
                    file.write(chunk)
        return path
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return ""


def _try_ytdlp_metadata(entry: URLContextEntry) -> None:
    command_options = [
        ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download", entry.url],
        [sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist", "--skip-download", entry.url],
    ]
    for command in command_options:
        try:
            result = subprocess.run(command, capture_output=True, timeout=12, text=True, encoding="utf-8", errors="replace")
        except Exception:
            continue
        if result.returncode != 0 or not result.stdout.strip():
            entry.failure_reason = _classify_failure(result.stderr or result.stdout)
            continue
        try:
            payload = json.loads(result.stdout.splitlines()[0])
        except Exception:
            entry.failure_reason = "extractor_bad_json"
            continue
        entry.title = entry.title or str(payload.get("title") or "")[:500]
        entry.description = entry.description or str(payload.get("description") or "")[:900]
        entry.thumbnail_url = entry.thumbnail_url or str(payload.get("thumbnail") or "")
        duration = payload.get("duration")
        uploader = payload.get("uploader") or payload.get("channel")
        extras = []
        if uploader:
            extras.append(f"uploader={uploader}")
        if duration:
            extras.append(f"duration={duration}s")
        if extras:
            entry.transcript_summary = " | ".join(extras)
        if entry.thumbnail_url and not entry.thumbnail_path:
            entry.thumbnail_path = _download_preview_image(entry.thumbnail_url, entry.url)
        if entry.thumbnail_path:
            _maybe_analyze_preview_image(entry)
        entry.status = "ok"
        entry.failure_reason = ""
        return


def _maybe_analyze_preview_image(entry: URLContextEntry) -> None:
    if entry.vision_summary or not entry.thumbnail_path or not os.path.exists(entry.thumbnail_path):
        return
    api_key = _load_api_key()
    if not api_key:
        return
    try:
        if os.path.getsize(entry.thumbnail_path) > 8 * 1024 * 1024:
            return
        import base64

        import httpx
        from openai import OpenAI

        mime = mimetypes.guess_type(entry.thumbnail_path)[0] or "image/jpeg"
        with open(entry.thumbnail_path, "rb") as file:
            image_url = f"data:{mime};base64,{base64.b64encode(file.read()).decode('ascii')}"
        model_candidates = [
            os.getenv("VISION_MODEL", "").strip(),
            "Qwen/Qwen3-VL-8B-Instruct",
            "Qwen/Qwen3-VL-32B-Instruct",
            "zai-org/GLM-4.5V",
        ]
        model_candidates = [model for index, model in enumerate(model_candidates) if model and model not in model_candidates[:index]]
        prompt = "用中文简短描述这张视频封面/预览帧里能看到什么。只说画面，不要猜完整视频剧情。"
        for model in model_candidates:
            try:
                with httpx.Client(timeout=45.0) as http_client:
                    client = OpenAI(api_key=api_key, base_url="https://api.siliconflow.cn/v1", http_client=http_client)
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
                        max_tokens=240,
                    )
                    entry.vision_summary = (response.choices[0].message.content or "").strip()[:500]
                return
            except Exception:
                continue
    except Exception:
        return


def _load_api_key() -> str:
    if os.getenv("SILICONFLOW_API_KEY"):
        return os.getenv("SILICONFLOW_API_KEY", "")
    env_path = os.path.join(ROOT_DIR, ".env")
    try:
        with open(env_path, encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "SILICONFLOW_API_KEY":
                    return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _try_playwright_screenshot(entry: URLContextEntry) -> None:
    os.makedirs(URL_PREVIEW_DIR, exist_ok=True)
    path = os.path.join(URL_PREVIEW_DIR, f"url_{url_cache_key(entry.url)}_page.png")
    script = (
        "import asyncio, sys\n"
        "from playwright.async_api import async_playwright\n"
        "async def main():\n"
        "    async with async_playwright() as p:\n"
        "        try:\n"
        "            browser = await p.chromium.launch(channel='chrome', headless=True)\n"
        "        except Exception:\n"
        "            browser = await p.chromium.launch(headless=True)\n"
        "        page = await browser.new_page(viewport={'width': 1280, 'height': 900})\n"
        "        await page.goto(sys.argv[1], wait_until='domcontentloaded', timeout=9000)\n"
        "        await page.screenshot(path=sys.argv[2], full_page=False)\n"
        "        await browser.close()\n"
        "asyncio.run(main())\n"
    )
    try:
        result = subprocess.run([sys.executable, "-c", script, entry.url, path], capture_output=True, timeout=12, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            entry.screenshot_path = path
            entry.status = "ok"
        elif not entry.failure_reason:
            entry.failure_reason = _classify_failure(result.stderr or result.stdout or "screenshot_failed")
    except Exception as exc:
        if not entry.failure_reason:
            entry.failure_reason = _classify_failure(str(exc))


def _classify_failure(message: str) -> str:
    text = html.unescape(message or "").casefold()
    if any(marker in text for marker in ["bad network reputation", "anonymous queries", "as9009", "blocked from performing anonymous"]):
        return "reader_service_blocked"
    if any(marker in text for marker in ["login", "sign in", "authenticate", "authentication", "401", "40103", "403", "forbidden", "private"]):
        return "login_or_permission_required"
    if any(marker in text for marker in ["region", "country", "unavailable", "451"]):
        return "region_or_platform_restricted"
    if any(marker in text for marker in ["timeout", "timed out"]):
        return "timeout"
    if any(marker in text for marker in ["unsupported url", "extractor", "no video"]):
        return "extractor_failed"
    if any(marker in text for marker in ["connection", "network", "resolve", "ssl"]):
        return "network_error"
    if not text:
        return "empty_response"
    return "preview_unavailable"


DEFAULT_URL_CONTEXT_CACHE = URLContextCache()
