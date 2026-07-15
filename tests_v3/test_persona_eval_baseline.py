from __future__ import annotations

from evals.cases import PERSONA_CASES
from yueyue_v3.runtime import _chat_reply_violates_social_policy


def test_persona_eval_matches_recorded_baseline():
    """Tier-1 persona eval as an automated drift guard.

    Every curated exemplar has an ideal should_pass label. The chat gate must match it, EXCEPT for
    cases flagged known_gap (recorded baseline the blocklist currently misses, deliberately not
    whack-a-mole-fixed). This test fails on DRIFT: a new mismatch, or a known_gap that silently
    changed. It is the regression net that makes trimming the 526-phrase blocklist safe - if a trim
    starts catching an ideal line (false positive) or letting a caught bad line through, this goes
    red. See scripts/eval_suite.py for the full runner (incl. the live tier)."""
    drift: list[str] = []
    for case in PERSONA_CASES:
        gate_allows = not _chat_reply_violates_social_policy(case.reply, case.owner_text)
        matches_ideal = gate_allows == case.should_pass
        if matches_ideal and case.known_gap:
            drift.append(f"{case.id}: recorded as known_gap but now matches - clear the flag")
        elif not matches_ideal and not case.known_gap:
            kind = "ideal line caught (over-reach)" if case.should_pass else "bad line allowed (hole)"
            drift.append(f"{case.id} [{case.dimension}]: {kind}")
    assert not drift, "persona eval drift from baseline:\n" + "\n".join(drift)


def test_persona_good_exemplars_are_not_over_caught():
    """Sharper focus: NO ideal line may be rejected by the gate. This is the property that most
    directly protects persona quality (a false positive silently degrades a good reply into a
    canned fallback), and the one a blocklist trim is least likely to break - so lock it hard."""
    for case in PERSONA_CASES:
        if case.should_pass:
            assert not _chat_reply_violates_social_policy(case.reply, case.owner_text), (
                f"ideal line wrongly caught by the gate: [{case.id}] {case.reply!r}"
            )
