from __future__ import annotations

import response_composer as rc
from chat_text_sanitizers import UNSTABLE_SOCIAL_META_MARKERS

# ROADMAP P1: the phrase blocklists are FROZEN - they may only shrink, never grow. Growing them
# is the whack-a-mole anti-pattern the owner explicitly ended ("我希望移除罐頭句，讓agent模型自己寫");
# quality problems get fixed with exemplars + model self-critique (see evals/) instead of phrase 527.
# When you legitimately REMOVE entries, lower the ceiling to the new count in the same commit.
GREETING_BAD_PHRASES_CEILING = 143
SOCIAL_META_MARKERS_CEILING = 179


def test_greeting_blocklist_never_grows():
    count = len(rc.GENERATED_GREETING_BAD_PHRASES)
    assert count <= GREETING_BAD_PHRASES_CEILING, (
        f"GENERATED_GREETING_BAD_PHRASES grew to {count} (> {GREETING_BAD_PHRASES_CEILING}). "
        "Do NOT add phrase entries - fix the failure with exemplars/self-critique (evals/cases.py) "
        "and, if the gate truly misses a class, a structural check. The blocklist only shrinks."
    )


def test_social_meta_markers_never_grow():
    count = len(UNSTABLE_SOCIAL_META_MARKERS)
    assert count <= SOCIAL_META_MARKERS_CEILING, (
        f"UNSTABLE_SOCIAL_META_MARKERS grew to {count} (> {SOCIAL_META_MARKERS_CEILING}). "
        "Frozen list - only shrinks. See docs/ROADMAP.md Phase 1."
    )
