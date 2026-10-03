#!/usr/bin/env python3
"""
The fold, tested on its own.

It is a pure function, so it needs no AWS and no server — and it is the one place
where a defect is invisible to every other test: if the fold drifts, the agent
reasons over a conversation that did not happen, and nothing turns red.

So the headline test is not an output comparison but an **invariant**: whatever the
log contains, the fold never returns a history with a tool call nobody answered.
`Agent.step()` raises on one, and providers disagree about such a history in the
worst possible way — three reject it, one is lenient, and a self-hosted
OpenAI-compatible server templates it through so the model meets its own unanswered
call and improvises differently each time.

    python3 tests/test_fold.py
"""

from __future__ import annotations

import itertools
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import events as E  # noqa: E402
from fold import UNKNOWN_OUTCOME, fold, unanswered  # noqa: E402


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        print(f"\n{self.passed}/{self.passed + len(self.failures)} checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


def log(*types_and_payloads) -> list[dict]:
    """A log with sequence numbers, from (type, payload) pairs."""
    out = [dict(E.event(E.SESSION_CREATED), seq=0)]
    for i, (kind, payload) in enumerate(types_and_payloads, start=1):
        out.append(dict(E.event(kind, **payload), seq=i))
    return out


CALL = {"id": "toolu_1", "name": "search", "arguments": {"q": "x"}}


def run() -> int:
    res = Results()

    # ── the ordinary shapes ────────────────────────────────────────────────

    messages = fold(log((E.USER_MESSAGE, {"text": "hello"})),
                    instructions="be brief")
    res.check("instructions become a system turn",
              messages[0] == {"role": "system", "parts": ["be brief"]},
              f"{messages[0]}")
    res.check("a user message becomes a user turn",
              messages[1]["role"] == "user"
              and messages[1]["parts"][0]["text"] == "hello", f"{messages[1]}")

    messages = fold(log((E.USER_MESSAGE, {"text": "hi"}),
                        (E.AGENT_MESSAGE, {"text": "there"})))
    res.check("no instructions means no system turn",
              [m["role"] for m in messages] == ["user", "assistant"],
              f"{[m['role'] for m in messages]}")

    messages = fold(log((E.AGENT_TOOL_CALL, {"calls": [CALL], "text": "looking"}),
                        (E.TOOL_RESULT, {"call_id": "toolu_1", "result": "found"})))
    res.check("a call becomes an assistant turn with tool_calls",
              messages[0]["tool_calls"] == [{"id": "toolu_1", "name": "search",
                                             "arguments": {"q": "x"}}],
              f"{messages[0]}")
    res.check("text alongside a call is kept",
              messages[0]["parts"][0]["text"] == "looking", f"{messages[0]}")
    res.check("a result becomes a tool turn keyed by call_id",
              messages[1] == {"role": "tool", "call_id": "toolu_1",
                              "parts": [{"type": "text", "text": "found"}]},
              f"{messages[1]}")

    # ── what must be ignored ───────────────────────────────────────────────

    noisy = log(
        (E.USER_MESSAGE, {"text": "go"}),
        (E.MODEL_REQUEST_START, {}),
        (E.TOOL_CALL_STARTED, {"call_id": "toolu_1"}),
        (E.APPROVAL_REQUESTED, {"call_id": "toolu_1"}),
        (E.SESSION_USAGE, {"cost_usd": 1}),
        (E.MODEL_REQUEST_END, {}),
        (E.AGENT_MESSAGE, {"text": "done"}),
    )
    messages = fold(noisy)
    res.check("observational events are not folded",
              [m["role"] for m in messages] == ["user", "assistant"],
              f"{[m['role'] for m in messages]}")
    res.check("a started-but-unfinished call alone adds nothing",
              not any(m["role"] == "tool" for m in messages),
              "tool_call.started must not become a turn")

    # ── the invariant ──────────────────────────────────────────────────────

    unresolved = fold(log((E.AGENT_TOOL_CALL, {"calls": [CALL]}),
                          (E.TOOL_CALL_STARTED, {"call_id": "toolu_1"})))
    res.check("an unanswered call is healed, not left dangling",
              unanswered(unresolved) == [], f"dangling: {unanswered(unresolved)}")
    healed = [m for m in unresolved if m.get("call_id") == "toolu_1"]
    res.check("the synthesised result says the outcome is unknown",
              healed and UNKNOWN_OUTCOME in healed[0]["parts"][0]["text"],
              f"{healed}")
    res.check("the healed result follows its own call",
              unresolved.index(healed[0]) == 1,
              "it must sit after the turn that asked, not at the end")

    two = [CALL, {"id": "toolu_2", "name": "write", "arguments": {}}]
    partial = fold(log((E.AGENT_TOOL_CALL, {"calls": two}),
                       (E.TOOL_RESULT, {"call_id": "toolu_2", "result": "ok"})))
    res.check("one answered and one not is still not dangling",
              unanswered(partial) == [], f"dangling: {unanswered(partial)}")

    # A denial is an answer, and it carries its reason: "not approved" with nothing
    # else is the part a person cannot act on.
    denied = fold(log((E.AGENT_TOOL_CALL, {"calls": [CALL]}),
                      (E.APPROVAL_DECIDED, {"call_id": "toolu_1", "approved": False,
                                            "reason": "read the example instead"})))
    res.check("a refusal answers the call", unanswered(denied) == [],
              f"dangling: {unanswered(denied)}")
    res.check("a refusal carries its reason",
              "read the example instead" in denied[1]["parts"][0]["text"],
              f"{denied[1]}")

    approved = fold(log((E.AGENT_TOOL_CALL, {"calls": [CALL]}),
                        (E.APPROVAL_DECIDED, {"call_id": "toolu_1",
                                              "approved": True}),
                        (E.TOOL_RESULT, {"call_id": "toolu_1", "result": "done"})))
    res.check("an approval is not itself a result",
              sum(1 for m in approved if m.get("call_id") == "toolu_1") == 1,
              "an approved call must be answered once, by its own result")

    # ── turn order, not log order ──────────────────────────────────────────
    #
    # A message sent while the model thinks about turn A is committed before A's
    # answer. Folded in log order the answer reads as a reply to both, and the new
    # message looks already dealt with. Found by the worker's tests, kept here
    # because it is a property of the fold.
    def turned(kind, turn, **kw):
        return E.event(kind, turn_id=turn, **kw)
    interleaved = [
        dict(E.event(E.SESSION_CREATED), seq=0),
        dict(turned(E.USER_MESSAGE, "A", text="first"), seq=1),
        dict(turned(E.MODEL_REQUEST_START, "A"), seq=2),
        dict(turned(E.USER_MESSAGE, "B", text="while you think"), seq=3),
        dict(turned(E.AGENT_MESSAGE, "A", text="answer to first"), seq=4),
    ]
    flat = [(m["role"], m["parts"][0]["text"]) for m in fold(interleaved)]
    res.check("each turn is folded whole before the next",
              flat == [("user", "first"), ("assistant", "answer to first"),
                       ("user", "while you think")], f"{flat}")
    upto = [(m["role"], m["parts"][0]["text"])
            for m in fold(interleaved, until_turn="A")]
    res.check("answering a turn does not see later turns",
              upto == [("user", "first"), ("assistant", "answer to first")],
              f"{upto}")

    # ── the invariant, over every prefix of every log ──────────────────────
    #
    # A step reads a log that was cut at an arbitrary point: the invocation before
    # it may have died anywhere. So the invariant has to hold for every prefix, not
    # only for complete logs — which is the case a hand-written example never
    # covers.
    pieces = [
        (E.USER_MESSAGE, {"text": "go"}),
        (E.AGENT_TOOL_CALL, {"calls": two}),
        (E.TOOL_CALL_STARTED, {"call_id": "toolu_1"}),
        (E.TOOL_RESULT, {"call_id": "toolu_1", "result": "a"}),
        (E.TOOL_CALL_STARTED, {"call_id": "toolu_2"}),
        (E.USER_INTERRUPT, {}),
        (E.AGENT_MESSAGE, {"text": "stopped"}),
    ]
    bad_prefixes = []
    for length in range(len(pieces) + 1):
        for order in itertools.permutations(pieces[:length]):
            dangling = unanswered(fold(log(*order)))
            if dangling:
                bad_prefixes.append((length, dangling))
                break
    res.check("no prefix or ordering leaves a dangling call", not bad_prefixes,
              f"{bad_prefixes[:3]}")

    return res.report()


if __name__ == "__main__":
    raise SystemExit(run())
