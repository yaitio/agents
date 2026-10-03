"""
Events to messages. A pure function, and the most dangerous code here.

The event log is the only source of truth for what happened, so the conversation
the model sees is folded from it on every step rather than stored as a second copy.
The alternative — saving the whole conversation each step — puts a growing document
under DynamoDB's 400 KB item limit and makes the log decorative.

**If the fold drifts, the agent's memory is wrong and nothing raises.** No status
turns red, no request fails; the model simply reasons over a conversation that did
not happen. That is why this is pure, why it is tested on its own, and why it is
the one function whose test asserts an invariant rather than an output.

The invariant: **the fold never returns a history with a tool call nobody
answered.** `Agent.step()` raises `ValueError` on one, and it is right to — three
providers reject such a history, Google is looser, and a self-hosted
OpenAI-compatible server templates it through so the model meets its own unanswered
call and improvises differently each time. So a call whose result never arrived
gets a synthesised one saying the outcome is unknown, rather than being left out.
"""

from __future__ import annotations

import json
from typing import Any

import events as E

#: What a tool call that was started and never finished is answered with. It is
#: deliberately uncomfortable: a side effect of unknown outcome is never silently
#: repeated, and never silently recorded as a clean failure either. The model can
#: act on "this may have happened"; it cannot act on a lie.
UNKNOWN_OUTCOME = (
    "The outcome of this call is unknown: it was started and the step did not "
    "finish. It may or may not have taken effect. Do not assume either; if it "
    "matters, check before acting."
)


def _text_part(value: Any) -> dict:
    if isinstance(value, str):
        return {"type": "text", "text": value}
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}


def _in_turn_order(events: list[dict], until_turn: str | None) -> list[dict]:
    """
    The log regrouped by turn, turns in the order they began.

    **Log order is not conversation order.** A message sent while the model is
    thinking about turn A lands in the log *before* A's answer — it was committed
    first. Folded in log order, the model sees two user messages and then one reply,
    and the reply reads as an answer to both: the new message looks already dealt
    with. Nothing raises. The agent simply misremembers.

    So each turn is emitted whole — its input, then its output — before the next.

    `until_turn` stops after that turn. Answering an earlier turn must not see later
    ones: a worker resuming an orphaned turn A, with turn B queued behind it, would
    otherwise answer A with B's words already in view.

    Events without a turn keep their log order, as one group.
    """
    groups: dict = {}
    for ev in events:
        groups.setdefault(ev.get("turn_id"), []).append(ev)
    out: list[dict] = []
    for turn_id, group in groups.items():
        out.extend(group)
        if until_turn is not None and turn_id == until_turn:
            break
    return out


def fold(events: list[dict], *, instructions: str | None = None,
         until_turn: str | None = None) -> list[dict]:
    """
    The conversation, in the shape `Agent.step()` expects.

    `events` is the session's log in sequence order. Events outside `E.FOLDED` are
    skipped: deltas, spans, a call's *started* record, status changes. Only
    committed facts carry meaning. They are folded turn by turn — see
    `_in_turn_order` for why log order would be wrong.
    """
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "parts": [instructions]})

    # Every call the agent asked for, and every call something answered. The two
    # are reconciled at the end, because a result may arrive several events after
    # its call and an interrupt may land in between.
    asked: list[tuple[int, str]] = []   # (index of the assistant turn, call id)
    answered: set[str] = set()

    for ev in _in_turn_order(events, until_turn):
        kind = ev.get("type")
        if kind not in E.FOLDED:
            continue

        if kind == E.USER_MESSAGE:
            messages.append({"role": "user",
                             "parts": [_text_part(ev.get("text", ""))]})

        elif kind == E.AGENT_MESSAGE:
            messages.append({"role": "assistant",
                             "parts": [_text_part(ev.get("text", ""))]})

        elif kind == E.AGENT_TOOL_CALL:
            calls = ev.get("calls") or []
            turn: dict = {"role": "assistant",
                          "tool_calls": [
                              {"id": c["id"], "name": c["name"],
                               "arguments": c.get("arguments") or {}}
                              for c in calls
                          ]}
            if ev.get("text"):
                turn["parts"] = [_text_part(ev["text"])]
            messages.append(turn)
            for c in calls:
                asked.append((len(messages) - 1, c["id"]))

        elif kind in (E.TOOL_RESULT, E.USER_TOOL_RESULT):
            call_id = ev.get("call_id")
            if not call_id:
                continue
            messages.append({"role": "tool", "call_id": call_id,
                             "parts": [_text_part(ev.get("result", ""))]})
            answered.add(call_id)

        elif kind == E.APPROVAL_DECIDED:
            # A refusal is a result, and it carries its reason. "Not approved" with
            # nothing else is the part a person cannot act on.
            call_id = ev.get("call_id")
            if not call_id:
                continue
            if ev.get("approved"):
                continue          # the call proceeds; its own result will follow
            reason = ev.get("reason") or "The owner did not approve this call."
            messages.append({"role": "tool", "call_id": call_id,
                             "parts": [_text_part(reason)]})
            answered.add(call_id)

        elif kind == E.USER_INTERRUPT:
            if not ev.get("turn_id"):
                continue          # an interrupt with nothing in flight stopped nothing
            messages.append({"role": "user", "parts": [_text_part(
                "The user interrupted. Stop what you were doing and wait.")]})

    # Heal, in place, right after the turn that asked — so the result follows its
    # call rather than landing at the end of a conversation that moved on.
    for index, call_id in sorted(asked, reverse=True):
        if call_id in answered:
            continue
        messages.insert(index + 1, {"role": "tool", "call_id": call_id,
                                    "parts": [_text_part(UNKNOWN_OUTCOME)]})

    return messages


def unanswered(messages: list[dict]) -> list[str]:
    """
    Tool call ids no later turn answers — the check `Agent.step()` performs.

    Mirrored here so the fold's own tests can assert the invariant without the
    library on the path. It becomes `from yait_aichain.models import dangling_calls`
    when the step worker takes that dependency, and the two are compared then.
    """
    asked: list[str] = []
    answered: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            asked.append(call["id"])
        if message.get("role") == "tool" and message.get("call_id"):
            answered.add(message["call_id"])
    return [c for c in asked if c not in answered]
