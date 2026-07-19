"""Native tool-calling skill catalog (ROADMAP Phase 6, v2 — the Claude-style architecture).

The owner's bar: "要智能而且質量高，像 Claude/Codex 一樣" and "要知道什麼時候用，而不是只有
提醒了才曉得用". v1 used a keyword prefilter + separate router call — that IS the whack-a-mole
pattern. v2 does what Claude does natively: every chat turn, the chat model sees the FULL skill
catalog as function-calling tools and decides itself whether to call one, which, and with what
args — mid-conversation, no keywords. 「冷不冷呀」 can reach for weather; 「幫我記一下」 can
reach for notes; plain chatter calls nothing.

Handlers are deterministic pure Python (provider-agnostic, publishable). A handler returns a
factual note; the model weaves it into YueYue's own reply — nothing canned. Multi-turn games
(guess-the-number, 接龍, riddles) deliberately have NO handlers: the model plays them in
character, which is the intelligent path.

Adding a skill = one Skill entry (name / description-with-when / JSON-schema params / handler).
"""

from __future__ import annotations

import ast
import datetime as _dt
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from local_store import EXPENSES, NOTES, TODOS
from reminders import DEFAULT_REMINDER_STORE


@dataclass
class SkillContext:
    chat_id: str
    now: float


@dataclass
class SkillResult:
    note: str          # factual outcome; the persona layer phrases it - never shown raw
    ok: bool = True


@dataclass
class Skill:
    name: str
    description: str   # WHEN to use, written for the model - this is the routing intelligence
    parameters: dict
    handler: Callable[[dict, SkillContext], SkillResult]


class _ToolSpec:
    """Duck-types AgentTool for providers.chat (needs .name/.description/.parameters)."""

    def __init__(self, skill: Skill):
        self.name = skill.name
        self.description = skill.description
        self.parameters = skill.parameters


def _p(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _fmt_when(fire_at: float, now: float) -> str:
    return time.strftime("%m月%d日 %H:%M", time.localtime(max(fire_at, now)))


# ------------------------------------------------------------------ reminders

def _set_reminder(args: dict, ctx: SkillContext) -> SkillResult:
    text = str(args.get("what") or "").strip()
    try:
        delay = float(args.get("fire_in_seconds"))
    except (TypeError, ValueError):
        return SkillResult("時間沒解析出來，要再問主人一次幾點提醒", ok=False)
    if not text or delay < 0:
        return SkillResult("提醒內容或時間不完整，要再確認一次", ok=False)
    repeat = 0.0
    try:
        repeat = max(0.0, float(args.get("repeat_every_seconds") or 0))
    except (TypeError, ValueError):
        repeat = 0.0
    reminder = DEFAULT_REMINDER_STORE.add(ctx.chat_id, ctx.now + max(5.0, delay), text, repeat_every=repeat)
    when = _fmt_when(reminder.fire_at, ctx.now)
    if repeat > 0:
        return SkillResult(f"設好了週期提醒「{text}」，首次 {when}，之後每 {int(repeat / 60)} 分鐘/相應週期重複")
    return SkillResult(f"設好了提醒「{text}」，會在 {when} 準時提醒")


def _list_reminders(args: dict, ctx: SkillContext) -> SkillResult:
    pending = DEFAULT_REMINDER_STORE.pending(ctx.chat_id)
    if not pending:
        return SkillResult("目前沒有任何待提醒")
    lines = [f"「{r.text}」{_fmt_when(r.fire_at, ctx.now)}" + ("（週期）" if r.repeat_every else "") for r in pending[:8]]
    return SkillResult("待提醒：" + "；".join(lines))


def _cancel_reminders(args: dict, ctx: SkillContext) -> SkillResult:
    count = DEFAULT_REMINDER_STORE.cancel(ctx.chat_id)
    return SkillResult(f"取消了 {count} 個提醒" if count else "本來就沒有待提醒")


# ------------------------------------------------------------------ notes / todos / expenses

def _note_add(args: dict, ctx: SkillContext) -> SkillResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return SkillResult("備忘內容是空的", ok=False)
    NOTES.add({"text": text[:500]})
    return SkillResult(f"記下了備忘：「{text[:80]}」")


def _note_list(args: dict, ctx: SkillContext) -> SkillResult:
    query = str(args.get("query") or "").strip().casefold()
    items = NOTES.all()
    if query:
        items = [n for n in items if query in str(n.get("text", "")).casefold()]
    if not items:
        return SkillResult("沒有找到相關備忘" if query else "目前沒有備忘")
    lines = [f"{i + 1}. {n['text'][:60]}" for i, n in enumerate(items[-10:])]
    return SkillResult("備忘：" + "；".join(lines))


def _todo_add(args: dict, ctx: SkillContext) -> SkillResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return SkillResult("待辦內容是空的", ok=False)
    TODOS.add({"text": text[:300], "done": False})
    return SkillResult(f"加進待辦：「{text[:80]}」")


def _todo_list(args: dict, ctx: SkillContext) -> SkillResult:
    open_items = [t for t in TODOS.all() if not t.get("done")]
    if not open_items:
        return SkillResult("待辦清單是空的，全部做完了")
    lines = [f"{i + 1}. {t['text'][:60]}" for i, t in enumerate(open_items[:12])]
    return SkillResult("未完成待辦:" + "；".join(lines))


def _todo_done(args: dict, ctx: SkillContext) -> SkillResult:
    key = str(args.get("which") or "").strip()
    open_items = [t for t in TODOS.all() if not t.get("done")]
    target = None
    if key.isdigit() and 1 <= int(key) <= len(open_items):
        target = open_items[int(key) - 1]
    else:
        for item in open_items:
            if key and key.casefold() in str(item.get("text", "")).casefold():
                target = item
                break
    if not target:
        return SkillResult("沒找到那條待辦，要確認一下是哪一條", ok=False)
    TODOS.update(target["id"], done=True, done_at=ctx.now)
    return SkillResult(f"完成了待辦：「{target['text'][:60]}」")


def _expense_log(args: dict, ctx: SkillContext) -> SkillResult:
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        return SkillResult("金額沒聽清楚", ok=False)
    item = str(args.get("item") or "").strip() or "未註明"
    category = str(args.get("category") or "").strip() or "其他"
    EXPENSES.add({"amount": round(amount, 2), "item": item[:80], "category": category[:30]})
    return SkillResult(f"記帳：{item} {amount:.2f} 元（{category}）")


def _expense_summary(args: dict, ctx: SkillContext) -> SkillResult:
    period = str(args.get("period") or "month")
    now = _dt.datetime.fromtimestamp(ctx.now)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - _dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        period = "month"
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = [e for e in EXPENSES.all() if e.get("created_at", 0) >= start.timestamp()]
    if not rows:
        return SkillResult(f"這個{'天' if period == 'today' else '週' if period == 'week' else '月'}還沒有記帳記錄")
    total = sum(float(e.get("amount", 0)) for e in rows)
    by_cat: dict[str, float] = {}
    for e in rows:
        by_cat[e.get("category", "其他")] = by_cat.get(e.get("category", "其他"), 0) + float(e.get("amount", 0))
    cats = "、".join(f"{k} {v:.0f}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:5])
    label = {"today": "今天", "week": "本週", "month": "本月"}[period]
    return SkillResult(f"{label}共 {len(rows)} 筆、合計 {total:.2f} 元；分類：{cats}")


# ------------------------------------------------------------------ calc / convert / date / clock

_CALC_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                 ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load)
_CALC_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log, "sin": math.sin,
               "cos": math.cos, "tan": math.tan, "pi": math.pi, "e": math.e}


def _quick_calc(args: dict, ctx: SkillContext) -> SkillResult:
    expr = str(args.get("expression") or "").strip().replace("×", "*").replace("÷", "/").replace("^", "**")
    if not expr or len(expr) > 120:
        return SkillResult("算式沒看懂", ok=False)
    try:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _CALC_ALLOWED):
                raise ValueError(f"不支援的語法 {type(node).__name__}")
            if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCS):
                raise ValueError("不支援的函數")
            if isinstance(node, ast.Name) and node.id not in _CALC_FUNCS:
                raise ValueError("未知名稱")
        value = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, _CALC_FUNCS)  # noqa: S307
        text = f"{value:.6g}" if isinstance(value, float) else str(value)
        return SkillResult(f"{expr} = {text}")
    except Exception as exc:
        return SkillResult(f"這條算式算不了（{exc}）", ok=False)


_UNITS = {  # to-SI factors within a family
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "inch": 0.0254,
               "ft": 0.3048, "mile": 1609.344, "英里": 1609.344, "英尺": 0.3048, "英寸": 0.0254,
               "公里": 1000.0, "米": 1.0, "公分": 0.01, "厘米": 0.01, "毫米": 0.001},
    "weight": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "lb": 0.453592, "磅": 0.453592,
               "oz": 0.0283495, "斤": 0.5, "公斤": 1.0, "克": 0.001, "噸": 1000.0},
    "volume": {"ml": 0.001, "l": 1.0, "L": 1.0, "毫升": 0.001, "升": 1.0, "gal": 3.78541, "加侖": 3.78541},
}


def _unit_convert(args: dict, ctx: SkillContext) -> SkillResult:
    try:
        value = float(args.get("value"))
    except (TypeError, ValueError):
        return SkillResult("數值沒看懂", ok=False)
    src = str(args.get("from_unit") or "").strip()
    dst = str(args.get("to_unit") or "").strip()
    lowered_src, lowered_dst = src.casefold(), dst.casefold()
    if {lowered_src, lowered_dst} & {"c", "f", "°c", "°f", "攝氏", "華氏"}:
        if lowered_src in {"c", "°c", "攝氏"}:
            return SkillResult(f"{value:g}°C = {value * 9 / 5 + 32:.1f}°F")
        return SkillResult(f"{value:g}°F = {(value - 32) * 5 / 9:.1f}°C")
    for family in _UNITS.values():
        f_src = family.get(src) or family.get(lowered_src)
        f_dst = family.get(dst) or family.get(lowered_dst)
        if f_src and f_dst:
            return SkillResult(f"{value:g} {src} = {value * f_src / f_dst:.4g} {dst}")
    return SkillResult(f"不認識 {src}→{dst} 這組單位", ok=False)


def _date_calc(args: dict, ctx: SkillContext) -> SkillResult:
    today = _dt.date.fromtimestamp(ctx.now)
    target_raw = str(args.get("target_date") or "").strip()
    days_raw = args.get("days_from_now")
    weekdays = "一二三四五六日"
    if target_raw:
        try:
            target = _dt.date.fromisoformat(target_raw)
        except ValueError:
            return SkillResult("日期格式要 YYYY-MM-DD", ok=False)
        delta = (target - today).days
        wd = weekdays[target.weekday()]
        if delta > 0:
            return SkillResult(f"{target.isoformat()} 是星期{wd}，距離今天還有 {delta} 天")
        if delta < 0:
            return SkillResult(f"{target.isoformat()} 是星期{wd}，已經過去 {-delta} 天")
        return SkillResult(f"{target.isoformat()} 就是今天，星期{wd}")
    try:
        days = int(days_raw)
        target = today + _dt.timedelta(days=days)
        return SkillResult(f"{days:+d} 天後是 {target.isoformat()}（星期{weekdays[target.weekday()]}）")
    except (TypeError, ValueError):
        return SkillResult(f"今天是 {today.isoformat()}（星期{weekdays[today.weekday()]}）")


_CITY_TZ = {"紐約": "America/New_York", "纽约": "America/New_York", "洛杉磯": "America/Los_Angeles",
            "倫敦": "Europe/London", "伦敦": "Europe/London", "巴黎": "Europe/Paris", "東京": "Asia/Tokyo",
            "东京": "Asia/Tokyo", "首爾": "Asia/Seoul", "香港": "Asia/Hong_Kong", "台北": "Asia/Taipei",
            "北京": "Asia/Shanghai", "上海": "Asia/Shanghai", "新加坡": "Asia/Singapore",
            "悉尼": "Australia/Sydney", "雪梨": "Australia/Sydney", "柏林": "Europe/Berlin",
            "莫斯科": "Europe/Moscow", "杜拜": "Asia/Dubai", "迪拜": "Asia/Dubai"}


def _world_clock(args: dict, ctx: SkillContext) -> SkillResult:
    city = str(args.get("city") or "").strip()
    zone_name = _CITY_TZ.get(city)
    if not zone_name:
        return SkillResult(f"不認識城市「{city}」，支援：{'、'.join(sorted(set(_CITY_TZ)))[:80]}…", ok=False)
    try:
        from zoneinfo import ZoneInfo

        now = _dt.datetime.fromtimestamp(ctx.now, tz=ZoneInfo(zone_name))
        return SkillResult(f"{city}現在是 {now.strftime('%m月%d日 %H:%M')}（{'星期' + '一二三四五六日'[now.weekday()]}）")
    except Exception:
        return SkillResult("時區數據不可用", ok=False)


# ------------------------------------------------------------------ fun

def _dice_roll(args: dict, ctx: SkillContext) -> SkillResult:
    sides = max(2, min(1000, int(args.get("sides") or 6)))
    count = max(1, min(10, int(args.get("count") or 1)))
    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        return SkillResult(f"擲出 D{sides}：{rolls[0]} 點")
    return SkillResult(f"擲出 {count} 顆 D{sides}：{rolls}，合計 {sum(rolls)}")


def _coin_flip(args: dict, ctx: SkillContext) -> SkillResult:
    return SkillResult(f"拋硬幣結果：{random.choice(['正面', '反面'])}")


def _decide_pick(args: dict, ctx: SkillContext) -> SkillResult:
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if len(options) < 2:
        return SkillResult("至少要給兩個選項才好選", ok=False)
    return SkillResult(f"從 {len(options)} 個選項裡選中了：「{random.choice(options)}」")


_EAT_POOL = ["拉麵", "咖哩飯", "壽司", "火鍋", "麻辣燙", "披薩", "漢堡", "餃子", "牛肉麵", "炒飯",
             "沙拉輕食", "韓式炸雞", "石鍋拌飯", "泰式打拋豬", "義大利麵", "燒臘飯", "粥品", "湯麵"]


def _eat_decider(args: dict, ctx: SkillContext) -> SkillResult:
    picks = random.sample(_EAT_POOL, k=2)
    return SkillResult(f"抽到的答案：首選「{picks[0]}」，備選「{picks[1]}」")


def _daily_fortune(args: dict, ctx: SkillContext) -> SkillResult:
    # Deterministic per day so repeat asks the same day agree (feels like a real 籤).
    day_seed = int(_dt.date.fromtimestamp(ctx.now).strftime("%Y%m%d"))
    rng = random.Random(day_seed + hash(ctx.chat_id) % 1000)
    level = rng.choice(["大吉", "中吉", "小吉", "吉", "末吉", "凶(明天會更好)"])
    lucky = rng.choice(["紅色", "藍色", "白色", "紫色", "金色", "綠色"])
    number = rng.randint(1, 99)
    return SkillResult(f"今日運勢：{level}；幸運色 {lucky}；幸運數字 {number}")


def _rock_paper_scissors(args: dict, ctx: SkillContext) -> SkillResult:
    moves = {"石頭": 0, "剪刀": 1, "布": 2, "rock": 0, "scissors": 1, "paper": 2}
    owner_raw = str(args.get("owner_move") or "").strip()
    owner = moves.get(owner_raw) if owner_raw else None
    mine = random.randint(0, 2)
    names = ["石頭", "剪刀", "布"]
    if owner is None:
        return SkillResult(f"月月出了「{names[mine]}」（主人還沒出，等他出了再判勝負）")
    outcome = (owner - mine) % 3
    verdict = ["平手", "月月贏了", "主人贏了"][outcome]
    return SkillResult(f"主人出「{names[owner]}」，月月出「{names[mine]}」——{verdict}")


def _draw_lots(args: dict, ctx: SkillContext) -> SkillResult:
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if options:
        return SkillResult(f"抽中了：「{random.choice(options)}」")
    stick = random.choice(["上上籤", "上籤", "中籤", "中平籤", "下籤(逢凶化吉)"])
    return SkillResult(f"抽到一支{stick}")


# ------------------------------------------------------------------ time / tasks

# Set by the runtime at construction: a zero-arg callable returning the live task snapshot
# (active workflow objective/status + queued objectives). Kept as a hook so this module stays
# import-clean of the runtime (no circular dependency).
RUNTIME_INTROSPECT = None

_PERIODS = ((8, "清晨"), (11, "早上"), (13, "中午"), (18, "下午"), (19, "傍晚"), (23, "晚上"), (25, "深夜"))


def _current_time(args: dict, ctx: SkillContext) -> SkillResult:
    now = _dt.datetime.fromtimestamp(ctx.now)
    hour = now.hour if now.hour >= 5 else 24  # 0-4 點歸入深夜
    period = next(label for bound, label in _PERIODS if hour < bound)
    weekday = "一二三四五六日"[now.weekday()]
    return SkillResult(f"現在是 {now.strftime('%Y-%m-%d %H:%M')}，星期{weekday}，{period}")


def _list_tasks(args: dict, ctx: SkillContext) -> SkillResult:
    parts: list[str] = []
    snapshot = {}
    if callable(RUNTIME_INTROSPECT):
        try:
            snapshot = RUNTIME_INTROSPECT() or {}
        except Exception:
            snapshot = {}
    active = snapshot.get("active")
    if active:
        parts.append(f"正在做：「{active['objective'][:80]}」（{active['status']}）")
    queued = snapshot.get("queued") or []
    if queued:
        parts.append("排隊中：" + "；".join(f"「{q[:50]}」" for q in queued[:5]))
    pending = DEFAULT_REMINDER_STORE.pending(ctx.chat_id)
    if pending:
        parts.append(f"待提醒 {len(pending)} 個（最近：「{pending[0].text[:40]}」{_fmt_when(pending[0].fire_at, ctx.now)}）")
    open_todos = [t for t in TODOS.all() if not t.get("done")]
    if open_todos:
        parts.append(f"未完成待辦 {len(open_todos)} 條")
    if not parts:
        return SkillResult("目前沒有進行中或排隊的任務，也沒有待提醒")
    return SkillResult("；".join(parts))


# ------------------------------------------------------------------ web / weather (network, graceful)

def _web_search(args: dict, ctx: SkillContext) -> SkillResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return SkillResult("搜索詞是空的", ok=False)
    from core_tools import real_web_search

    result = real_web_search(query)
    if result.status != "ok":
        return SkillResult(f"搜索失敗：{result.error or result.message}", ok=False)
    return SkillResult(str(result.message)[:2400])


def _weather(args: dict, ctx: SkillContext) -> SkillResult:
    city = str(args.get("city") or "").strip() or "Hong Kong"
    try:
        import httpx

        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = client.get(f"https://wttr.in/{city}", params={"format": "j1"})
            data = response.json()
        current = data["current_condition"][0]
        today = data["weather"][0]
        desc = current.get("lang_zh", current.get("weatherDesc", [{}]))
        desc_text = desc[0].get("value", "") if isinstance(desc, list) and desc else ""
        return SkillResult(
            f"{city} 現在 {current.get('temp_C')}°C（體感 {current.get('FeelsLikeC')}°C），{desc_text}；"
            f"今天 {today.get('mintempC')}~{today.get('maxtempC')}°C，"
            f"降雨機率約 {today.get('hourly', [{}])[4].get('chanceofrain', '?')}%"
        )
    except Exception:
        return SkillResult("天氣服務暫時連不上", ok=False)


# ------------------------------------------------------------------ catalog

SKILLS: list[Skill] = [
    Skill("set_reminder",
          "主人要在某個時間被提醒做某事（鬧鐘/提醒/叫我/計時器/番茄鐘）。fire_in_seconds=距現在的秒數；"
          "週期性的（每天吃藥/每小時喝水）加 repeat_every_seconds。",
          _p({"what": {"type": "string", "description": "要提醒的內容"},
              "fire_in_seconds": {"type": "number", "description": "距離現在幾秒後首次觸發"},
              "repeat_every_seconds": {"type": "number", "description": "週期秒數，一次性提醒填 0"}},
             ["what", "fire_in_seconds"]), _set_reminder),
    Skill("list_reminders", "主人想知道自己有哪些待提醒/鬧鐘。", _p({}), _list_reminders),
    Skill("cancel_reminders", "主人想取消/清掉提醒或鬧鐘。", _p({}), _cancel_reminders),
    Skill("note_add", "主人要隨手記下一條備忘/靈感/事情（幫我記一下X）。",
          _p({"text": {"type": "string"}}, ["text"]), _note_add),
    Skill("note_list", "主人想看或搜自己的備忘（我記過什麼/找找關於X的備忘）。",
          _p({"query": {"type": "string", "description": "可選的搜索詞"}}), _note_list),
    Skill("todo_add", "主人要加一條待辦事項。", _p({"text": {"type": "string"}}, ["text"]), _todo_add),
    Skill("todo_list", "主人想看未完成的待辦。", _p({}), _todo_list),
    Skill("todo_done", "主人說某件待辦完成了。which 可以是序號或內容關鍵詞。",
          _p({"which": {"type": "string"}}, ["which"]), _todo_done),
    Skill("expense_log", "主人記一筆花費（午餐60/買了杯咖啡45塊）。",
          _p({"amount": {"type": "number"}, "item": {"type": "string"},
              "category": {"type": "string", "description": "餐飲/交通/購物/娛樂/其他"}}, ["amount"]), _expense_log),
    Skill("expense_summary", "主人想知道自己花了多少錢（今天/本週/本月）。",
          _p({"period": {"type": "string", "enum": ["today", "week", "month"]}}), _expense_summary),
    Skill("quick_calc", "需要精確計算的算術（別心算，尤其多位數/百分比/開根號）。",
          _p({"expression": {"type": "string", "description": "如 (128*46)/3 或 sqrt(2)"}}, ["expression"]), _quick_calc),
    Skill("unit_convert", "單位換算（長度/重量/容量/溫度，如 5 英里是幾公里、華氏轉攝氏）。",
          _p({"value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}},
             ["value", "from_unit", "to_unit"]), _unit_convert),
    Skill("date_calc", "日期計算：某天是星期幾/距離某日還有幾天/N天後是幾號。",
          _p({"target_date": {"type": "string", "description": "YYYY-MM-DD，可選"},
              "days_from_now": {"type": "integer", "description": "可選"}}), _date_calc),
    Skill("world_clock", "查其他城市/時區現在幾點。", _p({"city": {"type": "string"}}, ["city"]), _world_clock),
    Skill("dice_roll", "擲骰子（幾顆、幾面）。",
          _p({"sides": {"type": "integer"}, "count": {"type": "integer"}}), _dice_roll),
    Skill("coin_flip", "拋硬幣做決定。", _p({}), _coin_flip),
    Skill("decide_pick", "主人在幾個明確選項間猶豫，要月月幫忙隨機選一個。",
          _p({"options": {"type": "array", "items": {"type": "string"}}}, ["options"]), _decide_pick),
    Skill("eat_decider", "主人不知道吃什麼，幫他抽一個。", _p({}), _eat_decider),
    Skill("daily_fortune", "主人想看今日運勢/求籤問今天運氣。", _p({}), _daily_fortune),
    Skill("rock_paper_scissors", "跟主人玩猜拳。owner_move 是主人出的（沒出就先亮月月的）。",
          _p({"owner_move": {"type": "string", "enum": ["石頭", "剪刀", "布"]}}), _rock_paper_scissors),
    Skill("draw_lots", "抽籤：從候選裡抽一個，或抽運勢籤。",
          _p({"options": {"type": "array", "items": {"type": "string"}}}), _draw_lots),
    Skill("current_time",
          "主人問現在幾點/今天幾號星期幾/現在是早上還是晚上等任何跟當下時間有關的問題。",
          _p({}), _current_time),
    Skill("list_tasks",
          "主人想知道月月現在手上有什麼任務在做、排隊中的任務、待提醒或待辦的總覽。",
          _p({}), _list_tasks),
    Skill("web_search",
          "查網上的即時/事實資訊：新聞、價格、賽果、天氣以外的任何「現在/最新」問題，"
          "或月月不確定、答錯會誤導主人的事實。閒聊觀點不需要。",
          _p({"query": {"type": "string", "description": "精煉的搜索詞"}}, ["query"]), _web_search),
    Skill("weather", "查天氣（今天冷不冷/要不要帶傘/某城市天氣）。",
          _p({"city": {"type": "string", "description": "城市名，主人沒說就用他常在的城市"}}), _weather),
]

_SKILLS_BY_NAME = {s.name: s for s in SKILLS}


def skill_tools() -> list[_ToolSpec]:
    """The full catalog as function-calling tool specs for the chat model."""
    return [_ToolSpec(s) for s in SKILLS]


def execute_skill(name: str, args: dict, ctx: SkillContext) -> SkillResult:
    skill = _SKILLS_BY_NAME.get(str(name or ""))
    if not skill:
        return SkillResult(f"沒有叫 {name} 的技能", ok=False)
    try:
        return skill.handler(args if isinstance(args, dict) else {}, ctx)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(f"技能執行出錯：{exc}", ok=False)


_ = re  # keep re available for future handlers without an unused-import churn
