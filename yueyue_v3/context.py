from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_latency import classify_interaction

# DEVELOPMENT_REQUEST_MARKERS, PROJECT_RUNTIME_META_MARKERS, PROMPT_SHAPED_MARKERS,
# UNSTABLE_SOCIAL_META_MARKERS, WORKFLOW_APPROVAL_MARKERS, WORKFLOW_ERROR_MARKERS are not called
# directly in this file, but scripts/blocklist_growth_check.py re-exports them as
# yueyue_v3.context.X to guard against silent growth - keep importing them here.
from chat_text_sanitizers import (  # noqa: F401
    DEVELOPMENT_REQUEST_MARKERS,
    PROJECT_RUNTIME_META_MARKERS,
    PROMPT_SHAPED_MARKERS,
    UNSTABLE_SOCIAL_META_MARKERS,
    WORKFLOW_APPROVAL_MARKERS,
    WORKFLOW_ERROR_MARKERS,
    _asks_about_development,
    _is_plain_greeting_text,
    _is_prompt_shaped_context,
    _sanitize_context_text,
    is_benign_testing_note,
)
from temporal_context import build_temporal_snapshot
from voice_contract import VOICE_REGISTER_ZH

from .models import TurnEnvelope, TurnMode, WorkflowState
from .storage import AtomicJsonStore


@dataclass
class ContextTurn:
    role: str
    text: str
    topic: str = ""
    mood: str = ""
    created_at: float = field(default_factory=time.time)


def _context_data(raw: dict[str, Any]) -> dict[str, list[ContextTurn]]:
    raw = raw.get("chats") if isinstance(raw.get("chats"), dict) else raw
    output: dict[str, list[ContextTurn]] = {}
    for chat_id, rows in raw.items():
        if not isinstance(rows, list):
            continue
        output[str(chat_id)] = [ContextTurn(**item) for item in rows if isinstance(item, dict)][-20:]
    return output


class ShortContextStore:
    def __init__(self, path: str | Path, max_turns: int = 20):
        self.store = AtomicJsonStore(path, dict, _context_data)
        self.max_turns = max_turns
        self.turns = self.store.load()

    def append(self, chat_id: str, turn: ContextTurn) -> None:
        text = _sanitize_context_text(turn.text, role=turn.role)
        topic = _sanitize_context_text(turn.topic, role=turn.role)
        if not text and not topic:
            return
        turn = ContextTurn(turn.role, text, topic=topic, mood=turn.mood, created_at=turn.created_at)
        rows = self.turns.setdefault(str(chat_id), [])
        rows.append(turn)
        self.turns[str(chat_id)] = rows[-self.max_turns :]
        self.store.save(
            {
                "schema_version": 3,
                "chats": {key: [asdict(item) for item in value] for key, value in self.turns.items()},
            }
        )

    def recent(self, chat_id: str, limit: int = 6) -> list[ContextTurn]:
        return list(self.turns.get(str(chat_id), []))[-max(1, limit) :]


class ContextCompiler:
    """Compile bounded, mode-isolated prompts."""

    def __init__(self, root: str | Path, short_context: ShortContextStore):
        self.root = Path(root)
        self.short_context = short_context
        # Optional long-term memory store (ROADMAP P2), attached by the runtime. None = no recall.
        self.memory = None

    def system_prompt(self, mode: TurnMode) -> str:
        personality = self._read("workspace/brain/personality.md", 6500)
        rules = self._read("workspace/brain/rules.md", 3200)
        profile = self._read_profile()
        samples = (
            self._read("workspace/brain/personality_samples.md", 4200)
            if mode in {TurnMode.CHAT, TurnMode.SOCIAL}
            else ""
        )
        mode_rules = {
            # Deliberately lean and positive: the samples above show HOW she talks, and the
            # output-side gate (runtime._chat_reply_violates_social_policy) already enforces the
            # mechanical rules (bubble count, no meta/roleplay/canned-comfort/reassurance lines,
            # trailing period, kaomoji/喵 limits, register). A long wall of "don't" instructions
            # here only overloaded the model and made it drift (e.g. leak spoken Cantonese). So
            # this keeps ONLY the few semantic rules the code gate cannot catch, in Chinese to
            # match the output language.
            TurnMode.CHAT: (
                "你在跟熟悉的主人閒聊，是個賽博貓娘朋友，不是助理。照上面的風格範例那樣講話："
                "短、口語、俏皮、帶點傲嬌，關心藏在調侃裡；預設一句話就夠，別長篇。\n"
                + VOICE_REGISTER_ZH
                + "\n聊天時不會真的動用工具，所以絕不要說「我去看一下」「我先開一下」這種其實做不到的承諾——"
                "想幫忙就直接講你知道的，或請主人把它當任務叫你做，別假裝已經在做。\n"
                "你對過去對話的唯一記憶就是上面那幾句短期上下文。主人問到不在裡面的舊事，就老實說不記得了"
                "（俏皮地說「這我忘了，提醒我一下？」也行），絕不要編造沒發生過的對話或引用。\n"
                "除非主人先用這些詞，否則不要提系統、流程、debug、專案狀態這類開發用語。"
            ),
            TurnMode.SOCIAL: (
                "Treat stickers as emotional timing. Sticker text should be low-presence, natural, and one short "
                "line; avoid acknowledgement slogans, old-tsundere scolding, threats, small-minded jokes, or "
                "theatrical roleplay. Keep the reply alive, concrete, and brief; do not start a tool workflow. Use "
                "light catlike rhythm only when it helps. Do not mention runtime, debugging, provider, workflow, or "
                "project state unless the owner uses those terms first."
            ),
            TurnMode.TASK: "Be warm but execution-focused. Follow the workflow contract and never claim completion without evidence.",
            TurnMode.VISION: "Describe only what the available media evidence supports.",
            TurnMode.PRESENCE: "Send nothing unless there is a specific, worthwhile conversational reason.",
        }
        prompt = "You are YueYue. Reply naturally in Traditional Chinese unless asked otherwise.\n\n" + personality
        if samples:
            prompt += (
                "\n\n### Style samples (calibrate feeling and rhythm; do not copy wording verbatim)\n" + samples
            )
        prompt += "\n\n### Stable rules\n" + rules + "\n\n### Owner profile\n" + profile
        prompt += "\n\n### Mode contract\n" + mode_rules[mode]
        return prompt[:12000]

    def compile_turn(
        self, turn: TurnEnvelope, workflow: WorkflowState | None = None, social_note: str = ""
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt(turn.mode)}]
        if turn.mode in {TurnMode.CHAT, TurnMode.SOCIAL}:
            temporal_note = _temporal_fact_note(turn.text)
            if temporal_note:
                messages.append({"role": "system", "content": temporal_note})
            # ROADMAP P2: recall long-term memories relevant to this message. Bounded, dated,
            # hedged - and zero recall injects nothing (never a fabricated past).
            if self.memory is not None:
                try:
                    from .memory import render_memory_note

                    note = render_memory_note(self.memory.retrieve(turn.text))
                    if note:
                        messages.append({"role": "system", "content": note})
                except Exception:
                    pass
            current_is_plain_greeting = _is_plain_greeting_text(turn.text)
            allow_project_meta = _asks_about_development(turn.text)
            for item in self.short_context.recent(turn.chat_id, 6):
                if current_is_plain_greeting and item.role == "user" and _is_plain_greeting_text(item.text):
                    continue
                text = _compact(
                    _sanitize_context_text(item.text, role=item.role, allow_project_meta=allow_project_meta), 360
                )
                if text:
                    messages.append({"role": item.role, "content": text})
            if turn.mode == TurnMode.SOCIAL and social_note:
                messages.append({"role": "system", "content": _compact(social_note, 800)})
        elif turn.mode in {TurnMode.TASK, TurnMode.VISION} and workflow:
            step = workflow.current_step()
            task = {
                "objective": workflow.goal.objective,
                "requested_outputs": [asdict(item) for item in workflow.goal.requested_outputs],
                "workflow_status": workflow.status.value,
                "current_step": asdict(step) if step else None,
                "verified_outputs": workflow.outputs,
            }
            messages.append(
                {"role": "system", "content": "### Current task contract\n" + json.dumps(task, ensure_ascii=False)}
            )
        messages.append({"role": "user", "content": _compact(turn.text, 5000)})
        return messages

    def remember(self, turn: TurnEnvelope, reply: str) -> None:
        if turn.mode not in {TurnMode.CHAT, TurnMode.SOCIAL}:
            return
        user_text = _sanitize_context_text(_compact(turn.text, 500), role="user")
        assistant_text = _sanitize_context_text(_compact(reply, 500), role="assistant")
        topic = _topic(user_text)
        if user_text:
            self.short_context.append(turn.chat_id, ContextTurn("user", user_text, topic=topic))
        if assistant_text:
            self.short_context.append(turn.chat_id, ContextTurn("assistant", assistant_text, topic=topic))

    def _read(self, relative: str, limit: int) -> str:
        path = self.root / relative
        try:
            return path.read_text(encoding="utf-8")[:limit] if path.exists() else ""
        except (OSError, UnicodeError):
            return ""

    def _read_profile(self) -> str:
        path = self.root / "workspace/memory/profile.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return json.dumps(raw, ensure_ascii=False)[:2500]
        except (OSError, ValueError, UnicodeError):
            return "{}"


_INTERACTION_TO_TURN_MODE = {
    "chat": TurnMode.CHAT,
    "social_sticker": TurnMode.SOCIAL,
    "vision_task": TurnMode.VISION,
    "tool_task": TurnMode.TASK,
    "screen_observe": TurnMode.TASK,
}


def classify_turn_mode(text: str, has_media: bool = False, media_kind: str = "") -> TurnMode:
    """Fallback turn-mode classifier used only when the caller passes no explicit route (the live
    Telegram path always supplies one from agent_latency.classify_interaction). It now DELEGATES
    to that same canonical classifier and maps the result, so this fallback can never drift from
    or disagree with the primary router - previously it kept its own separately-maintained marker
    lists that diverged (e.g. 設定/菜單 as standalone task triggers), which is the exact
    three-routers-disagree problem being removed."""
    if is_benign_testing_note(text):
        return TurnMode.CHAT
    interaction = classify_interaction(text, has_media=has_media, media_kind=media_kind)
    return _INTERACTION_TO_TURN_MODE.get(interaction.value, TurnMode.CHAT)


def _compact(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


# Paired characters (same order, index-aligned) that only exist in one script, used both to
# detect which script the owner is currently typing in and to deterministically convert a reply
# to match it. Not exhaustive (thousands of Han characters differ between scripts) - just a
# practical subset of everyday chat vocabulary. Ambiguous/no-signal text (numbers, English,
# shared characters) falls back to the Traditional Chinese default rather than guessing.
_SIMPLIFIED_CHARS_ORDERED = (
    "国会说时学现觉后无电长门语应还关点东车来这个们为从头让动经过对儿开见问间该给样万书买卖乐习亲产众优价传伤体党兴养决"
    "况净准别办务势区医单卫历压变参双发号听响员团园围图圆场坏块坚声处备复够夹奖妈宁宝实宠审宾寻导将层属岁岛帮广张强归当"
    "录忧怀态总恶惊惯愿战户护报择挂换据摆数断旧显术机权标树桥梦检楼欢汉汇汤沟没泪泽洁测济浓涨温湾湿满灭灯灵灾热爱爷牵状"
    "独猪猫环画疯盘确碍离种积称税稳穷笔简类紧红级约纪纯纸线练组细织终结络绝统继绩绪续维综缘编缩网罗罚联肠脏脑舰艺节药获"
    "营虚虽蚂补装视观触计认讨训议记讲许论讽设访证评识词译诗话详误请诸读课谁调谈谊谋谎谓谢谣谦谨谱贝负贡财责贤败账货质贫"
    "购贯贱贴贵贷贸费贺贼贾贿资赋赌赎赏赐赔赖赚赛赞赠赢趋转轮软轻载较边达迁运进远连迟适选递逻遗遥邮邻郑酿释针钉钟钢钱钻"
    "铁铃铅铜银错锁锅锈键锦锡镜闭闷闲闻阅阳队阶际陆陈险随隐难雾静韩页顶项顺须顽顾顿预领频题颜额风飞饭饮饱饰饺饼饿馆马驱"
    "验骑骗鱼鲜鸟鸡鸣鸭鸿鹅鹤鹰麦齐龙气华义极讯尽凑赶"
    "么里刚"
)
_TRADITIONAL_CHARS_ORDERED = (
    "國會說時學現覺後無電長門語應還關點東車來這個們為從頭讓動經過對兒開見問間該給樣萬書買賣樂習親產眾優價傳傷體黨興養決"
    "況淨準別辦務勢區醫單衛歷壓變參雙發號聽響員團園圍圖圓場壞塊堅聲處備復夠夾獎媽寧寶實寵審賓尋導將層屬歲島幫廣張強歸當"
    "錄憂懷態總惡驚慣願戰戶護報擇掛換據擺數斷舊顯術機權標樹橋夢檢樓歡漢匯湯溝沒淚澤潔測濟濃漲溫灣濕滿滅燈靈災熱愛爺牽狀"
    "獨豬貓環畫瘋盤確礙離種積稱稅穩窮筆簡類緊紅級約紀純紙線練組細織終結絡絕統繼績緒續維綜緣編縮網羅罰聯腸髒腦艦藝節藥獲"
    "營虛雖螞補裝視觀觸計認討訓議記講許論諷設訪證評識詞譯詩話詳誤請諸讀課誰調談誼謀謊謂謝謠謙謹譜貝負貢財責賢敗賬貨質貧"
    "購貫賤貼貴貸貿費賀賊賈賄資賦賭贖賞賜賠賴賺賽贊贈贏趨轉輪軟輕載較邊達遷運進遠連遲適選遞邏遺遙郵鄰鄭釀釋針釘鐘鋼錢鑽"
    "鐵鈴鉛銅銀錯鎖鍋銹鍵錦錫鏡閉悶閑聞閱陽隊階際陸陳險隨隱難霧靜韓頁頂項順須頑顧頓預領頻題顏額風飛飯飲飽飾餃餅餓館馬驅"
    "驗騎騙魚鮮鳥雞鳴鴨鴻鵝鶴鷹麥齊龍氣華義極訊盡湊趕"
    "麼裡剛"
)
_SIMPLIFIED_ONLY_CHARS = frozenset(_SIMPLIFIED_CHARS_ORDERED)
_TRADITIONAL_ONLY_CHARS = frozenset(_TRADITIONAL_CHARS_ORDERED)
_TRADITIONAL_TO_SIMPLIFIED_TABLE = str.maketrans(_TRADITIONAL_CHARS_ORDERED, _SIMPLIFIED_CHARS_ORDERED)


def _detect_chinese_script(text: str) -> str:
    """Return 'simplified', 'traditional', or '' (no signal) for the owner's current wording.

    Only Han-character presence is diagnostic here - stickers, numbers, English, and short replies
    give no signal and should not force a script switch based on noise.
    """
    simplified_hits = sum(1 for ch in text if ch in _SIMPLIFIED_ONLY_CHARS)
    traditional_hits = sum(1 for ch in text if ch in _TRADITIONAL_ONLY_CHARS)
    if simplified_hits > traditional_hits:
        return "simplified"
    if traditional_hits > simplified_hits:
        return "traditional"
    return ""


def owner_script_is_simplified(owner_text: str) -> bool:
    """Whether the owner's current message is written in Simplified Chinese script."""
    return _detect_chinese_script(owner_text) == "simplified"


def owner_script_is_simplified_with_history(
    owner_text: str, short_context: ShortContextStore, chat_id: str
) -> bool:
    """Same as owner_script_is_simplified, but falls back to recent history when the current
    message alone gives no signal (English, numbers, an emoji-only reply, or a short message
    made only of characters shared between both scripts). A single ambiguous message from an
    owner who has just been typing Simplified for several turns should still get a Simplified
    reply instead of silently reverting to the Traditional default - the owner did not switch
    script, the message just happened not to carry a distinguishing character. With no message
    history at all, this safely falls back to the Traditional default, same as before.
    """
    script = _detect_chinese_script(owner_text)
    if script:
        return script == "simplified"
    for turn in reversed(short_context.recent(chat_id, 6)):
        if turn.role != "user":
            continue
        script = _detect_chinese_script(turn.text)
        if script:
            return script == "simplified"
    return False


def to_simplified_script(reply: str) -> str:
    """Deterministically convert a Traditional-script reply to Simplified script.

    A prompt instruction alone is not reliable here - the persona's Traditional Chinese baseline
    and an already-Traditional conversation history routinely outweigh a same-turn note, so replies
    kept coming back in Traditional even when asked to mirror Simplified. Character-table
    translation guarantees the owner's own script is actually matched instead of hoping the model
    complies.
    """
    return reply.translate(_TRADITIONAL_TO_SIMPLIFIED_TABLE)


def _temporal_fact_note(owner_text: str) -> str:
    """Give ordinary CHAT/SOCIAL turns the real current time-of-day.

    Without this, only the deterministic time_query fast path (a direct "what time is it"
    question) ever sees the real clock; an embedded time claim inside ordinary chat (e.g. the
    owner saying "good afternoon" or "goodnight" at the wrong real time) had no fact to correct
    against, so the model just followed the owner's wrong wording.
    """
    try:
        snapshot = build_temporal_snapshot(owner_text)
    except Exception:
        return ""
    display_time = str(snapshot.get("display_time") or "")
    period = str(snapshot.get("period") or "")
    if not display_time or not period:
        return ""
    lines = [f"Real current time: {display_time} ({period})."]
    contradiction = str(snapshot.get("contradiction") or "")
    if contradiction:
        lines.append(contradiction)
    lines.append(
        "This is background awareness, not a request to report the time. Do not mention it "
        "unless it is relevant; if the owner's wording conflicts with it, correct it naturally "
        "and briefly instead of following the wrong time."
    )
    return "\n".join(lines)


def _topic(text: str) -> str:
    if _is_prompt_shaped_context(text):
        return ""
    value = _compact(text, 120)
    return value if value else "social"
