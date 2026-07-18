from __future__ import annotations

import tempfile
from pathlib import Path

from voice_contract import voice_register_violation
from yueyue_v3.providers import ProviderResponse, ScriptedProvider
from yueyue_v3.runtime import YueYueRuntimeV3


def _runtime(*replies: str) -> YueYueRuntimeV3:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "workspace" / "brain").mkdir(parents=True)
    (tmp / "workspace" / "brain" / "personality.md").write_text("月月是清新軟萌的貓娘女孩", encoding="utf-8")
    (tmp / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse(text, "", []) for text in replies])
    return YueYueRuntimeV3(tmp, provider, state_dir=tmp / "v3")


def test_opener_is_generated_and_register_clean():
    rt = _runtime("嗨～主人，我是月月，今天第一次見到你呢，以後多多關照呀")
    opener = rt.compose_opener()
    assert opener
    assert voice_register_violation(opener) == ""


def test_opener_retries_then_gives_up_cleanly():
    # First attempt leaks an internal term, second is empty -> no opener rather than a bad one.
    rt = _runtime("我剛初始化完成，workflow 已就緒", "")
    assert rt.compose_opener() == ""


def test_opener_repairs_simplified_leak():
    # A Simplified leak is repaired (not rejected), so a good opener still ships.
    rt = _runtime("主人来啦～月月等你好久了呢")
    opener = rt.compose_opener()
    assert opener
    assert "來" in opener and "来" not in opener
    assert voice_register_violation(opener) == ""


def test_opener_second_attempt_wins():
    rt = _runtime("系統啟動中，正在載入模組", "嗨～我是月月，今天第一次見到你，以後請多多關照啦")
    opener = rt.compose_opener()
    assert opener.startswith("嗨")
