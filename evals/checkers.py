"""
Checks for cases whose right answers are many — so an expected value cannot be
written down, but a rule can decide.

Each takes the case's `expect` (the rule's data) and the answer, and returns
`(ok, score)`. A case names its check with `"checker": "<name>"`; the case set's
controls feed every check its oracle answers and a known-wrong one, so a check
that accepts nothing, or anything, is caught before a run.
"""

from __future__ import annotations

import json
import re


def _json(text: str):
    text = re.sub(r"```(?:json)?", "", str(text or ""))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def schedule(expect: dict, output) -> tuple[bool, float]:
    """
    A meeting plan that keeps every constraint: every meeting placed in an allowed
    slot, nobody in two meetings at once, everyone available, the orderings kept.
    Any plan that does is right; the score is the share of constraints kept.
    """
    plan = _json(output)
    if not isinstance(plan, dict):
        return False, 0.0
    meetings, slots = expect["meetings"], expect["slots"]
    kept, total = 0, 0

    def rule(ok: bool) -> None:
        nonlocal kept, total
        total += 1
        kept += bool(ok)

    for m in meetings:
        rule(plan.get(m) in slots)
    for person, busy in expect["unavailable"].items():
        for m, people in meetings.items():
            if person in people:
                rule(plan.get(m) not in busy)
    names = list(meetings)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if set(meetings[a]) & set(meetings[b]):
                rule(plan.get(a) is None or plan.get(a) != plan.get(b))
    order = {s: n for n, s in enumerate(slots)}
    for before, after in expect["before"]:
        rule(plan.get(before) in order and plan.get(after) in order
             and order[plan[before]] < order[plan[after]])
    for m, earliest in expect.get("not_before", {}).items():
        rule(plan.get(m) in order and order[plan[m]] >= order[earliest])
    return kept == total, kept / total if total else 0.0


def translation(expect: dict, output) -> tuple[bool, float]:
    """
    A translated record: the same keys, what must not change unchanged, no
    Cyrillic left in the translated fields, and the words the translation needs.
    """
    got = _json(output)
    if not isinstance(got, dict):
        return False, 0.0
    checks = [set(got) == set(expect["keys"])]
    for key, value in expect["unchanged"].items():
        checks.append(got.get(key) == value)
    for key in expect["translated"]:
        text = json.dumps(got.get(key), ensure_ascii=False)
        checks.append(not re.search(r"[а-яА-ЯёЁ]", text))
    blob = json.dumps(got, ensure_ascii=False).casefold()
    for word in expect["words"]:
        checks.append(word.casefold() in blob)
    for key, length in expect.get("lengths", {}).items():
        checks.append(isinstance(got.get(key), list) and len(got[key]) == length)
    # Placeholders and markup a template engine will look for: kept byte for byte,
    # or the translation breaks the page it is poured into.
    raw = json.dumps(got, ensure_ascii=False)
    for token in expect.get("verbatim", []):
        checks.append(raw.count(token) == expect.get("verbatim_counts", {}).get(token, 1))
    return all(checks), sum(checks) / len(checks)


CHECKS = {"schedule": schedule, "translation": translation}
