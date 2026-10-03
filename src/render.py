"""
One record, two shapes. This is the whole of what a façade is.

Everything above this module works on internal records; everything a client sees
passes through here. The split is the claim api.md makes — shared inside, a
serializer per dialect — and this is where it is either true or it is not.

The differences are not cosmetic. They are, for the same agent:

| | Anthropic | OpenAI |
|---|---|---|
| version | exposed, and a session pins one | no versions endpoint at all |
| system prompt | `system` | `instructions` |
| timestamps | ISO-8601 strings | integer Unix seconds |
| type marker | none | `object: "agent"` |

So a renderer that merely renamed fields would be wrong about two of those.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

ANTHROPIC = "anthropic"
OPENAI = "openai"


def iso(ms: int | float | None) -> str | None:
    """Milliseconds to ISO-8601 with a `Z`, which is what the Anthropic SDK parses."""
    if ms is None:
        return None
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def seconds(ms: int | float | None) -> int | None:
    """Milliseconds to whole Unix seconds, which is what OpenAI's objects carry."""
    return None if ms is None else int(ms // 1000)


def _drop_none(item: dict) -> dict:
    return {k: v for k, v in item.items() if v is not None}


# ── agents ─────────────────────────────────────────────────────────────────

def agent(record: dict, dialect: str) -> dict:
    if dialect == ANTHROPIC:
        return _drop_none({
            "id": record["agent_id"],
            "type": "agent",
            # Required by the dialect even when nobody named the agent.
            "name": record.get("name") or "",
            # An object on the way out, though a bare string is accepted on the way
            # in: the request's shorthand is not the resource's shape.
            "model": {"id": record.get("model")},
            # The dialect calls the system prompt `system`; we store the OpenAI
            # spelling because it is the one that also reads as prose.
            "system": record.get("instructions"),
            "tools": [_anthropic_tool(t) for t in record.get("tools") or []],
            "mcp_servers": [{"type": "url", "name": s.get("name"), "url": s.get("url")}
                            for s in record.get("mcp_servers") or []],
            "skills": record.get("skills") or [],
            "metadata": record.get("metadata") or {},
            "version": record.get("version"),
            "created_at": iso(record.get("created_at")),
            "updated_at": iso(record.get("updated_at")),
            "archived_at": iso(record.get("archived_at")),
        })

    if dialect == OPENAI:
        # Every field present, null included: the dialect requires them all, and
        # a nullable field left out is a different document from one set to null.
        return {
            "id": record["agent_id"],
            # A constant discriminator, which their SDKs use to pick a type. Its
            # absence would not be a cosmetic difference: a discriminated union
            # with an unknown value raises.
            "object": "agent",
            "created_at": seconds(record.get("created_at")),
            "updated_at": seconds(record.get("updated_at")),
            "name": record.get("name"),
            "model": record.get("model"),
            "instructions": record.get("instructions"),
            "tools": [_openai_tool(t) for t in record.get("tools") or []],
            "metadata": record.get("metadata") or {},
            # The effective configuration, which is what the resource reports:
            # none of these is configurable here yet, so each reads as the
            # dialect's own default — which is what the model is actually run with.
            "reasoning": {"effort": None, "summary": None},
            "text": {"format": {"type": "text"}, "verbosity": "medium"},
            "service_tier": "auto",
            "multi_agent": {"enabled": False, "max_concurrent_subagents": None},
            # Deliberately absent: `version`. The dialect has no versions endpoint
            # and no version field, so showing one would invent a surface its
            # clients cannot use. We still store it, because the other dialect
            # pins it.
        }

    raise ValueError(f"unknown dialect {dialect!r}")


#: Every call runs without asking: approvals are not served yet, so a policy that
#: asks is refused when the agent is made (tools.check) and this is the one shown.
_ALLOW = {"type": "always_allow"}


def _anthropic_tool(tool: dict) -> dict:
    """A tool as the resource shows it: every default spelled out."""
    if tool.get("type") != "mcp_toolset":
        return tool
    default = tool.get("default_config") or {}
    return {"type": "mcp_toolset", "mcp_server_name": tool.get("mcp_server_name"),
            "default_config": {"enabled": default.get("enabled", True),
                               "permission_policy": _ALLOW},
            "configs": [{"name": c.get("name"), "enabled": c.get("enabled", True),
                         "permission_policy": _ALLOW}
                        for c in tool.get("configs") or []]}


def _openai_tool(tool: dict) -> dict:
    if tool.get("type") != "mcp":
        return tool
    transport = tool.get("transport") or {}
    return {"type": "mcp", "server_label": tool.get("server_label"),
            "credential_id": tool.get("credential_id"),
            "transport": {"type": "http",
                          "server_url": transport.get("server_url") or tool.get("server_url"),
                          "headers": transport.get("headers") or {}},
            "request_metadata": tool.get("request_metadata") or {},
            "allowed_tools": tool.get("allowed_tools"),
            "required": bool(tool.get("required", False)),
            "connection_origin": tool.get("connection_origin") or "service"}


def agent_list(records: list[dict], dialect: str, *,
               next_page: str | None = None,
               prev_page: str | None = None) -> dict:
    items = [agent(r, dialect) for r in records]
    if dialect == ANTHROPIC:
        # Cursor pagination, and `order` is encoded inside the cursor — reusing one
        # with a different order is a 400. See docs/api.md.
        return _drop_none({"data": items, "has_more": bool(next_page),
                           "next_page": next_page, "prev_page": prev_page})
    return _drop_none({"object": "list", "data": items,
                       "has_more": bool(next_page),
                       "first_id": items[0]["id"] if items else None,
                       "last_id": items[-1]["id"] if items else None})


def agent_versions(records: list[dict]) -> dict:
    """
    Anthropic only: a page of **full agents**, one per version.

    Not a slimmer "version" object — the official SDK types this endpoint as a page
    of agents, and a client reads each entry as one. Each record here is the agent as
    it was at that version.
    """
    return {"data": [agent(r, ANTHROPIC) for r in records], "next_page": None}


# ── errors ─────────────────────────────────────────────────────────────────

# A failure kind (events.FAILURES) in each dialect's own words.
_ANTHROPIC_FAILURE = {"billing": "billing_error",
                      "mcp_authentication": "mcp_authentication_failed_error",
                      "mcp_connection": "mcp_connection_failed_error",
                      "rate_limited": "model_rate_limited_error",
                      "overloaded": "model_overloaded_error",
                      "unknown": "unknown_error"}     # the rest: the request failed
_OPENAI_FAILURE = {"budget": "session_budget_exceeded",
                   "billing": "credit_balance_exhausted",
                   "rate_limited": "rate_limit_exceeded",
                   "overloaded": "server_overloaded",
                   "authentication": "authentication_error",
                   "invalid_request": "invalid_request",
                   "not_found": "resource_not_found",
                   "connection_failed": "connection_failed",
                   "mcp_authentication": "authentication_error",
                   "mcp_connection": "connection_failed",
                   "server_error": "server_error"}    # the rest: internal_error


def failure(kind: str | None, dialect: str) -> str:
    kind = kind or "unknown"
    if dialect == ANTHROPIC:
        return _ANTHROPIC_FAILURE.get(kind, "model_request_failed_error")
    return _OPENAI_FAILURE.get(kind, "internal_error")


def error(status: int, type_: str, message: str, dialect: str) -> dict:
    """
    The two dialects disagree about the envelope, and a client parses one of them.

    Anthropic: `{"type": "error", "error": {"type", "message"}}`.
    OpenAI: `{"error": {"message", "type", "param", "code"}}`.
    """
    if dialect == OPENAI:
        # `code` is a required string, never null — and the original sends the
        # type again in it ("not_found_error"), so that is what a client matches on.
        return {"error": {"message": message, "type": type_,
                          "param": None, "code": type_}}
    return {"type": "error", "error": {"type": type_, "message": message}}


# ── sessions ───────────────────────────────────────────────────────────────
#
# Status is the sharpest divergence between the dialects: four values each, one
# name in common, and each loses something the other keeps. The internal state is
# richer than both, and these two tables are the lossy projections. See
# docs/architecture.md.

_ANTHROPIC_STATUS = {
    "queued": "running",        # the dialect: idle -> running on receiving an event
    "working": "running",
    "between_steps": "running",
    "owes_answer": "idle",      # with stop_reason requires_action, on the event
    "turn_over": "idle",
    "at_ceiling": "idle",       # with stop_reason budget_reached
    "failed": "terminated",
    "archived": "terminated",   # finishing is idle; terminated is error or archive
}

_OPENAI_STATUS = {
    "queued": "in_progress",
    "working": "in_progress",
    "between_steps": "in_progress",
    "owes_answer": "requires_action",
    "turn_over": "idle",
    "at_ceiling": "idle",
    "failed": "failed",
    "archived": "idle",         # no counterpart: OpenAI has delete, not archive
}


_SESSION_AGENT = ("id", "name", "model", "reasoning", "text", "service_tier",
                  "instructions", "tools", "multi_agent")

def list_cost(record: dict) -> dict | None:
    """What the session has spent at list prices, in minor units, or None if unknown."""
    spent = (record.get("agent_state") or {}).get("cost")
    if spent is None:
        return None
    cents = (Decimal(str(spent)) * 100).to_integral_value(ROUND_HALF_UP)
    return {"amount": str(int(cents)), "currency": "USD"}


#: The environment id of a session that was opened without one.
NO_ENVIRONMENT = "env_none"


def session(record: dict, dialect: str, agent_record: dict | None) -> dict:
    status = record.get("status", "turn_over")
    if agent_record and record.get("agent_overrides"):
        # The agent as this session runs it: the pinned version, with what the
        # session changed on top.
        agent_record = {**agent_record, **record["agent_overrides"]}
    if dialect == ANTHROPIC:
        return _drop_none({
            "type": "session",
            "id": record["session"],
            "title": record.get("title"),
            "status": _ANTHROPIC_STATUS.get(status, "idle"),
            # Required by the dialect. A session opened through the OpenAI façade has
            # no environment id — its environment is only a type — and the same
            # tenant lists it here too, so it reads as the one environment it has.
            "environment_id": (record.get("environment") or {}).get("id")
                              or NO_ENVIRONMENT,
            "agent": agent(agent_record, ANTHROPIC) if agent_record else None,
            "resources": [],
            "vault_ids": list(record.get("vault_ids") or []),
            "outcome_evaluations": [],
            # Required, and every field in them optional. Only the list cost is
            # tracked yet — it is what a budget is measured against — and the
            # rest stays absent rather than a sum that is quietly wrong.
            "usage": _drop_none({"list_cost": list_cost(record)}),
            "budget": record.get("budget"),
            "stats": {},
            "metadata": record.get("metadata") or {},
            "created_at": iso(record.get("created_at")),
            "updated_at": iso(record.get("updated_at")),
            "archived_at": iso(record.get("archived_at")),
        })
    if dialect == OPENAI:
        body = {
            "id": record["session"],
            "object": "agent.session",
            "created_at": seconds(record.get("created_at")),
            "last_active_at": seconds(record.get("updated_at")),
            "status": _OPENAI_STATUS.get(status, "idle"),
            "required_actions": [],
            "error": None,
            "environment": {"type": "none"},
            "vault_ids": list(record.get("vault_ids") or []),
            "usage": None,
            "metadata": record.get("metadata") or {},
        }
        if agent_record:
            # The agent's configuration as the session runs it: the agent resource
            # without its bookkeeping — no object, timestamps or metadata.
            full = agent(agent_record, OPENAI)
            body["agent"] = {k: full[k] for k in _SESSION_AGENT}
            # The session's view of an MCP tool is the plain agent tool: its
            # transport carries no headers there.
            body["agent"]["tools"] = [
                {**t, "transport": {"type": "http",
                                    "server_url": t["transport"]["server_url"]}}
                if t.get("type") == "mcp" else t for t in body["agent"]["tools"]]
        return body
    raise ValueError(f"unknown dialect {dialect!r}")


# ── events ─────────────────────────────────────────────────────────────────

def event(ev: dict, dialect: str, *, session_id: str,
          processed: dict[str, int] | None = None,
          session: dict | None = None,
          timeline: dict[str, dict] | None = None) -> list[dict]:
    """
    One internal event to zero or more dialect events.

    Zero, because some internal events have no counterpart in a dialect —
    `session.created` is not an Anthropic event at all. More than one is possible
    too, which is why this returns a list: a projection is not a renaming.

    Many OpenAI events carry a whole resource as it stood at that moment — the
    session on its status events, the turn on its lifecycle events — so the caller
    passes the session, rendered, and the turn timeline (project.turn_timeline).
    """
    kind = ev.get("type")
    if dialect == ANTHROPIC:
        at = ev.get("processed_at") or (processed or {}).get(ev.get("event_id"))
        stamp = iso(at) if at else None
        if kind in ("user.message", "agent.message"):
            return [{"id": ev["event_id"], "type": kind, "processed_at": stamp,
                     "content": [{"type": "text", "text": ev.get("text", "")}]}]
        if kind == "user.interrupt":
            return [{"id": ev["event_id"], "type": kind, "processed_at": stamp}]
        if kind == "agent.tool_call":
            # What the model said before calling, then each call. A call's own id
            # is its event id: the result names it as `mcp_tool_use_id`.
            out = []
            if ev.get("text"):
                out.append({"id": f"{ev['event_id']}_text", "type": "agent.message",
                            "processed_at": stamp,
                            "content": [{"type": "text", "text": ev["text"]}]})
            for call in ev.get("calls") or []:
                out.append({"id": call["id"], "type": "agent.mcp_tool_use",
                            "name": call["name"], "input": call.get("arguments") or {},
                            "mcp_server_name": call.get("server") or "",
                            "processed_at": stamp})
            return out
        if kind == "tool_result" and ev.get("late"):
            return []        # finished after the turn was interrupted: kept, not shown
        if kind == "tool_result":
            return [{"id": ev["event_id"], "type": "agent.mcp_tool_result",
                     "mcp_tool_use_id": ev.get("call_id", ""),
                     "content": [{"type": "text", "text": ev.get("result", "")}],
                     "is_error": bool(ev.get("is_error")), "processed_at": stamp}]
        if kind in ("span.model_request_start", "session.status_running",
                    "agent.thinking"):
            return [{"id": ev["event_id"], "type": kind, "processed_at": stamp}]
        if kind == "session.status_idle":
            return [{"id": ev["event_id"], "type": kind, "processed_at": stamp,
                     "stop_reason": {"type": ev.get("stop_reason", "end_turn")}}]
        if kind == "span.model_request_end":
            usage = ev.get("usage") or {}
            return [{"id": ev["event_id"], "type": kind, "processed_at": stamp,
                     "model_request_start_id": ev.get("model_request_start_id", ""),
                     "is_error": bool(ev.get("error")),
                     "model_usage": {
                         "input_tokens": usage.get("input_tokens", 0),
                         "output_tokens": usage.get("output_tokens", 0),
                         "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                         "cache_creation_input_tokens":
                             usage.get("cache_creation_input_tokens", 0)}}]
        if kind == "session.usage":
            snapshot = {k: ev.get(k) for k in ("input_tokens", "output_tokens",
                                                "cache_read_input_tokens")}
            spent = list_cost({"agent_state": {"cost": ev.get("cost_usd")}})
            return [{"id": ev["event_id"], "type": kind,
                     "processed_at": stamp or iso(ev["ts"]),
                     "usage": _drop_none({**snapshot, "list_cost": spent})}]
        if kind == "step.failed" and ev.get("error_type") == "budget":
            # Said by the idle event's stop_reason, `budget_reached`, which is
            # how the dialect reports it; it is not an error.
            return []
        if kind == "step.failed":
            # `exhausted`: the turn is over and the session stays usable — the
            # dialect's `terminal` would say the session itself is finished.
            error = {"type": failure(ev.get("error_type"), ANTHROPIC),
                     "message": ev.get("reason", ""),
                     "retry_status": {"type": "exhausted"}}
            if (ev.get("error_type") or "").startswith("mcp_"):
                error["mcp_server_name"] = ev.get("mcp_server") or ""
            return [{"id": ev["event_id"], "type": "session.error",
                     "processed_at": stamp or iso(ev["ts"]), "error": error}]
        return []

    if dialect == OPENAI:
        out = _openai_event(ev, session_id, session, timeline or {})
        # One internal event may become several here, and each needs its own id —
        # `after` pages by it. The first keeps the log's id; the rest derive from it.
        for n, rendered in enumerate(out):
            rendered["event_id"] = ev["event_id"] if n == 0 else f"{ev['event_id']}_{n}"
        return out
    raise ValueError(f"unknown dialect {dialect!r}")


def events(evs: list[dict], dialect: str, *, session_id: str,
           processed: dict[str, int] | None = None,
           session: dict | None = None,
           timeline: dict[str, dict] | None = None) -> list[dict]:
    if dialect == OPENAI:
        evs = _with_mcp_items(evs)
    return [out for ev in evs
            for out in event(ev, dialect, session_id=session_id, processed=processed,
                             session=session, timeline=timeline)]


def _with_mcp_items(evs: list[dict]) -> list[dict]:
    """
    Each MCP result, carrying the whole `mcp_call` item it completes: OpenAI closes
    the item with the call and its output together, and the two are separate
    events in the log.
    """
    calls: dict[str, dict] = {}
    placed: dict[str, int] = {}          # per turn: output items so far
    out = []
    for ev in evs:
        if ev.get("type") == "agent.tool_call":
            indexes = []
            for n, call in enumerate(ev.get("calls") or []):
                index = placed.get(ev.get("turn_id"), 0)
                placed[ev.get("turn_id")] = index + 1
                indexes.append(index)
                calls[call["id"]] = {"id": f"{ev['item_id']}_{n}", "index": index,
                                     "arguments": json.dumps(call.get("arguments") or {},
                                                             ensure_ascii=False)}
            ev = {**ev, "output_indexes": indexes}
        if ev.get("type") == "tool_result" and ev.get("server"):
            made = calls.get(ev.get("call_id"),
                             {"id": ev.get("item_id"), "arguments": "{}", "index": 0})
            failed = bool(ev.get("is_error"))
            ev = {**ev, "mcp_index": made["index"], "mcp_item": {
                "type": "mcp_call", "id": made["id"], "turn_id": ev.get("turn_id"),
                "server_label": ev["server"], "name": ev.get("name", ""),
                "arguments": made["arguments"],
                "status": "failed" if failed else "completed",
                "output": None if failed else ev.get("result", ""),
                "error": ev.get("result", "") if failed else None}}
        out.append(ev)
    return out


_TURN_ENDED = {"completed": "agent.session.turn.completed",
               "failed": "agent.session.turn.failed",
               "cancelled": "agent.session.turn.cancelled"}


def _openai_event(ev: dict, session_id: str, session: dict | None,
                  timeline: dict[str, dict]) -> list[dict]:
    """
    One internal event as OpenAI's session events, in the order they happened.

    The turn's lifecycle is read from the timeline rather than from the event's
    type, so it cannot disagree with `/turns`: the event that opened a turn emits
    `turn.created`, the one that started it `turn.in_progress`, the one that ended
    it `turn.completed`, `failed` or `cancelled` — each with the turn as it stood
    just then.
    """
    kind = ev.get("type")
    state = timeline.get(ev.get("event_id"))
    out: list[dict] = []

    def turn_event(type_: str, **extra) -> dict:
        return {"type": type_, "session_id": session_id, "turn_id": state["turn_id"],
                "turn": turn(state), **extra}

    def session_event(type_: str, status: str) -> dict | None:
        if session is None:
            return None
        # The session as it stood then: its status, and the moment. The rest of
        # it is not kept historically, so it reads as it is now.
        return {"type": type_, "session": {**session, "status": status,
                                           "last_active_at": seconds(ev.get("ts"))}}

    if state is not None and state.get("first_event") == ev.get("event_id"):
        opened = dict(state, status="queued", started_at=None, completed_at=None,
                      error=None)
        out.append({"type": "agent.session.turn.created", "session_id": session_id,
                    "turn_id": state["turn_id"], "turn": turn(opened)})

    if kind == "session.created":
        made = session_event("agent.session.created", "idle")
        out += [made] if made else []
    elif kind == "session.status_running" and not (
            state is not None and state.get("first_event") == ev.get("event_id")):
        # Running for a turn that is already open: said here. For a turn it opens,
        # the original says it after the message that set it going — below.
        running = session_event("agent.session.in_progress", "in_progress")
        out += [running] if running else []

    if state is not None and state.get("started_by") == ev.get("event_id"):
        out.append(turn_event("agent.session.turn.in_progress"))

    if kind in ("user.message", "agent.message"):
        user = kind == "user.message"
        text = ev.get("text", "")
        item = message_item({"id": ev["item_id"], "turn_id": ev.get("turn_id"),
                             "role": "user" if user else "assistant",
                             "text": text, "status": "completed"})
        # Input has no place in the turn's output; what the agent says does.
        index = None if user or state is None else state["outputs"] - 1
        where = {"session_id": session_id, "turn_id": ev.get("turn_id")}
        if user:
            out.append({"type": "agent.session.turn.item.added", **where,
                        "output_index": index, "item": item})
            if state is not None and state.get("opened_by_running") \
                    and state.get("messages") == 1:
                running = session_event("agent.session.in_progress", "in_progress")
                out += [running] if running else []
        else:
            # The original's sequence for an answer: the item opens empty, its one
            # text part is added, the text arrives — in deltas when it streams; we
            # have it whole, so in one — and each closes. Clients that render as
            # text arrives key on the deltas; clients that wait key on item.done.
            part = {"item_id": ev["item_id"], "output_index": index, "content_index": 0}
            out += [
                {"type": "agent.session.turn.item.added", **where, "output_index": index,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "agent.session.turn.content_part.added", **where, **part,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "agent.session.turn.output_text.delta", **where, **part,
                 "delta": text},
                {"type": "agent.session.turn.output_text.done", **where, **part,
                 "text": text},
                {"type": "agent.session.turn.content_part.done", **where, **part,
                 "part": {"type": "output_text", "text": text}},
                {"type": "agent.session.turn.item.done", **where, "output_index": index,
                 "item": item},
            ]

    if kind == "agent.tool_call":
        where = {"session_id": session_id, "turn_id": ev.get("turn_id")}
        for n, call in enumerate(ev.get("calls") or []):
            if call.get("server"):
                out.append({"type": "agent.session.turn.item.added", **where,
                            "output_index": (ev.get("output_indexes") or [n] * (n + 1))[n],
                            "item": _as_item({
                                "type": "mcp_call", "id": f"{ev['item_id']}_{n}",
                                "turn_id": ev.get("turn_id"),
                                "server_label": call["server"], "name": call["name"],
                                "arguments": json.dumps(call.get("arguments") or {},
                                                        ensure_ascii=False),
                                "status": "in_progress", "output": None, "error": None})})
    if kind == "tool_result" and ev.get("server") and ev.get("mcp_item") \
            and not ev.get("late"):
        where = {"session_id": session_id, "turn_id": ev.get("turn_id")}
        out.append({"type": "agent.session.turn.item.done", **where,
                    "output_index": ev.get("mcp_index", 0), "item": _as_item(ev["mcp_item"])})

    if state is not None and kind in ("agent.message", "step.failed", "user.interrupt",
                                      "turn.cancelled") \
            and state["status"] in _TURN_ENDED:
        out.append(turn_event(_TURN_ENDED[state["status"]], usage=None))

    if kind == "session.status_idle":
        waiting = ev.get("stop_reason") == "requires_action"
        idle = session_event(
            "agent.session.requires_action" if waiting else "agent.session.idle",
            "requires_action" if waiting else "idle")
        out += [idle] if idle else []
    return out


# ── turns and items (OpenAI only — the other dialect has neither) ──────────

def turn(record: dict) -> dict:
    return {
        "id": record["turn_id"],
        "object": "agent.session.turn",
        "session_id": record["session_id"],
        "agent_id": record.get("agent_id"),
        "subagent_id": None,
        "status": record["status"],
        "created_at": seconds(record.get("created_at")),
        "started_at": seconds(record.get("started_at")),
        "completed_at": seconds(record.get("completed_at")),
        "error": ({"code": failure(record.get("error_type"), OPENAI),
                   "message": record["error"]} if record.get("error") else None),
        "usage": None,
    }


def message_item(record: dict) -> dict:
    # The part type follows the author: what a user sent is input_text, what the
    # agent wrote is output_text. Their SDKs dispatch on it.
    part = "input_text" if record["role"] == "user" else "output_text"
    return {"type": "message", "id": record["id"], "turn_id": record.get("turn_id"),
            "role": record["role"],
            "content": [{"type": part, "text": record.get("text", "")}],
            "status": record.get("status", "completed"),
            # What the agent says ends its turn, so it is the final answer; a
            # user's message has no phase.
            "phase": "final_answer" if record["role"] == "assistant" else None}


def item(record: dict) -> dict:
    if record["type"] == "message":
        return message_item(record)
    if record["type"] == "mcp_call":
        return {k: record.get(k) for k in
                ("type", "id", "turn_id", "server_label", "name", "arguments",
                 "status", "output", "error")}
    if record["type"] == "function_call":
        return {k: record.get(k) for k in
                ("type", "id", "turn_id", "call_id", "name", "arguments", "status")}
    if record["type"] == "function_call_output":
        return {k: record.get(k) for k in
                ("type", "id", "turn_id", "call_id", "status", "output", "error")}
    raise ValueError(f"no rendering for item type {record['type']!r}")


#: `item` by another name, for code where a local variable of that name hides it.
_as_item = item


def openai_list(data: list[dict], page: dict) -> dict:
    return {"object": "list", "data": data, "first_id": page["first_id"],
            "last_id": page["last_id"], "has_more": page["has_more"]}


def anthropic_list(data: list[dict], page: dict) -> dict:
    return {"data": data, "next_page": page["next_page"],
            "prev_page": page["prev_page"]}


# ── vaults ─────────────────────────────────────────────────────────────────
#
# The same resource in both dialects, spelled differently: `display_name` and ISO
# times with `type` in one, `name` and Unix seconds with `object` in the other.
# A secret is never in either: records reaching here have had it removed.

def vault(record: dict, dialect: str) -> dict:
    if dialect == ANTHROPIC:
        return {"id": record["vault_id"], "type": "vault",
                "display_name": record.get("name") or "",
                "metadata": record.get("metadata") or {},
                "created_at": iso(record.get("created_at")),
                "updated_at": iso(record.get("updated_at")),
                "archived_at": iso(record.get("archived_at"))}
    return {"id": record["vault_id"], "object": "vault", "name": record.get("name"),
            "metadata": record.get("metadata") or {},
            "created_at": seconds(record.get("created_at"))}


def _credential_auth(auth: dict, dialect: str) -> dict:
    out = {"type": auth["type"], "mcp_server_url": auth["mcp_server_url"]}
    if auth["type"] == "mcp_oauth":
        out["expires_at"] = auth.get("expires_at")
        out["refresh"] = auth.get("refresh")
        if dialect == ANTHROPIC and out["refresh"] is None:
            out.pop("refresh")
    return out


def credential(record: dict, dialect: str) -> dict:
    if dialect == ANTHROPIC:
        return {"id": record["credential_id"], "type": "vault_credential",
                "vault_id": record["vault_id"],
                "display_name": record.get("name"),
                "auth": _credential_auth(record["auth"], dialect),
                "metadata": record.get("metadata") or {},
                "created_at": iso(record.get("created_at")),
                "updated_at": iso(record.get("updated_at")),
                "archived_at": iso(record.get("archived_at"))}
    return {"id": record["credential_id"], "object": "vault.credential",
            "vault_id": record["vault_id"],
            "name": record.get("name") or record["auth"]["mcp_server_url"],
            "auth": _credential_auth(record["auth"], dialect),
            "metadata": record.get("metadata") or {},
            "created_at": seconds(record.get("created_at")),
            "updated_at": seconds(record.get("updated_at"))}


def resource_list(items: list[dict], dialect: str) -> dict:
    """A page of already-rendered resources; everything fits one page for now."""
    if dialect == ANTHROPIC:
        return {"data": items, "next_page": None}
    return {"object": "list", "data": items, "has_more": False,
            "first_id": items[0]["id"] if items else None,
            "last_id": items[-1]["id"] if items else None}
