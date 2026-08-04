"""The owner's language setting, and the native-per-language persona it selects.

Stage A+B of the three-language work (2026-08-04). The behaviour these tests pin down:
  - an owner who never set a language sees EXACTLY the old behaviour (Traditional default plus
    the legacy detect-their-script-and-mirror-it path);
  - an owner who set 简体 gets Simplified persona material, a Simplified reply directive, and no
    detection or mirroring anywhere;
  - every safety gate keeps biting in Simplified, because the register/marker lists are matched
    against a Traditional shadow rather than duplicated per script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import owner_language
from owner_language import SIMPLIFIED, TRADITIONAL
from yueyue_v3.context import ContextCompiler, ShortContextStore, to_simplified_script, to_traditional_script
from yueyue_v3.models import TurnEnvelope, TurnMode

# --------------------------------------------------------------------- the setting itself

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("繁體", TRADITIONAL), ("繁体中文", TRADITIONAL), ("zh-Hant", TRADITIONAL),
        ("Traditional Chinese", TRADITIONAL), ("zh-tw", TRADITIONAL),
        ("簡體", SIMPLIFIED), ("简体中文", SIMPLIFIED), ("简", SIMPLIFIED),
        ("zh-Hans", SIMPLIFIED), ("simplified", SIMPLIFIED), ("zh-CN", SIMPLIFIED),
        ("英文", "en"), ("English", "en"),
        ("klingon", ""), ("", ""), ("   ", ""),
    ],
)
def test_language_names_normalise_to_codes(value: str, expected: str) -> None:
    assert owner_language.normalize_language(value) == expected


def test_unset_language_is_empty_not_the_default(tmp_path: Path) -> None:
    """"" and zh-Hant are different states: unset keeps the legacy mirroring path alive."""
    (tmp_path / "workspace" / "memory").mkdir(parents=True)
    (tmp_path / "workspace" / "memory" / "profile.json").write_text("{}", encoding="utf-8")
    assert owner_language.read_language(tmp_path) == ""
    assert owner_language.resolve_language(tmp_path) == TRADITIONAL


def test_write_language_round_trips_and_keeps_the_rest_of_the_profile(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "memory").mkdir(parents=True)
    profile = {"basic_info": {"name": "Xioshon"}, "preferences": ["喜歡自然"], "important_facts": ["a"]}
    (tmp_path / "workspace" / "memory" / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )

    assert owner_language.write_language("簡體", tmp_path) == SIMPLIFIED
    assert owner_language.read_language(tmp_path) == SIMPLIFIED
    saved = json.loads((tmp_path / "workspace" / "memory" / "profile.json").read_text(encoding="utf-8"))
    assert saved["basic_info"]["name"] == "Xioshon"
    assert saved["preferences"] == ["喜歡自然"]
    assert saved["important_facts"] == ["a"]

    assert owner_language.write_language("繁體", tmp_path) == TRADITIONAL
    assert owner_language.read_language(tmp_path) == TRADITIONAL


def test_unsupported_and_corrupt_values_degrade_to_unset(tmp_path: Path) -> None:
    """update_profile is a tool the MODEL can call, and profile.json can be corrupted. Neither may
    take chat down or silently switch the owner's language to something they never chose."""
    memory = tmp_path / "workspace" / "memory"
    memory.mkdir(parents=True)
    assert owner_language.write_language("klingon", tmp_path) == ""

    (memory / "profile.json").write_text('{"basic_info": {"language": "klingon"}}', encoding="utf-8")
    assert owner_language.read_language(tmp_path) == ""
    # English is a declared code with no persona material yet - not selectable until it has some.
    (memory / "profile.json").write_text('{"basic_info": {"language": "en"}}', encoding="utf-8")
    assert owner_language.read_language(tmp_path) == ""
    (memory / "profile.json").write_text("{ not json", encoding="utf-8")
    assert owner_language.read_language(tmp_path) == ""
    (memory / "profile.json").unlink()
    assert owner_language.read_language(tmp_path) == ""


# --------------------------------------------------------------------- script conversion

def test_traditional_repair_uses_the_projects_own_glyph_standard() -> None:
    """The 喫 fix (2026-07-23) declared zhconv's neutral over-conversions "a complete fixed set"
    after checking two characters by hand. Comparing the whole 428-pair table found seven more -
    为→爲 was reaching the owner in every repaired 因為/為什麼. The table now overrides zhconv, so
    each of these must come back in the glyph the rest of the codebase writes."""
    assert to_traditional_script("为什么这样") == "為什麼這樣"
    assert to_traditional_script("因为你认为") == "因為你認為"
    assert to_traditional_script("大众观众") == "大眾觀眾"
    assert to_traditional_script("潮湿的天气") == "潮濕的天氣"
    assert to_traditional_script("心里面") == "心裡面"
    # And the two script-NEUTRAL characters the table cannot speak for stay corrected.
    assert "喫" not in to_traditional_script("我想吃辣，你吃了吗")
    assert to_traditional_script("台湾") == "台灣"
    # Regressions from earlier rounds must stay fixed.
    assert to_traditional_script("那两件") == "那兩件"
    assert to_traditional_script("屏幕上的信息") == "屏幕上的信息"


def test_simplified_conversion_is_complete_not_the_old_428_char_table() -> None:
    """The old table-only version left 喫飯 and 臺灣 half-converted."""
    assert to_simplified_script("喫飯") == "吃饭"
    assert to_simplified_script("臺灣") == "台湾"
    assert to_simplified_script("為什麼這樣") == "为什么这样"
    assert to_simplified_script("我先處理一下") == "我先处理一下"
    assert to_simplified_script("hello 月月") == "hello 月月"


def test_simplified_conversion_resolves_the_shared_character() -> None:
    """A live 简体 reply came back 「月月在这里陪著你」 on 2026-08-04 - the mirror of the 喫
    complaint. 著/着 is one Traditional character covering two Simplified ones, so no glyph table
    can decide it; zh-cn resolves it by context, and must not over-apply to 著作/顯著."""
    assert to_simplified_script("月月在这里陪著你") == "月月在这里陪着你"
    assert to_simplified_script("就這樣靜靜待著也很好") == "就这样静静待着也很好"
    assert to_simplified_script("睡著了") == "睡着了"
    assert to_simplified_script("著作和顯著") == "著作和显著"


def test_simplified_target_needs_no_glyph_override() -> None:
    """to_simplified_script deliberately skips the positional glyph override so it does not undo
    zh-cn's vocabulary substitutions (it turned 軟體 into 软体 instead of 软件). That is only safe
    while zh-cn agrees with the project's own table everywhere - assert it, so a zhconv upgrade
    that breaks the assumption fails the gate instead of silently degrading the owner's replies."""
    from yueyue_v3.context import _SIMPLIFIED_CHARS_ORDERED, _TRADITIONAL_CHARS_ORDERED

    disagreements = [
        (traditional, simplified, to_simplified_script(traditional))
        for simplified, traditional in zip(
            _SIMPLIFIED_CHARS_ORDERED, _TRADITIONAL_CHARS_ORDERED, strict=True
        )
        if to_simplified_script(traditional) != simplified
    ]
    assert not disagreements, disagreements
    # And the Traditional direction still NEEDS its override - this is what asymmetry buys.
    assert to_simplified_script("軟件和網絡") == "软件和网络"


# --------------------------------------------------------------------- persona selection

def _compiler(root: Path) -> ContextCompiler:
    return ContextCompiler(root, ShortContextStore(root / "ctx.json"))


def _seed_brain(root: Path, language: str = "") -> None:
    (root / "workspace" / "brain").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "memory").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "brain" / "personality.md").write_text("月月是清新軟萌的貓娘", encoding="utf-8")
    (root / "workspace" / "brain" / "rules.md").write_text("只根據證據回報", encoding="utf-8")
    (root / "workspace" / "brain" / "personality_samples.md").write_text("主人回來啦～", encoding="utf-8")
    (root / "workspace" / "memory" / "profile.json").write_text("{}", encoding="utf-8")
    if language:
        owner_language.write_language(language, root)


def test_unset_language_prompt_is_unchanged_traditional(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    prompt = _compiler(tmp_path).system_prompt(TurnMode.CHAT)
    assert "用繁體中文回覆主人" in prompt
    assert "月月是清新軟萌的貓娘" in prompt
    assert "字感：香港書面繁體" in prompt


def test_simplified_owner_gets_simplified_material_and_directive(tmp_path: Path) -> None:
    _seed_brain(tmp_path, SIMPLIFIED)
    (tmp_path / "workspace" / "brain" / "personality.zh-Hans.md").write_text(
        "月月是清新软萌的猫娘", encoding="utf-8"
    )
    (tmp_path / "workspace" / "brain" / "personality_samples.zh-Hans.md").write_text(
        "主人回来啦～", encoding="utf-8"
    )
    prompt = _compiler(tmp_path).system_prompt(TurnMode.CHAT)
    assert "用简体中文回复主人" in prompt
    # The Simplified FILE is used - not a runtime conversion of the Traditional one.
    assert "月月是清新软萌的猫娘" in prompt
    assert "月月是清新軟萌的貓娘" not in prompt
    assert "主人回来啦～" in prompt
    # Register comes from the hand-written Simplified contract, never a converted copy of the
    # Traditional one (which would read 「字感：香港书面繁体」).
    assert "字感：简体中文书面语" in prompt
    assert "香港书面繁体" not in prompt
    # And the mode contract stops speaking Traditional at the Simplified persona.
    assert "閒聊" not in prompt


def test_missing_locale_material_falls_back_to_the_traditional_source(tmp_path: Path) -> None:
    """A language whose material has not been generated yet must degrade to today's persona, not
    to an empty one - an empty personality block is far worse than a Traditional one."""
    _seed_brain(tmp_path, SIMPLIFIED)
    prompt = _compiler(tmp_path).system_prompt(TurnMode.CHAT)
    assert "月月是清新軟萌的貓娘" in prompt
    assert "用简体中文回复主人" in prompt


def test_shipped_persona_locale_files_are_in_sync_with_their_source() -> None:
    """The generated Simplified persona must never drift from the Traditional source it came from.
    Same check start_yueyue.ps1 -SelfTest runs; duplicated into pytest so a bare `pytest` catches
    an edit to personality.md that was never regenerated."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_persona_locales.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=root,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_simplified_persona_has_no_traditional_leftovers() -> None:
    """workspace/brain is gitignored private material, so a fresh clone has none of it - skip
    rather than fail there, but assert hard whenever it IS present."""
    root = Path(__file__).resolve().parents[1]
    from yueyue_v3.context import _TRADITIONAL_ONLY_CHARS

    checked = 0
    for name in ("personality.zh-Hans.md", "personality_samples.zh-Hans.md"):
        path = root / "workspace" / "brain" / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        leftovers = sorted({ch for ch in text if ch in _TRADITIONAL_ONLY_CHARS})
        assert not leftovers, f"{name} still contains Traditional-only characters: {leftovers}"
        checked += 1
    if not checked:
        pytest.skip("no private persona material in this checkout")


def test_persona_glossary_localises_vocabulary_not_just_glyphs() -> None:
    """Script conversion alone turns 檔案/資料夾 into 档案/资料夹, which a mainland reader parses as
    "dossier"/"data folder" rather than "computer file". The generator's glossary fixes the terms
    the converters agreed on; the assertion is on the SHIPPED file, not on the glossary dict, so it
    fails if the glossary is ever bypassed."""
    root = Path(__file__).resolve().parents[1]
    path = root / "workspace" / "brain" / "personality_samples.zh-Hans.md"
    if not path.exists():
        pytest.skip("no private persona material in this checkout")
    from voice_contract import VOICE_REGISTER_ZH_HANS

    # The register block legitimately NAMES 档案/资料夹 as the words to avoid, so strip it before
    # asserting they are absent - otherwise the rule's own statement trips the rule.
    text = path.read_text(encoding="utf-8").replace(VOICE_REGISTER_ZH_HANS, "")
    assert "档案" not in text and "资料夹" not in text
    assert "文件夹" in text


def test_canned_literal_pools_do_not_need_per_language_copies() -> None:
    """Pins the decision NOT to duplicate the fallback pools per language (2026-08-04).

    Every canned line this project ships is Traditional source that a Simplified owner receives
    through the egress conversion. Measured across all of them, that conversion already produces
    idiomatic Simplified - so maintaining a second (then a third) parallel pool of last-resort
    canned lines would be pure duplication for zero quality gain.

    The guard is what makes that decision safe to keep: a NEW pooled line containing regional
    vocabulary that does not survive conversion fails here, pointing at the exact line. Detection
    reuses the persona glossary rather than a separate word list - if a term was worth localising
    in the persona, it is worth catching in a canned line."""
    import response_composer as rc
    from agent_latency import QUICK_ACK_POOLS
    from scripts.generate_persona_locales import _LOCALE_GLOSSARY
    from yueyue_v3.runtime import _provider_failure_reply, _social_chat_fallback

    lines: list[str] = []
    lines += rc._sticker_send_options() + rc._sticker_resend_options()
    lines += rc._plain_greeting_options("hi") + rc._plain_greeting_options("你好")
    for pool in QUICK_ACK_POOLS.values():
        lines += list(pool)
    for trigger in ("你像機器人", "陪我聊一下", "只是測試", "最近一直調你真的好累", "有點煩",
                    "今天好累", "隨便講點什麼", "幫我看一下檔案"):
        lines.append(_social_chat_fallback(trigger))
    lines.append(_provider_failure_reply(Exception()))

    glossary = _LOCALE_GLOSSARY[SIMPLIFIED]
    offenders = [
        (line, term) for line in lines for term in glossary if term in line
    ]
    assert not offenders, (
        "these canned lines carry regional vocabulary the egress conversion cannot localise; "
        f"reword them or add a per-language pool: {offenders}"
    )
    # And every pooled line must survive conversion as real Simplified, not a half-converted mix.
    for line in lines:
        converted = to_simplified_script(line)
        assert converted, line
        assert to_traditional_script(converted) != "" and "喫" not in converted, line


# --------------------------------------------------------------------- gates stay script-blind

def test_register_gate_still_bites_on_simplified_text() -> None:
    """Every list in voice_contract is written in Traditional. A natively-Simplified reply would
    sail past all of them without the Traditional shadow - 谂 is 諗, 呢个 is 呢個."""
    from voice_contract import voice_register_violation

    assert voice_register_violation("我谂住帮你", allow_simplified=True).startswith("cantonese_chars:")
    assert voice_register_violation("有几多个", allow_simplified=True).startswith("cantonese_phrase:")
    assert voice_register_violation("目前没有正在进行的任务喔", allow_simplified=True).startswith("taiwan_particle:")
    # Ordinary Simplified chat must stay clean - the shadow must not manufacture violations.
    for clean in ("主人回来啦～月月刚刚还在想你", "没关系的，慢慢来就好", "为什么这样呀", "关系还不错",
                  "这个应该系统的问题吧", "屏幕上开着网易云在播歌"):
        assert voice_register_violation(clean, allow_simplified=True) == "", clean


def test_social_policy_markers_match_in_either_script() -> None:
    """「先別硬撐」/「別把臉皺成那樣」 are canned-comfort markers that exist ONLY in Traditional in
    the marker tuples. Written in Simplified they are the same defect, and before the Traditional
    shadow they went completely ungated - which is how a natively-Simplified reply would have
    quietly escaped a rule the Traditional path enforces."""
    from yueyue_v3.runtime import _chat_reply_violates_social_policy

    for traditional, simplified in (("先別硬撐，早點睡", "先别硬撑，早点睡"),
                                    ("別把臉皺成那樣", "别把脸皱成那样")):
        assert _chat_reply_violates_social_policy(traditional, "在幹嘛")
        assert _chat_reply_violates_social_policy(simplified, "在干嘛", allow_simplified=True)


def test_nod_idiom_repair_is_script_blind() -> None:
    from voice_contract import repair_nod_idiom

    assert "點頭" not in repair_nod_idiom("這一步要等你點頭月月才動手")
    assert "点头" not in repair_nod_idiom("这一步要等你点头月月才动手")


# --------------------------------------------------------------------- end to end

def _runtime(tmp_path: Path, replies: list[str]):
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    provider = ScriptedProvider([ProviderResponse(text, "", []) for text in replies])
    return YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")


def test_simplified_owner_reply_is_simplified_without_any_detection(tmp_path: Path) -> None:
    """The owner writes in ENGLISH - nothing to detect, nothing to mirror. The reply is Simplified
    purely because that is what they set."""
    _seed_brain(tmp_path, SIMPLIFIED)
    runtime = _runtime(tmp_path, ["外面好像下雨了"])
    reply = runtime.process_turn(TurnEnvelope("chat1", "hey there", TurnMode.CHAT))
    assert reply == "外面好像下雨了"


def test_simplified_owner_gets_traditional_literals_rendered_in_their_script(tmp_path: Path) -> None:
    """The canned fallback pool is Traditional source code. A Simplified owner must never see it
    in Traditional just because a model reply was rejected."""
    _seed_brain(tmp_path, SIMPLIFIED)
    from yueyue_v3.runtime import _social_chat_fallback

    runtime = _runtime(tmp_path, ["這是一句被拒的話。" * 40, "還是太長了。" * 40])
    reply = runtime.process_turn(TurnEnvelope("chat1", "隨便說點什麼", TurnMode.CHAT))
    assert reply == to_simplified_script(_social_chat_fallback("隨便說點什麼")).rstrip("。")


def test_unset_language_keeps_the_legacy_mirroring_path(tmp_path: Path) -> None:
    """Nobody's experience changes until they use the setting: an owner typing Simplified with no
    language configured still gets their script mirrored, exactly as before."""
    _seed_brain(tmp_path)
    runtime = _runtime(tmp_path, ["外面好像下雨了"])
    reply = runtime.process_turn(TurnEnvelope("chat1", "你还在吗", TurnMode.CHAT))
    assert reply == "外面好像下雨了"


def test_configured_traditional_owner_ignores_simplified_input(tmp_path: Path) -> None:
    """Once a language is SET, the owner's own typing no longer steers the reply script - which is
    the entire point of replacing detection with a setting."""
    _seed_brain(tmp_path, TRADITIONAL)
    runtime = _runtime(tmp_path, ["外面好像下雨了"])
    reply = runtime.process_turn(TurnEnvelope("chat1", "你还在吗", TurnMode.CHAT))
    assert reply == "外面好像下雨了"


# --------------------------------------------------------------------- the owner-facing controls

def _redirect_live_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the rootless profile lookup at a temp file.

    Patching core_tools.PROFILE_FILE (not YUEYUE_ROOT_DIR) is deliberate: that constant is the ONE
    definition of where profile.json lives, and owner_language defers to it precisely so the
    setting and core_tools.real_update_profile can never write to two different files. Patching
    the env var instead would miss that linkage - and would let these tests scribble on the real
    owner's profile, which is how the live check on 2026-08-04 found the split in the first place.
    """
    profile = tmp_path / "workspace" / "memory" / "profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core_tools.PROFILE_FILE", str(profile))
    return profile


def test_rootless_profile_path_is_the_same_file_core_tools_writes() -> None:
    """The setting and the update_profile tool must agree on one file, by construction."""
    from core_tools import PROFILE_FILE

    assert owner_language.profile_path() == Path(PROFILE_FILE)


def test_language_skills_report_and_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skill_engine import SkillContext, execute_skill

    _redirect_live_profile(tmp_path, monkeypatch)
    context = SkillContext(chat_id="chat1", now=0.0)

    unset = execute_skill("get_language", {}, context)
    assert unset.ok and "還沒設定" in unset.note and "繁體中文" in unset.note

    switched = execute_skill("set_language", {"language": "簡體"}, context)
    assert switched.ok and "简体中文" in switched.note
    assert owner_language.read_language(tmp_path) == SIMPLIFIED

    now = execute_skill("get_language", {}, context)
    assert now.ok and "简体中文" in now.note

    rejected = execute_skill("set_language", {"language": "火星文"}, context)
    assert not rejected.ok
    assert owner_language.read_language(tmp_path) == SIMPLIFIED


def test_language_skills_are_in_the_catalog_the_model_sees() -> None:
    """Routing is by tool description, not by a keyword table in front of the model - so the
    skills only work if they are actually offered."""
    from skill_engine import skill_tools

    names = {tool.name for tool in skill_tools()}
    assert {"get_language", "set_language"} <= names


def test_outgoing_message_script_never_touches_sticker_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The egress normaliser runs AFTER markers are stripped. Sticker payloads are FILENAMES on
    disk (「嗚嗚嗚.gif」); converting one would break the lookup silently."""
    import main

    _redirect_live_profile(tmp_path, monkeypatch)
    owner_language.write_language(SIMPLIFIED, tmp_path)
    assert main._in_owner_script("我先處理一下") == "我先处理一下"
    # sanity: the helper is a no-op for a Traditional owner
    owner_language.write_language(TRADITIONAL, tmp_path)
    assert main._in_owner_script("我先處理一下") == "我先處理一下"
