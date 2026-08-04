from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import response_composer as rc  # noqa: E402
import yueyue_v3.context as ctx  # noqa: E402
import yueyue_v3.runtime as rt  # noqa: E402
from scripts import generate_persona_locales as locales  # noqa: E402

# This guard exists because response-quality tuning kept growing these reject/marker
# lists cycle after cycle without anyone stepping back to ask whether the *prompt*
# should change instead of the blocklist. It does not forbid growth - it forces a
# deliberate, visible decision (bump BASELINE) instead of silent, unreviewed growth.
#
# When this check fails: look at what was added. If it is a real internal-state
# leakage term (runtime/workflow/permission/etc.), adding it is fine - bump the
# baseline number for that entry. If it is a stylistic phrase (another "sounds too
# much like ChatGPT" catch), prefer improving workspace/brain/personality_samples.md
# or the mode prompt instead of adding a 40th banned phrase.

TOLERANCE = 5  # allow small, reviewed growth before requiring a baseline bump

BASELINE: dict[str, tuple[object, int]] = {
    "runtime.ANTI_CHATGPT_PROCESSING_MARKERS": (rt.ANTI_CHATGPT_PROCESSING_MARKERS, 26),
    "runtime.CHAT_DEVELOPMENT_REQUEST_MARKERS": (rt.CHAT_DEVELOPMENT_REQUEST_MARKERS, 14),
    "runtime.CHAT_META_LEAKAGE_MARKERS": (rt.CHAT_META_LEAKAGE_MARKERS, 24),
    "runtime.CHAT_PROVIDER_TEMPLATE_MARKERS": (rt.CHAT_PROVIDER_TEMPLATE_MARKERS, 29),
    "runtime.CHAT_ROLEPLAY_ACTION_MARKERS": (rt.CHAT_ROLEPLAY_ACTION_MARKERS, 16),
    "runtime.CHAT_ROLEPLAY_BODY_MARKERS": (rt.CHAT_ROLEPLAY_BODY_MARKERS, 6),
    "runtime.CONTROLLED_FALLBACK_MARKERS": (rt.CONTROLLED_FALLBACK_MARKERS, 3),
    "runtime.LIGHT_CATGIRL_STYLE_MARKERS": (rt.LIGHT_CATGIRL_STYLE_MARKERS, 21),
    "runtime.SOCIAL_GENERIC_COMFORT_MARKERS": (rt.SOCIAL_GENERIC_COMFORT_MARKERS, 19),
    "runtime.SOCIAL_META_REPLY_MARKERS": (rt.SOCIAL_META_REPLY_MARKERS, 10),
    "runtime.SOCIAL_OVERPRODUCTION_MARKERS": (rt.SOCIAL_OVERPRODUCTION_MARKERS, 13),
    "runtime.TEST_CONTEXT_REPLY_MARKERS": (rt.TEST_CONTEXT_REPLY_MARKERS, 7),
    "context.DEVELOPMENT_REQUEST_MARKERS": (ctx.DEVELOPMENT_REQUEST_MARKERS, 14),
    "context.PROJECT_RUNTIME_META_MARKERS": (ctx.PROJECT_RUNTIME_META_MARKERS, 20),
    "context.PROMPT_SHAPED_MARKERS": (ctx.PROMPT_SHAPED_MARKERS, 7),
    "context.UNSTABLE_SOCIAL_META_MARKERS": (ctx.UNSTABLE_SOCIAL_META_MARKERS, 179),
    "context.WORKFLOW_APPROVAL_MARKERS": (ctx.WORKFLOW_APPROVAL_MARKERS, 10),
    "context.WORKFLOW_ERROR_MARKERS": (ctx.WORKFLOW_ERROR_MARKERS, 10),
    "response_composer.GENERATED_GREETING_BAD_PHRASES": (rc.GENERATED_GREETING_BAD_PHRASES, 143),
    # The per-language persona glossary and its adjudication record. Tracked here for the same
    # reason as everything above: each entry is a judgement call, and judgement calls must
    # accumulate visibly. Both are DERIVED from `generate_persona_locales.py --audit`, so growth
    # should follow a measurement, never a hunch.
    "locales.GLOSSARY[zh-Hans]": (locales._LOCALE_GLOSSARY["zh-Hans"], 2),
    "locales.AUDIT_REJECTED[zh-Hans]": (locales._AUDIT_REJECTED["zh-Hans"], 5),
}

failed = 0
for label, (collection, baseline_count) in BASELINE.items():
    current_count = len(collection)
    growth = current_count - baseline_count
    if growth > TOLERANCE:
        failed += 1
        print(
            f"FAIL {label}: grew from {baseline_count} to {current_count} "
            f"(+{growth}, tolerance {TOLERANCE}). Review the new entries; if they are "
            "genuine leakage terms, bump BASELINE in this script deliberately. If they "
            "are stylistic whack-a-mole additions, fix the prompt/samples instead."
        )
    elif growth < 0:
        print(f"INFO {label}: shrank from {baseline_count} to {current_count} (cleanup, lower BASELINE when convenient).")
    else:
        print(f"PASS {label}: {current_count} (baseline {baseline_count})")

raise SystemExit(failed)
