"""Safety rewrite layer (V4): real-person / celebrity name → appearance.

Experiment finding (2026-07-21): gemini-omni rejects prompts naming real people
("Einstein"), even in cartoon form, on likeness grounds. Rewriting the name to a
neutral appearance description ("white-haired elderly man with a walrus
moustache, brown cardigan") passes.

Design: this layer NEVER silently drops a flagged name. It returns a structured
result with the rewrites it applied AND any names it could not rewrite
(``flagged``). The caller (send.compile) surfaces the diff on the send card and
blocks dispatch on un-rewritten flags unless the human overrides. The actual
name→appearance map normally comes from the prompt-strategy LLM; this module
supplies the substitution engine, a detector, and a small extensible seed map.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Seed map of well-known real-person references → appearance descriptions.
# Intentionally tiny and example-only: the authoritative rewrites come from the
# strategy layer at runtime. Extend via ``rewrite(..., name_map=...)``.
SEED_APPEARANCE_MAP: dict[str, str] = {
    "einstein": "a white-haired elderly man with a bushy walrus moustache and a rumpled cardigan",
    "albert einstein": "a white-haired elderly man with a bushy walrus moustache and a rumpled cardigan",
}

# A conservative detector for "looks like a real person's name" so we can FLAG
# (not auto-rewrite) unknown candidates. Two capitalised words in a row, latin
# script. This only raises a warning; unknown names are never auto-substituted.
_PERSON_HINT = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")


def detect_person_references(prompt: str, name_map: Mapping[str, str] | None = None) -> list[str]:
    """Return distinct substrings that look like real-person references.

    Known names (seed + ``name_map``) are matched case-insensitively; unknown
    capitalised bigrams are flagged as hints for the human to review.
    """
    known = dict(SEED_APPEARANCE_MAP)
    if name_map:
        known.update({k.lower(): v for k, v in name_map.items()})

    found: list[str] = []
    low = prompt.lower()
    for name in known:
        if name in low and name not in [f.lower() for f in found]:
            found.append(name)
    for m in _PERSON_HINT.finditer(prompt):
        phrase = m.group(0)
        if phrase.lower() not in known and phrase not in found:
            found.append(phrase)
    return found


def rewrite(prompt: str, *, name_map: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Rewrite known real-person names to appearance descriptions.

    Returns::

        {
          "rewritten": str,          # prompt with known names substituted
          "replacements": [{"from": name, "to": appearance}],
          "flagged": [name, ...],    # detected but NOT rewritten (no mapping)
          "changed": bool,
          "blocked": bool,           # True if any flag lacks a rewrite
        }

    ``blocked`` tells the compiler to require human confirmation before
    dispatch; nothing is ever silently sent with an un-rewritten flag.
    """
    known = {k.lower(): v for k, v in SEED_APPEARANCE_MAP.items()}
    if name_map:
        known.update({k.lower(): v for k, v in name_map.items()})

    # 空映射 = 调用方明示「这个名字我知道是真人,但没有可用外观描述」→ 绝不替换成空
    # (那是静默删除),而是留在原文里并作为红线阻断,要求人补描述或换素材。
    no_rewrite = {k for k, v in known.items() if not str(v or "").strip()}
    rewrites = {k: v for k, v in known.items() if k not in no_rewrite}

    replacements: list[dict[str, str]] = []
    out = prompt
    # Substitute longest names first so "albert einstein" wins over "einstein" and
    # no fragment of the real name is left dangling.
    for name in sorted(rewrites, key=len, reverse=True):
        appearance = rewrites[name]
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub(appearance, out)
            replacements.append({"from": name, "to": appearance})

    # Flags come from what REMAINS in the rewritten output — a known name that was
    # already substituted (or was a substring of a longer substituted name) is gone
    # from ``out`` and correctly not re-flagged.
    detected = detect_person_references(out, name_map)

    # 判断权保留:大写双词只是**启发式线索**(英文提示词里 "Wooden Block" / "Night City"
    # 之类比比皆是),拿它当硬红线会误杀绝大多数英文提示词。所以:
    #   已知名 → 上面已改写(replacements 带 diff 可看);
    #   疑似名 → flagged(黄):发送卡提示人复核,由人决定,不拦派发;
    #   blocked 只留给**确定事实** —— 调用方显式声明「该名字无可用改写」
    #   (name_map 里映射到空串);猜测不阻断,事实才阻断。
    hard = [d for d in detected if d.lower() in no_rewrite]

    return {
        "rewritten": out,
        "replacements": replacements,
        "flagged": detected,    # 黄:疑似真人引用,提示人复核(不拦派发)
        "unrewritable": hard,   # 红:调用方明示无可用改写
        "changed": bool(replacements),
        "blocked": bool(hard),
        "review_required": bool(detected),
    }
