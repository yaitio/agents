"""
Turns and items: what the OpenAI dialect reads, folded from the same log.

This is where slice one's claim is tested. The log records `turn_id` and `item_id`
on every event although the Anthropic dialect has no notion of either — on the
promise that OpenAI's `/turns` and `/items` would be derivable from it rather than
needing a second store. If this module needed anything the log does not hold, the
promise was wrong and the second façade would be a storage migration.

Both functions are pure. Nothing here is stored: a turn's status is computed from
the events in it every time it is read, so it cannot disagree with the log.
"""

from __future__ import annotations

import json
from typing import Any

import events as E

# ── turns ──────────────────────────────────────────────────────────────────

#: Events that end a turn, and how. Anything else inside a turn leaves it open.
_ENDINGS = {
    E.AGENT_MESSAGE: "completed",      # the agent answered; the user has the floor
    E.STEP_FAILED: "failed",
    E.USER_INTERRUPT: "cancelled",
    E.TURN_CANCELLED: "cancelled",
}


def turns(events: list[dict], *, session_id: str, agent_id: str | None) -> list[dict]:
    """
    One record per turn, in the order the turns began.

    A turn begins with the first event that carries its `turn_id` — the user's
    message — and its status is the last thing that happened in it:

    * only input so far, and nothing has taken it up: `queued`;
    * work has started (a model request, a tool call): `in_progress`;
    * the agent is waiting on a person or a client-side tool: `waiting`;
    * the agent answered: `completed`; a step failed: `failed`; interrupted:
      `cancelled`.
    """
    final: dict[str, dict] = {}
    for _, turn in _walk(events, session_id, agent_id):
        final[turn["turn_id"]] = turn
    return sorted(final.values(), key=lambda t: t["first_seq"])


def turn_timeline(events: list[dict], *, session_id: str,
                  agent_id: str | None) -> dict[str, dict]:
    """
    `{event_id: the turn as it stood just after that event}`, for every event that
    belongs to a turn.

    OpenAI's turn events carry the turn *at that moment* — `turn.created` a queued
    one, `turn.in_progress` one with a start time — so the rendering needs the
    history, not only the outcome. It is the same walk as `turns()`, kept at every
    step rather than only the last.
    """
    return {ev["event_id"]: turn for ev, turn in _walk(events, session_id, agent_id)}


def _walk(events: list[dict], session_id: str, agent_id: str | None):
    """Each event in a turn, with a copy of its turn's state after it."""
    by_turn: dict[str, dict] = {}
    for ev in events:
        turn_id = ev.get("turn_id")
        if not turn_id:
            continue
        turn = by_turn.get(turn_id)
        if turn is None:
            turn = by_turn[turn_id] = {
                "turn_id": turn_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "status": "queued",
                "created_at": ev.get("ts"),
                "started_at": None,
                "completed_at": None,
                "error": None,
                "first_seq": ev.get("seq", 0),
                "first_event": ev.get("event_id"),
                "opened_by_running": ev.get("type") == E.STATUS_RUNNING,
                "messages": 0,
                "outputs": 0,
            }
        kind = ev.get("type")
        if kind in _ENDINGS:
            turn["status"] = _ENDINGS[kind]
            turn["completed_at"] = ev.get("ts")
            if kind == E.STEP_FAILED:
                turn["error"] = ev.get("reason") or "the step failed"
                turn["error_type"] = ev.get("error_type") or "unknown"
        elif kind in (E.MODEL_REQUEST_START, E.AGENT_TOOL_CALL, E.TOOL_CALL_STARTED):
            if turn["status"] == "queued":
                turn["status"] = "in_progress"
                turn["started_at"] = ev.get("ts")
                turn["started_by"] = ev.get("event_id")
        elif kind == E.APPROVAL_REQUESTED:
            turn["status"] = "waiting"
        if kind == E.USER_MESSAGE:
            turn["messages"] += 1
        # What the agent produced, counted: an output's place in its turn is the
        # dialect's `output_index`.
        if kind in (E.AGENT_MESSAGE, E.AGENT_TOOL_CALL):
            turn["outputs"] += 1
        yield ev, dict(turn)


def is_running(events: list[dict]) -> bool:
    """
    Whether the session has said it is running and not yet that it is idle — read
    from the status events, so that "running" is announced once per stretch of work
    however many turns it takes, and by whichever of the API and the worker gets
    there first.
    """
    for ev in reversed(events):
        if ev.get("type") == E.STATUS_RUNNING:
            return True
        if ev.get("type") == E.STATUS_IDLE:
            return False
    return False


def processed_times(events: list[dict]) -> dict[str, int]:
    """
    When each unprocessed input was taken up: the start of its turn's first model
    request — or, for a turn the worker ended without one (a budget already spent,
    an agent that no longer exists), the moment it said so.

    Derived rather than written back, so the log stays append-only. A message is
    committed with processed_at null and nothing ever rewrites it; the moment a
    worker starts on its turn is itself an event, and that is when the message was
    processed.
    """
    started: dict[str, int] = {}
    for ev in events:
        if ev.get("type") in (E.MODEL_REQUEST_START, E.STEP_FAILED) \
                and ev.get("turn_id") not in started:
            started[ev["turn_id"]] = ev.get("ts")
    return {ev["event_id"]: started[ev["turn_id"]]
            for ev in events
            if ev.get("processed_at") is None and ev.get("turn_id") in started}


def unfinished_turns(events: list[dict]) -> list[str]:
    """Every turn not yet ended, earliest first — what an interrupt stops."""
    return [t["turn_id"] for t in turns(events, session_id="", agent_id=None)
            if t["status"] not in ("completed", "failed", "cancelled")]


def open_turn(events: list[dict]) -> str | None:
    """
    The turn a new message should join, if one is still waiting to be taken up.

    Messages sent while nothing has picked up the previous one join it, so the agent
    answers them as one turn — which is what the Anthropic dialect documents for
    rapid follow-ups. A turn that has started, or ended, is not joined.
    """
    for turn in reversed(turns(events, session_id="", agent_id=None)):
        return turn["turn_id"] if turn["status"] == "queued" else None
    return None


def current_turn(events: list[dict]) -> str | None:
    """
    The turn an interrupt should end: the last one, if it has not already ended.

    Not the same as `open_turn`. A new message may only *join* a turn nothing has
    taken up; an interrupt must be able to stop one that is under way.
    """
    for turn in reversed(turns(events, session_id="", agent_id=None)):
        return turn["turn_id"] if turn["status"] not in (
            "completed", "failed", "cancelled") else None
    return None


# ── items ──────────────────────────────────────────────────────────────────

def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def items(events: list[dict]) -> list[dict]:
    """
    Messages, calls and results, one record each, in log order.

    Only committed facts become items — the same boundary the fold draws, and for
    the same reason: a delta, a span or a call's *started* record is not something
    the conversation contains.
    """
    out: list[dict] = []
    for ev in events:
        kind = ev.get("type")
        base = {"turn_id": ev.get("turn_id"), "seq": ev.get("seq", 0)}

        if kind == E.USER_MESSAGE:
            out.append({**base, "type": "message", "id": ev["item_id"],
                        "role": "user", "text": _text(ev.get("text", "")),
                        "status": "completed"})

        elif kind == E.AGENT_MESSAGE:
            out.append({**base, "type": "message", "id": ev["item_id"],
                        "role": "assistant", "text": _text(ev.get("text", "")),
                        "status": "completed"})

        elif kind == E.AGENT_TOOL_CALL:
            # One event may carry several calls; each is an item of its own, and
            # each needs an id that stays the same on every read.
            for n, call in enumerate(ev.get("calls") or []):
                if call.get("server"):
                    # A call to an MCP server is one item, call and result
                    # together: it opens here and its result completes it.
                    out.append({**base, "type": "mcp_call", "id": f"{ev['item_id']}_{n}",
                                "call_id": call["id"], "server_label": call["server"],
                                "name": call["name"],
                                "arguments": json.dumps(call.get("arguments") or {},
                                                        ensure_ascii=False),
                                "output": None, "error": None, "status": "in_progress"})
                    continue
                out.append({**base, "type": "function_call",
                            "id": f"{ev['item_id']}_{n}",
                            "call_id": call["id"], "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {},
                                                    ensure_ascii=False),
                            "status": "completed"})

        elif kind == E.TOOL_RESULT and ev.get("server"):
            for made in out:
                if made["type"] == "mcp_call" and made["call_id"] == ev.get("call_id"):
                    failed = bool(ev.get("is_error"))
                    made.update(output=None if failed else _text(ev.get("result", "")),
                                error=_text(ev.get("result", "")) if failed else None,
                                status="failed" if failed else "completed")

        elif kind in (E.TOOL_RESULT, E.USER_TOOL_RESULT):
            out.append({**base, "type": "function_call_output", "id": ev["item_id"],
                        "call_id": ev.get("call_id"),
                        "output": _text(ev.get("result", "")),
                        "error": ev.get("error"), "status": "completed"})
    return out
