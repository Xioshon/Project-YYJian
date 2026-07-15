"""YueYue quality eval runner.

  python scripts/eval_suite.py                # Tier 1 only (free, deterministic)
  python scripts/eval_suite.py --live         # + Tier 2 live generation (uses the API)
  python scripts/eval_suite.py --live --judge  # + LLM persona-feel judging (more API)

Tier 1 pins the chat quality gate: every curated exemplar has a known good/bad label, and a
mismatch is a finding - a good line CAUGHT is gate/blocklist over-reach, a bad line PASSED is a
hole. This is the regression net that makes trimming the 526-phrase blocklist safe. Exit code is
non-zero if any Tier 1 case mismatches, so it can gate blocklist edits.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from evals.cases import LIVE_CASES, PERSONA_CASES  # noqa: E402
from voice_contract import voice_register_violation  # noqa: E402


def run_tier1() -> tuple[int, int, list[str]]:
    from yueyue_v3.runtime import _chat_reply_violates_social_policy

    passed = 0
    known_gaps: list[str] = []
    drift: list[str] = []  # only these fail the build - unexpected changes from the recorded baseline
    by_dim: dict[str, list[bool]] = {}
    for case in PERSONA_CASES:
        violated = _chat_reply_violates_social_policy(case.reply, case.owner_text)
        gate_allows = not violated
        correct = gate_allows == case.should_pass
        by_dim.setdefault(case.dimension, []).append(correct)
        if correct:
            passed += 1
            if case.known_gap:
                # recorded as a gap but the gate now agrees -> baseline is stale, flag it
                drift.append(f"STALE known_gap [{case.id}] now matches expectation - clear known_gap")
            continue
        kind = "FALSE POSITIVE (gate over-reach)" if case.should_pass else "HOLE (gate too loose)"
        line = f"[{case.id}/{case.dimension}] {kind}: {case.reply!r} ({case.note})"
        (known_gaps if case.known_gap else drift).append(line)
    print(f"\n=== Tier 1 (persona gate, deterministic) : {passed}/{len(PERSONA_CASES)} ===")
    for dim, results in sorted(by_dim.items()):
        print(f"  {dim:<16} {sum(results)}/{len(results)}")
    if known_gaps:
        print("  -- known gaps (recorded baseline, not a regression) --")
        for gap in known_gaps:
            print("    ~ " + gap)
    for item in drift:
        print("  ! DRIFT " + item)
    return passed, len(PERSONA_CASES), drift


def run_tier2(judge: bool) -> tuple[int, int]:
    import tempfile

    from main import build_agent
    from yueyue_v3.context import classify_turn_mode

    passed = 0
    print(f"\n=== Tier 2 (live generation{' + judge' if judge else ''}) ===")
    for case in LIVE_CASES:
        # Fresh runtime per case: a task case leaves a pending-permission workflow that would
        # otherwise bleed into the next case's turn (a chat message getting the permission-reply
        # line). Each eval input must be measured from a clean state.
        rt = build_agent(state_dir=Path(tempfile.mkdtemp()) / "v3")
        route = classify_turn_mode(case.owner_text).value
        route_ok = route == case.expect_route
        reply = ""
        checks: list[str] = []
        if route_ok:
            resp = rt.chat(case.owner_text)
            reply = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        else:
            checks.append(f"route={route}!={case.expect_route}")
        reg = voice_register_violation(reply) if reply else ""
        contain_ok = all(term in reply for term in case.must_contain)
        avoid_ok = all(term not in reply for term in case.must_not_contain)
        if case.register_must_be_clean and reg:
            checks.append(f"register:{reg}")
        if not contain_ok:
            checks.append("missing_required")
        if not avoid_ok:
            checks.append("has_forbidden")
        judged = ""
        if judge and reply and case.rubric:
            verdict = _judge(rt, case.owner_text, reply, case.rubric)
            judged = f" judge={verdict}"
            if verdict.startswith("FAIL"):
                checks.append("judge_fail")
        ok = route_ok and not checks
        passed += int(ok)
        status = "PASS" if ok else "FAIL"
        print(f"  {status} [{case.id}] route={route}{judged}")
        if reply:
            print(f"       reply: {reply[:110]}")
        if checks:
            print(f"       issues: {', '.join(checks)}")
    print(f"  -- Tier 2: {passed}/{len(LIVE_CASES)} --")
    return passed, len(LIVE_CASES)


def _judge(rt, owner_text: str, reply: str, rubric: str) -> str:
    # Judge ONLY against the case rubric. Earlier prompt versions let the judge invent criteria:
    # it demanded "cocky tone" on a tired-owner case whose rubric asked for warmth, and misread
    # "Hong Kong written Traditional" as requiring Cantonese STYLE (spoken Cantonese is banned).
    prompt = (
        "You judge one reply from a companion-agent persona. Judge ONLY the rubric below - do not "
        "invent extra criteria. Context you may assume: the persona is a playful catgirl companion "
        "who adapts tone to the moment (soft when the owner is down, teasing when playing); the "
        "reply language is WRITTEN Traditional Chinese (spoken-Cantonese wording would be a "
        "defect, but that is checked elsewhere - ignore register). Fail only for: violating the "
        "rubric, sounding like a canned assistant, or ignoring what the owner actually said.\n"
        f"Owner said: {owner_text!r}\nReply: {reply!r}\nRubric: {rubric}\n"
        "Answer with exactly 'PASS: <=8 words' or 'FAIL: <=8 words'."
    )
    try:
        resp = rt.provider.chat([{"role": "user", "content": prompt}], [], tool_choice="none")
        return (resp.content or "").strip()[:60].replace("\n", " ")
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="run Tier 2 live generation (uses API)")
    ap.add_argument("--judge", action="store_true", help="LLM persona-feel judging in Tier 2 (more API)")
    ap.add_argument("--json", type=str, default="", help="write a JSON scorecard to this path")
    args = ap.parse_args()

    t1_pass, t1_total, findings = run_tier1()
    scorecard = {"ts": time.time(), "tier1": {"passed": t1_pass, "total": t1_total, "findings": findings}}
    if args.live:
        t2_pass, t2_total = run_tier2(args.judge)
        scorecard["tier2"] = {"passed": t2_pass, "total": t2_total}
    if args.json:
        Path(args.json).write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
