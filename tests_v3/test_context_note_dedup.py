from __future__ import annotations

import os
import tempfile

from agent_short_context import ShortContextBuffer


def _seeded_buffer() -> ShortContextBuffer:
    path = os.path.join(tempfile.mkdtemp(), "sc.json")
    buffer = ShortContextBuffer(path, max_turns=5)
    buffer.observe_turn("c", "幫我看看 https://example.com 這個網站")
    buffer.update_last_assistant("c", "好呀，我看看喵")
    buffer.observe_turn("c", "剛剛那個你覺得怎麼樣")
    buffer.update_last_assistant("c", "還不錯啦")
    return buffer


def test_chat_note_omits_conversation_lines_v3_already_injects():
    # For CHAT/SOCIAL the v3 ContextCompiler re-injects these exact turns as role-tagged messages,
    # so this metadata note must NOT duplicate the raw conversation (text=/last_reply=).
    buffer = _seeded_buffer()
    lean = buffer.render_for_turn("c", "剛剛那個怎麼樣", include_conversation=False)
    assert "text=" not in lean
    assert "last_reply=" not in lean


def test_task_note_keeps_full_conversation_background():
    # TASK/VISION do NOT get v3 recent(), so the note stays the sole conversational background.
    buffer = _seeded_buffer()
    full = buffer.render_for_turn("c", "剛剛那個怎麼樣", include_conversation=True)
    assert "text=" in full
    assert "last_reply=" in full


def test_lean_note_preserves_unique_contribution():
    # Reference resolution, topic/mood and URL context are this store's unique job and must survive
    # even when the conversation lines are dropped.
    buffer = _seeded_buffer()
    lean = buffer.render_for_turn("c", "剛剛那個怎麼樣", include_conversation=False)
    assert "可能指代" in lean
    assert "example.com" in lean
    assert "mood=" in lean


def test_default_is_full_render_so_existing_callers_are_unchanged():
    buffer = _seeded_buffer()
    assert buffer.render_for_turn("c", "剛剛那個怎麼樣") == buffer.render_for_turn(
        "c", "剛剛那個怎麼樣", include_conversation=True
    )
