"""YueYue quality eval suite.

Distinct from tests_v3/ (which lock mechanical invariants): these evals measure the QUALITY of
owner-facing output against the owner's standard. Tier 1 (persona_cases) is deterministic and free
- it pins the chat quality gate's judgments so the 526-phrase blocklist can be trimmed with a
regression net. Tier 2 (live_cases) generates real replies and needs the API. Run via
scripts/eval_suite.py. See [[owner-voice-standard]] and [[project-yyjian-yueyue-agent]].
"""
