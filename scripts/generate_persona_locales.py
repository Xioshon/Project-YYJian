"""Generate (and gate-check) the per-language copies of YueYue's persona material.

Stage B of the three-language work. The old way to serve a Simplified owner was to generate a
Traditional reply and convert it on the way out - detection, mirroring, and a hand-grown table of
conversion corrections, which is where the archaic 喫 came from. The new way is to give each
language its OWN persona material, generated once, so YueYue reads Simplified samples and writes
Simplified natively with nothing to convert.

Mechanical conversion carries almost everything: the persona is voice and rhythm, and those are
script-independent. Two kinds of text need help, and they are deliberately separate mechanisms:

  _LOCALE_BLOCKS  - passages whose MEANING is tied to the script they are written in. 「字感基準：
                    香港書面繁體」 converts into a self-contradiction, and the line listing the
                    Taiwan words to AVOID inverts into a line recommending them. Exact-match and
                    fail-loud: if the source text moves, this script refuses to run rather than
                    leave a stale Simplified copy behind.
  _LOCALE_GLOSSARY - terms where converting the script alone leaves a word that is correct but not
                    idiomatic (檔案 -> 档案, which a mainland reader parses as "dossier" rather
                    than "computer file").

The glossary is NOT hand-accumulated folklore, which is the failure mode this project keeps
removing. It is the reviewed OUTPUT of `--audit`, a repeatable measurement: --audit runs two
independent off-the-shelf regional converters (zhconv zh-cn and OpenCC tw2sp) over every line of
the persona corpus and reports each word they would write differently.

Using them as DETECTORS rather than as rewriters is the whole point. Measured on 2026-08-04,
OpenCC's tw2sp assumes TAIWAN source vocabulary while this owner writes HK: alongside the two
suggestions worth taking it also proposed 顏文字->颜文本, 核心->内核, 資料->数据 and 文件->文档 -
the last of which would have undone the very fix this glossary exists for. Blindly applying a
regional tool to the wrong region is the same mistake that produced the archaic 喫. So the tools
discover candidates, a human adjudicates once over a bounded corpus of three files this repo owns,
and both the accepted and the REFUSED decisions are recorded below so `--audit` stays actionable
(a clean run means "nothing new to decide"). Re-run --audit after editing the persona instead of
guessing; growth of both tables is tracked by scripts/blocklist_growth_check.py.

    python scripts/generate_persona_locales.py            # write the locale files
    python scripts/generate_persona_locales.py --check    # gate mode: fail if they are stale
    python scripts/generate_persona_locales.py --audit    # re-derive glossary candidates (dev)

--check runs in start_yueyue.ps1 -SelfTest, so editing personality.md without regenerating is a
red build rather than a persona that quietly drifts apart between languages. --audit needs the
dev-only dependency opencc-python-reimplemented; the live bot never imports it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from owner_language import SIMPLIFIED, locale_suffix  # noqa: E402
from voice_contract import VOICE_REGISTER_ZH_HANS  # noqa: E402
from yueyue_v3.context import to_simplified_script  # noqa: E402

SOURCE_FILES = (
    "workspace/brain/personality.md",
    "workspace/brain/personality_samples.md",
    "workspace/brain/rules.md",
)

# Passages whose MEANING is tied to the script they are written in. Keyed by target language; each
# entry is (exact text in the Traditional source, replacement in the target language). The register
# replacement is derived from voice_contract.VOICE_REGISTER_ZH_HANS rather than retyped, so the
# register keeps exactly one definition per language.
_LOCALE_BLOCKS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    SIMPLIFIED: {
        "workspace/brain/personality_samples.md": (
            (
                "字感基準：香港書面繁體 + 內地網絡聊天節奏。用「屏幕/網絡/軟件/視頻/信息」這一路的詞，\n"
                "不用台灣腔的「螢幕/網路/軟體」；語尾不用「喔/喲/耶」這類台味助詞；書面繁體不是粵語口語，\n"
                "不要寫「嘅/喺/㗎/唔/冇/撳/呢個/幾多」這類粵語字。內地網聊的梗（笑死/絕了/emmm/？？？）\n"
                "可以自然出現，但別堆砌，一句頂多一個。",
                VOICE_REGISTER_ZH_HANS,
            ),
        ),
    },
}

# Terms where converting the SCRIPT alone leaves a word that is correct but not idiomatic in the
# target locale. Written in the SOURCE's script (Traditional) on both sides - the replacement is a
# Traditional-form word chosen so the normal converter carries it the rest of the way (文件夾 ->
# 文件夹), which keeps this glossary script-agnostic and means it never needs a second copy per
# script. Applied longest-key-first so an entry can never eat a prefix of another.
#
# Every entry here was adjudicated from `--audit` output on 2026-08-04, not invented.
_LOCALE_GLOSSARY: dict[str, dict[str, str]] = {
    SIMPLIFIED: {
        # 檔案/資料夾 are the Windows-Traditional words. Script conversion alone yields 档案/资料夹,
        # which a mainland reader parses as "dossier"/"data folder" rather than "computer file".
        "檔案": "文件",
        "資料夾": "文件夾",
    },
}

# The other half of the same adjudication: suggestions that were reviewed and REFUSED. Recorded as
# data so `--audit` stays actionable - a clean run means "nothing new to decide" rather than the
# same rejected noise every time - and so nobody re-litigates them from scratch. These exist only
# in the dev audit path and have zero effect on runtime behaviour.
_AUDIT_REJECTED: dict[str, dict[str, str]] = {
    SIMPLIFIED: {
        "文字": "文本",   # 顏文字 is kaomoji and 「靠文字和語氣就夠」 is prose - neither is 文本
        "文件": "文档",   # tw2sp reads Traditional 文件 as Taiwan "document"; in this owner's HK
                         # register it already means "computer file", so this would UNDO the
                         # glossary entry above (文件在下载文件夹 -> 文档在下载文档夹)
        "核心": "内核",   # 核心人設 is not 内核人設
        "资料": "数据",   # only occurs inside 資料夾, which the glossary already handles
        "萤幕": "屏幕",   # only occurs in the register block, which is replaced wholesale -
                         # converting it there would invert "avoid these Taiwan words"
    },
}

_HEADER = (
    "<!-- GENERATED by scripts/generate_persona_locales.py from {source} - do not edit by hand.\n"
    "     Edit the Traditional source and re-run the generator; `--check` runs in the self-test. -->\n"
)


def render(relative: str, language: str) -> str:
    """The target-language content for one persona file, or "" when conversion is a no-op.

    An English-only file (rules.md) converts to itself; shipping a byte-identical copy would be
    dead weight and one more thing to drift, so it is skipped and the loader falls back to the
    Traditional source on its own.
    """
    # workspace/brain is gitignored - the persona is the owner's private material, so a fresh
    # clone has none of it (see docs/PERSONA_GUIDE.md). Nothing to generate is not an error.
    if not (ROOT / relative).exists():
        return ""
    source = (ROOT / relative).read_text(encoding="utf-8")
    body = source
    blocks = _LOCALE_BLOCKS.get(language, {}).get(relative, ())
    # Overrides are cut out BEFORE the conversion and pasted back AFTER it. Their replacements are
    # already hand-written in the target language, and running them through the converter would
    # quietly rewrite characters that were chosen deliberately (it turned the Cantonese example
    # 撳 into 揿 on the first run). ASCII placeholders survive Han conversion untouched.
    for index, (original, _) in enumerate(blocks):
        if original not in body:
            raise SystemExit(
                f"{relative}: a locale override no longer matches the source text.\n"
                f"The Traditional source changed; update _LOCALE_BLOCKS in "
                f"scripts/generate_persona_locales.py to match. Missing text:\n{original[:120]}…"
            )
        body = body.replace(original, f"@@LOCALE_OVERRIDE_{index}@@")
    # Glossary before conversion: entries are Traditional-form on both sides, so the converter
    # below finishes the job. Longest key first so 資料夾 wins over any shorter overlapping key.
    for term, replacement in sorted(
        _LOCALE_GLOSSARY.get(language, {}).items(), key=lambda item: -len(item[0])
    ):
        body = body.replace(term, replacement)
    if language == SIMPLIFIED:
        # Same converter the live reply path uses, so the persona material and any repaired reply
        # can never disagree about how a character should be written.
        body = to_simplified_script(body)
    for index, (_, replacement) in enumerate(blocks):
        body = body.replace(f"@@LOCALE_OVERRIDE_{index}@@", replacement)
    if body == source:
        return ""
    return _HEADER.format(source=Path(relative).name) + body


def target_path(relative: str, language: str) -> Path:
    stem, _, extension = relative.rpartition(".")
    return ROOT / f"{stem}{locale_suffix(language)}.{extension}"


def audit(language: str = SIMPLIFIED) -> int:
    """Re-derive glossary candidates by measurement, so the glossary above is never guesswork.

    Runs two independent off-the-shelf regional converters over every n-gram in the persona corpus
    and reports the terms either considers regionally marked, next to what this project's own
    converter currently produces. Neither tool is authoritative - OpenCC's tw2sp assumes Taiwan
    source vocabulary while this owner writes HK - so the output is a candidate list for a human to
    adjudicate ONCE, not a patch to apply. Anything already handled by _LOCALE_GLOSSARY is filtered
    out, so a clean run means "nothing new to decide".
    """
    try:
        import opencc
        from zhconv import convert
    except ImportError:
        print(
            "--audit needs the dev-only converters. Install them with:\n"
            "  pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 2

    tw2sp = opencc.OpenCC("tw2sp")
    known = _LOCALE_GLOSSARY.get(language, {})
    rejected = _AUDIT_REJECTED.get(language, {})
    lines: list[str] = []
    for relative in SOURCE_FILES:
        path = ROOT / relative
        if path.exists():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
    if not lines:
        print("no persona material in this checkout - nothing to audit")
        return 0

    # Compared line by line, not by n-gram: the converters are context-sensitive, so an isolated
    # fragment reports differences that do not exist in the real sentence (著 resolves to 着
    # correctly given its clause, but not on its own).
    candidates: list[tuple[str, str, str]] = []
    for line in lines:
        if not re.search(r"[一-鿿]", line):
            continue
        source = line
        for term, replacement in sorted(known.items(), key=lambda item: -len(item[0])):
            source = source.replace(term, replacement)
        current = _same_quotes(to_simplified_script(source))
        for name, produced in (("zhconv", convert(source, "zh-cn")), ("opencc", tw2sp.convert(source))):
            produced = _same_quotes(produced)
            if produced == current:
                continue
            for was, now in _word_differences(current, produced):
                # A difference is old news when applying the already-rejected mappings to it
                # reproduces the suggestion exactly. Substring matching would be too eager: a span
                # holding BOTH a rejected term and a genuinely new one must still be reported.
                explained = was
                for term, replacement in sorted(rejected.items(), key=lambda item: -len(item[0])):
                    explained = explained.replace(term, replacement)
                if explained == now:
                    continue
                candidates.append((was, now, name))

    seen: dict[tuple[str, str], set[str]] = {}
    for was, now, name in candidates:
        seen.setdefault((was, now), set()).add(name)

    print(f"audited {len(lines)} persona lines")
    print(f"glossary applies: {', '.join(f'{k}->{v}' for k, v in known.items()) or '(none)'}")
    print(f"previously rejected: {', '.join(f'{k}->{v}' for k, v in rejected.items()) or '(none)'}")
    if not seen:
        print("\nno new regionally-marked terms - the glossary is up to date")
        return 0
    print(f"\n{len(seen)} term(s) need adjudication (decide each; do not paste blindly):")
    for (was, now), names in sorted(seen.items()):
        print(f"  {was} -> {now}   (suggested by {', '.join(sorted(names))})")
    print("\nAccept -> add to _LOCALE_GLOSSARY. Reject -> add to _AUDIT_REJECTED with the reason.")
    return 0


# Quote style is a settled question, not a vocabulary one: mainland Simplified uses “ ”, which this
# project's converter already produces and OpenCC does not. Normalising both sides before the diff
# keeps that one decision from swallowing every neighbouring word into the report.
_QUOTE_STYLES = str.maketrans({"「": "“", "」": "”", "『": "‘", "』": "’"})


def _same_quotes(text: str) -> str:
    return text.translate(_QUOTE_STYLES)


def _word_differences(current: str, produced: str) -> list[tuple[str, str]]:
    """The whole WORDS that differ between two conversions of the same line.

    Differing positions are grouped into runs and then widened to the surrounding Han run, so a
    reviewer sees 文件 -> 文档 rather than the character-level 件 -> 档, which is unadjudicable.
    """
    if len(current) != len(produced):
        return [(current.strip()[:32], produced.strip()[:32])]
    han = re.compile(r"[一-鿿]")
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(current):
        if current[index] == produced[index]:
            index += 1
            continue
        start = end = index
        while end < len(current) and current[end] != produced[end]:
            end += 1
        while start > 0 and han.match(current[start - 1]) and han.match(produced[start - 1]):
            start -= 1
        while end < len(current) and han.match(current[end]) and han.match(produced[end]):
            end += 1
        spans.append((start, end))
        index = end
    return [(current[start:end], produced[start:end]) for start, end in spans]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any locale file is stale")
    parser.add_argument(
        "--audit", action="store_true", help="re-derive glossary candidates by measurement (dev)"
    )
    arguments = parser.parse_args()
    if arguments.audit:
        return audit()

    stale: list[str] = []
    for language in (SIMPLIFIED,):
        for relative in SOURCE_FILES:
            expected = render(relative, language)
            path = target_path(relative, language)
            if not expected:
                if path.exists() and arguments.check:
                    stale.append(f"{path.relative_to(ROOT)} exists but converts to the source unchanged")
                continue
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current == expected:
                print(f"ok   {path.relative_to(ROOT)}")
                continue
            if arguments.check:
                stale.append(str(path.relative_to(ROOT)))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")

    if stale:
        print("\nPersona locale files are stale:", file=sys.stderr)
        for item in stale:
            print(f"  - {item}", file=sys.stderr)
        print("Run: python scripts/generate_persona_locales.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
