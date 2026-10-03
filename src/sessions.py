"""
Sessions: created, listed, fed events, archived. No model call yet.

Everything a façade accepts is translated into internal events here, once, so the
two dialects cannot disagree about what a message *does* — only about how it is
spelled on the way in and rendered on the way out.

**A message is accepted before anything can answer it.** Slice three brings the
worker that takes a step; until then a message is committed with
`processed_at: null` and opens a `queued` turn. Both dialects have a native way to
say exactly that, so this is not a pretence — it is the state a real deployment is
in between a message arriving and a worker picking it up.

**What is not supported fails by name.** A field this service ignores is a
configuration nobody reviewed, so vault_ids, budgets, resources, overrides and the
like are refused with a message saying so, rather than dropped.
"""

from __future__ import annotations

from typing import Any

import agentstore
import events as E
import project
import store as S


class Invalid(ValueError):
    """The request is malformed, or asks for something not supported."""


class NotFound(Exception):
    pass


class Conflict(Exception):
    """The session is in a state that forbids this."""


ANTHROPIC, OPENAI = "anthropic", "openai"

#: Fields each dialect may send when creating a session. Known-but-unsupported ones
#: get their own message, so the answer says "not yet" rather than "no such thing".
_CREATE_FIELDS = {
    ANTHROPIC: {"agent", "environment_id", "title", "metadata", "initial_events",
                "resources", "vault_ids", "budget"},
    OPENAI: {"agent", "agent_id", "environment", "input", "metadata", "vault_ids",
             "stream"},
}
_NOT_YET = {
    "resources": "files, repositories and memory stores are not attached yet",
    "stream": "a streamed create needs the streaming function, which API Gateway "
              "cannot carry; create, then read events",
}


#: What an update may change. The dialects disagree about nearly all of it: in one
#: the agent's tools, in the other its model; metadata patched per key in one and
#: replaced whole in the other.
_UPDATE_FIELDS = {
    ANTHROPIC: {"agent", "budget", "metadata", "title", "vault_ids"},
    OPENAI: {"agent", "metadata"},
}
_AGENT_UPDATE = {
    ANTHROPIC: {"tools", "mcp_servers"},
    OPENAI: {"model", "reasoning", "service_tier"},
}
MAX_METADATA = 16


def _budget(value: Any) -> dict | None:
    """
    `{type: limit, max_list_cost: {amount, currency}}`, or null for none.

    The amount is minor units as an integer string — "2500" is $25.00 — so no
    float ever rounds it. USD only, as in the dialect.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "limit":
        raise Invalid("budget must be {type: limit, max_list_cost: {amount, currency}}")
    cost = value.get("max_list_cost")
    if not isinstance(cost, dict) or set(cost) != {"amount", "currency"}:
        raise Invalid("budget.max_list_cost must be {amount, currency}")
    if cost["currency"] != "USD":
        raise Invalid("budget.max_list_cost.currency must be USD")
    amount = cost["amount"]
    if not isinstance(amount, str) or not amount.isdigit() \
            or (len(amount) > 1 and amount[0] == "0"):
        raise Invalid("budget.max_list_cost.amount is minor units as an integer "
                      "string with no leading zeros: \"2500\" is $25.00")
    return {"type": "limit", "max_list_cost": {"amount": amount, "currency": "USD"}}


def _theirs(committed: list[dict]) -> list[dict]:
    """The caller's own events among those committed: what a send is answered with."""
    return [e for e in committed if e.get("actor") == "user"]


def _refuse_unknown(body: dict, dialect: str) -> None:
    unknown = sorted(set(body) - _CREATE_FIELDS[dialect])
    if unknown:
        raise Invalid(f"unknown field(s): {', '.join(unknown)}")
    for field, why in _NOT_YET.items():
        if body.get(field) not in (None, [], {}, False):
            raise Invalid(f"{field} is not supported by this deployment: {why}")


# ── turning request bodies into text ───────────────────────────────────────

def _anthropic_text(content: Any) -> str:
    """`[{"type": "text", "text": ...}, ...]` to one string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        raise Invalid("content must be a non-empty list of blocks")
    texts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise Invalid(f"only text blocks are supported yet, not "
                          f"{(block or {}).get('type')!r}")
        texts.append(str(block.get("text", "")))
    return "\n".join(texts)


def _openai_text(value: Any) -> str:
    """A string, or `[{"role": "user", "content": [{"type": "input_text", ...}]}]`."""
    if isinstance(value, str):
        if not value.strip():
            raise Invalid("input is empty")
        return value
    if not isinstance(value, list) or not value:
        raise Invalid("input must be a string or a non-empty list of messages")
    texts = []
    for message in value:
        if not isinstance(message, dict) or message.get("role", "user") != "user":
            raise Invalid("only user input messages are accepted")
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content or []:
            if part.get("type") != "input_text":
                raise Invalid(f"only input_text is supported yet, not "
                              f"{part.get('type')!r}")
            texts.append(str(part.get("text", "")))
    return "\n".join(texts)


class Sessions:
    def __init__(self, store: S.Store, agents: agentstore.AgentStore,
                 wake=None, vaults=None) -> None:
        self.store = store
        self.agents = agents
        #: Checks a session's vault_ids: each an existing vault of the tenant.
        self.vaults = vaults
        #: Called once a message is committed. In the cloud it invokes the worker
        #: asynchronously; locally it runs the step inline. None means nothing
        #: answers — which is what the tests of this module want.
        self.wake = wake

    def _wake(self, tenant: str, session_id: str) -> None:
        """
        Start the worker, and never let a failure to start it fail the request.

        The message is already committed. If the wake-up is lost, the message waits
        with processed_at null until the next one arrives — degraded and visible,
        not lost. Failing the request instead would tell the client its message was
        refused when it was not, and a retry would send it twice.
        """
        if self.wake is None:
            return
        try:
            self.wake(tenant, session_id)
        except Exception as exc:  # noqa: BLE001
            print(f'{{"at": "wake", "session": "{session_id}", '
                  f'"error": "{type(exc).__name__}"}}')

    # ── reading ────────────────────────────────────────────────────────────

    def get(self, tenant: str, session_id: str) -> dict:
        record = self.store.get_session(tenant, session_id)
        # A deleted session is gone from the API at once, in both dialects, while
        # its data is still being purged behind it.
        if record is None or record.get("status") == E.DELETED:
            raise NotFound(session_id)
        return record

    def list(self, tenant: str) -> list[dict]:
        return [r for r in self.store.list_sessions(tenant)
                if r.get("status") != E.DELETED]

    def events(self, tenant: str, session_id: str) -> list[dict]:
        self.get(tenant, session_id)
        return self.store.events(tenant, session_id)

    def agent_of(self, tenant: str, record: dict) -> dict | None:
        try:
            return self.agents.get(tenant, record["agent_id"],
                                   record.get("agent_version"))
        except agentstore.NotFound:
            return None

    # ── creating ───────────────────────────────────────────────────────────

    def create(self, tenant: str, dialect: str, body: dict) -> tuple[dict, list[dict]]:
        """A session, and the events its creation committed."""
        _refuse_unknown(body, dialect)

        if dialect == ANTHROPIC:
            agent_id, version = self._anthropic_agent(tenant, body.get("agent"))
            environment = body.get("environment_id")
            if not environment:
                raise Invalid("environment_id is required")
            # Recorded, not resolved: there is no environments resource yet. It is
            # required because the dialect requires it; it describes nothing,
            # because this deployment has no sandbox.
            fields = {"environment": {"id": environment, "type": "none"}}
            if body.get("budget") is not None:
                fields["budget"] = _budget(body["budget"])
            first_input = [
                _anthropic_text(ev.get("content"))
                for ev in body.get("initial_events") or []
                if self._initial_is_message(ev)
            ]
        else:
            environment = body.get("environment") or {}
            if environment.get("type") != "none":
                raise Invalid(
                    "environment.type must be 'none': this deployment has no sandbox, "
                    "so 'openai_hosted' and 'self_hosted' cannot be honoured")
            agent_id, version = self._openai_agent(tenant, body)
            fields = {"environment": {"type": "none"}}
            # The dialect makes input required when there is no environment — a
            # session without a sandbox and without work has nothing to be.
            if body.get("input") in (None, "", []):
                raise Invalid("input is required when environment.type is 'none'")
            first_input = [_openai_text(body["input"])]

        if body.get("vault_ids"):
            if self.vaults is None:
                raise Invalid("vault_ids: this deployment keeps no vaults")
            try:
                fields["vault_ids"] = self.vaults.check_ids(tenant, body["vault_ids"])
            except ValueError as exc:
                raise Invalid(f"vault_ids: {exc}") from exc

        session_id = E.new_id("sess")
        now = E.now_ms()
        record = self.store.create_session(
            tenant, session_id,
            agent_id=agent_id, agent_version=version, dialect=dialect,
            title=body.get("title"), metadata=body.get("metadata") or {},
            updated_at=now, **fields)
        committed: list[dict] = []
        for text in first_input:
            committed += self._accept_message(tenant, session_id, text)
        # The session as this request left it — read before the worker is woken,
        # so the answer is the same whether the worker runs elsewhere (the cloud)
        # or inline before this returns (a local run).
        made = self.get(tenant, session_id)
        if committed:
            self._wake(tenant, session_id)
        return made, _theirs(committed)

    @staticmethod
    def _initial_is_message(ev: dict) -> bool:
        kind = (ev or {}).get("type")
        if kind == "user.message":
            return True
        if kind == "user.define_outcome":
            raise Invalid("user.define_outcome is not supported yet")
        raise Invalid(f"initial_events accepts user.message only, not {kind!r}")

    def _anthropic_agent(self, tenant: str, ref: Any) -> tuple[str, int]:
        """A string (latest), or `{type: agent, id, version}` (pinned)."""
        if isinstance(ref, str):
            agent = self._agent(tenant, ref)
            return ref, agent["version"]
        if isinstance(ref, dict) and ref.get("type") == "agent":
            agent = self._agent(tenant, ref.get("id"), ref.get("version"))
            return agent["agent_id"], agent["version"]
        if isinstance(ref, dict) and ref.get("type") == "agent_with_overrides":
            raise Invalid("agent_with_overrides is not supported yet")
        raise Invalid("agent is required: an id, or {type: agent, id, version}")

    def _openai_agent(self, tenant: str, body: dict) -> tuple[str, int]:
        """A saved agent by `agent_id`, or an inline one saved on the way in."""
        if body.get("agent_id") and body.get("agent"):
            raise Invalid("overriding a saved agent per session is not supported yet")
        if body.get("agent_id"):
            agent = self._agent(tenant, body["agent_id"])
            return agent["agent_id"], agent["version"]
        inline = body.get("agent")
        if not isinstance(inline, dict) or not inline.get("model"):
            raise Invalid("agent_id, or an inline agent with a model, is required")
        # The dialect allows an agent that exists only for this session. We store it
        # anyway — the internal shape is the richer one, and a session always pins a
        # stored version, whichever door it came in by.
        fields = {k: inline[k] for k in agentstore.FIELDS if k in inline}
        try:
            made = self.agents.create(tenant, **fields)
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        return made["agent_id"], made["version"]

    def _agent(self, tenant: str, agent_id: Any, version: Any = None) -> dict:
        if not isinstance(agent_id, str) or not agent_id:
            raise Invalid("agent id is missing")
        try:
            agent = self.agents.get(tenant, agent_id, version)
        except agentstore.NotFound as exc:
            raise NotFound(f"agent {agent_id}") from exc
        if agent.get("archived"):
            raise Conflict(f"agent {agent_id} is archived; new sessions cannot use it")
        return agent

    # ── sending ────────────────────────────────────────────────────────────

    def send(self, tenant: str, session_id: str, dialect: str,
             incoming: list[dict]) -> list[dict]:
        """Translate each incoming event, commit, and return what the caller sent —
        not the status events committed with it, which are the session's own."""
        record = self.get(tenant, session_id)
        if record.get("status") == E.ARCHIVED:
            raise Conflict(f"session {session_id} is archived and accepts no events")
        if not isinstance(incoming, list) or not incoming:
            raise Invalid("events must be a non-empty list")

        committed: list[dict] = []
        for ev in incoming:
            kind = (ev or {}).get("type")
            if dialect == ANTHROPIC:
                if kind == "user.message":
                    committed += self._accept_message(
                        tenant, session_id, _anthropic_text(ev.get("content")))
                elif kind == "user.interrupt":
                    committed += self._interrupt(tenant, session_id)
                elif kind in ("user.tool_confirmation", "user.custom_tool_result"):
                    # True, not a placeholder: nothing has asked for either, because
                    # no step has run.
                    raise Invalid(f"{kind} answers a pending tool call, and this "
                                  "session has none")
                elif kind in ("system.message", "user.define_outcome"):
                    raise Invalid(f"{kind} is not supported yet")
                else:
                    raise Invalid(f"unknown event type {kind!r}")
            else:
                if kind == "agent.session.input.message":
                    committed += self._accept_message(
                        tenant, session_id, _openai_text(ev.get("input")))
                elif kind == "agent.session.input.cancel":
                    committed += self._interrupt(tenant, session_id)
                elif kind == "agent.session.input.tool_result":
                    raise Invalid("tool_result answers a pending function call, and "
                                  "this session has none")
                else:
                    raise Invalid(f"unknown event type {kind!r}")
        if any(e["type"] == E.USER_MESSAGE for e in committed):
            self._wake(tenant, session_id)
        return _theirs(committed)

    def _commit(self, tenant: str, session_id: str, build, **fields) -> list[dict]:
        """
        Build events from the current log, commit them, retry if someone else won.

        `build` sees the log as it is *now*, so a retry recomputes rather than
        replaying a decision made against a log that has since moved — which matters
        for joining a turn, the one decision here that depends on what came before.
        """
        for _ in range(10):
            record = self.get(tenant, session_id)
            log = self.store.events(tenant, session_id)
            # `build` returns the events, or the events and session fields that
            # depend on what it read.
            built = build(log)
            new, derived = built if isinstance(built, tuple) else (built, {})
            cursor = max((e["seq"] for e in log), default=record.get("cursor", 0))
            try:
                last = self.store.commit(tenant, session_id, new, cursor=cursor,
                                         updated_at=E.now_ms(),
                                         **{**fields, **derived})
            except S.Conflict:
                continue
            first = last - len(new) + 1
            return [dict(ev, seq=first + n) for n, ev in enumerate(new)]
        raise Conflict(f"session {session_id} is being written too fast to append to")

    def _accept_message(self, tenant: str, session_id: str, text: str) -> list[dict]:
        def build(log):
            # One message, one turn, one answer — as the original does, whether the
            # messages come in one request or one after another. They queue, and
            # are answered in order within one stretch of `running`.
            turn_id = E.new_id("turn")
            message = E.event(E.USER_MESSAGE, turn_id=turn_id, actor="user",
                              processed=False, text=text)
            # The original says the session is running before it echoes the
            # message that set it going, so a client sees the status change first.
            if project.is_running(log):
                return [message]
            return [E.event(E.STATUS_RUNNING, turn_id=turn_id), message]
        return self._commit(tenant, session_id, build, status=E.QUEUED)

    def _interrupt(self, tenant: str, session_id: str) -> list[dict]:
        """
        Stop, and hand the floor back.

        With nothing in flight it is still recorded, as the original records it: it
        is something the user did. It belongs to no turn, ends none, and the fold
        does not show it to the model.
        """
        def build(log):
            stopped = project.unfinished_turns(log)
            turn_id = stopped[-1] if stopped else None
            # One interrupt the client sees; every other waiting turn ends with it,
            # or the worker would go on to answer them.
            out = [E.event(E.TURN_CANCELLED, turn_id=t) for t in stopped[:-1]]
            out.append(E.event(E.USER_INTERRUPT, turn_id=turn_id, actor="user"))
            if project.is_running(log):
                out.append(E.event(E.STATUS_IDLE, turn_id=turn_id, stop_reason="end_turn"))
            return out
        return self._commit(tenant, session_id, build, status=E.TURN_OVER)

    # ── ending ─────────────────────────────────────────────────────────────

    # ── updating ───────────────────────────────────────────────────────────

    def update(self, tenant: str, session_id: str, dialect: str, body: dict) -> dict:
        """
        Title, metadata and the agent's settings for the turns that follow.

        Agent settings are kept on the session as overrides of the pinned agent
        version — the agent itself is not changed, other sessions do not see them —
        and the worker applies them from the next step. The change is an event, so
        two updates at once are ordered by the log and neither is lost: a metadata
        patch reads the metadata the log says is current, not a copy read earlier.
        """
        if not isinstance(body, dict):
            raise Invalid("the body must be a JSON object")
        unknown = sorted(set(body) - _UPDATE_FIELDS[dialect])
        if unknown:
            raise Invalid(f"unknown field(s): {', '.join(unknown)}")
        if body.get("vault_ids") not in (None, []):
            raise Invalid("vault_ids is fixed when the session is created")
        if "budget" in body:
            budget = _budget(body["budget"])
        agent = body.get("agent") or {}
        if not isinstance(agent, dict):
            raise Invalid("agent must be an object")
        unknown = sorted(set(agent) - _AGENT_UPDATE[dialect])
        if unknown:
            raise Invalid(f"agent: unknown or not updatable field(s): "
                          f"{', '.join(unknown)}")
        # Shown as the dialect's defaults because they are what the model runs
        # with; accepting another value and running the default would be a lie.
        if (agent.get("reasoning") or {}).get("effort") is not None:
            raise Invalid("agent.reasoning.effort is not configurable yet")
        if agent.get("service_tier") not in (None, "auto"):
            raise Invalid("agent.service_tier is not configurable yet; it is auto")
        if "model" in agent and not (isinstance(agent["model"], str) and agent["model"]):
            raise Invalid("agent.model must be a model name")
        for field in ("tools", "mcp_servers"):
            if field in agent and not isinstance(agent[field], list):
                raise Invalid(f"agent.{field} must be an array; it replaces the list")
        if "title" in body and not isinstance(body["title"], (str, type(None))):
            raise Invalid("title must be a string or null")
        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise Invalid("metadata must be an object or null")
        for k, v in (metadata or {}).items():
            allowed = (str, type(None)) if dialect == ANTHROPIC else (str,)
            if not isinstance(v, allowed):
                raise Invalid(f"metadata.{k} must be a string"
                              + (" or null" if dialect == ANTHROPIC else ""))

        def build(log):
            # Read after the log, so it is at least as new as the log; if it is
            # newer, the commit below conflicts on the cursor and this runs again.
            record = self.get(tenant, session_id)
            if record.get("status") == E.ARCHIVED:
                raise Conflict("an archived session cannot be updated")
            changes: dict = {}
            if "title" in body:
                changes["title"] = body["title"]
            if "budget" in body:
                # Raising it lets the next turn run; null removes it.
                changes["budget"] = budget
            if "metadata" in body:
                if dialect == ANTHROPIC:        # a patch: null deletes a key
                    merged = dict(record.get("metadata") or {})
                    for k, v in (metadata or {}).items():
                        if v is None:
                            merged.pop(k, None)
                        else:
                            merged[k] = v
                else:                           # a replacement: null or {} clears
                    merged = dict(metadata or {})
                if len(merged) > MAX_METADATA:
                    raise Invalid(f"metadata holds at most {MAX_METADATA} keys")
                changes["metadata"] = merged
            overrides = dict(record.get("agent_overrides") or {})
            for field in ("model", "tools", "mcp_servers"):
                if field in agent:
                    overrides[field] = agent[field]
            if overrides != (record.get("agent_overrides") or {}):
                changes["agent_overrides"] = overrides
            return [E.event(E.SESSION_UPDATED, actor="user",
                            changed=sorted(changes))], changes

        self._commit(tenant, session_id, build)
        return self.get(tenant, session_id)

    def delete(self, tenant: str, session_id: str) -> None:
        """
        Gone from the API now; the data is purged by the worker after.

        The deletion is an event, so it takes its place in the log by the same
        conditional append as everything else. That is what settles a race with a
        message sent at the same moment: if the message took the next sequence first,
        the deletion re-reads the log, finds a turn under way and refuses; if the
        deletion took it, the message re-reads, finds the session gone and gets a
        404. Neither can slip in behind the other.
        """
        def build(log):
            # Running execution must be cancelled first — the dialect's rule, and
            # the only safe one: a worker mid-step would write into a purged log.
            if project.current_turn(log) is not None:
                raise Conflict("a session with a turn under way cannot be deleted; "
                               "cancel the turn first and wait for it to end")
            return [E.event(E.SESSION_DELETED, actor="user")]
        self._commit(tenant, session_id, build, status=E.DELETED,
                     deleted_at=E.now_ms())
        self._wake(tenant, session_id)

    def was_deleted(self, tenant: str, session_id: str) -> bool:
        """Deleted, and its tombstone still there — the purge has not finished."""
        record = self.store.get_session(tenant, session_id)
        return bool(record) and record.get("status") == E.DELETED

    def archive(self, tenant: str, session_id: str) -> dict:
        record = self.get(tenant, session_id)
        if record.get("status") == E.ARCHIVED:
            return record
        # Both dialects refuse to archive a session that is working: interrupt it
        # first, wait for it to go idle, then archive.
        if record.get("status") in (E.QUEUED, E.WORKING, E.BETWEEN_STEPS):
            raise Conflict("a running session cannot be archived; send an interrupt "
                           "first and wait for it to go idle")
        now = E.now_ms()
        self._commit(tenant, session_id, lambda log: [],
                     status=E.ARCHIVED, archived_at=now)
        return self.get(tenant, session_id)
