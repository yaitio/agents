"""
The step worker: take the lease, fold the log, take steps, commit, let go.

Nothing lives in memory between steps. Everything this function knows it reads from
the log at the start of a step, and everything it learns it writes back before the
next one — so any invocation can take the next step, and an invocation that dies
loses at most the step it was in.

Three things here are easy to get wrong, and each has a comment where it is handled:

* **the lost wakeup** — a message arrives while this worker holds the lease. The
  invocation the API starts for it cannot take the lease and exits; this worker has
  already decided there is nothing left. So after letting go, it looks once more.
* **a commit that loses a race** — the API appended a message while the model was
  thinking. The commit is retried at the new cursor; the model is not asked again.
* **an orphaned turn** — a worker died between starting a turn and answering it. The
  next one takes the *earliest* unfinished turn, not the latest.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Callable

import agentstore
import events as E
import fold as F
import model as M
import project
import store as S
import tools as T
import vault as V

#: How long a claim on a session lasts. Longer than the function's timeout, so a
#: worker that is still alive never loses the lease; short enough that one killed
#: mid-step frees the session within a few minutes.
LEASE_SECONDS = int(os.environ.get("YAIT_LEASE_SECONDS", "150"))

#: Stop starting steps with less than this left of the invocation. A step that
#: cannot finish is worse than one that never starts: the handoff re-reads the log
#: and continues, while a step killed by the timeout leaves a started turn behind.
RESERVE_MS = int(os.environ.get("YAIT_RESERVE_MS", "20000"))

#: How many times the lost-wakeup loop may go round before giving up. It only goes
#: round when work arrived during the previous pass, so this bounds a flood, not a
#: normal session.
MAX_PASSES = 20

#: Tool rounds one turn may take before it is stopped: a model that keeps calling
#: tools without answering is looping, and every round is paid for.
MAX_TOOL_ROUNDS = int(os.environ.get("YAIT_MAX_TOOL_ROUNDS", "20"))


def commit(store: S.Store, tenant: str, session: str, new: list[dict],
           **fields) -> int:
    """
    Append `new` at whatever the cursor is *now*, retrying if another writer won.

    The events do not depend on their sequence numbers, so a lost race costs a
    re-read and nothing else — in particular, not a second model call.
    """
    for _ in range(20):
        log = store.events(tenant, session)
        cursor = max((e["seq"] for e in log), default=0)
        try:
            return store.commit(tenant, session, new, cursor=cursor,
                                updated_at=E.now_ms(), **fields)
        except S.Conflict:
            continue
    raise S.Conflict(f"could not commit to {session}: the log kept moving")


def persistable(state: dict) -> dict:
    """
    The library's run state, reduced to what a store can hold.

    It is small and mostly numbers, but not only: `adaptations` may hold objects, and
    a value the store cannot serialise fails the commit that was meant to record the
    answer. Anything JSON cannot take is kept as its text — lossy, but visible, and
    never the reason a turn goes unrecorded.
    """
    import dataclasses
    out: dict = {}
    for k, v in state.items():
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            v = dataclasses.asdict(v)
        elif isinstance(v, list):
            v = [dataclasses.asdict(x) if dataclasses.is_dataclass(x)
                 and not isinstance(x, type) else x for x in v]
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


class Worker:
    def __init__(self, store: S.Store, agents: agentstore.AgentStore, model,
                 wake_self: Callable[[str, str], None] | None = None,
                 vaults=None) -> None:
        self.store = store
        self.agents = agents
        self.model = model
        self.wake_self = wake_self
        #: The session's credentials for MCP servers (vault.py).
        self.vaults = vaults

    def _credential(self, tenant: str, vault_ids: list[str], url: str) -> dict | None:
        if self.vaults is None or not vault_ids:
            return None
        return self.vaults.secret_for(tenant, vault_ids, url)

    def _cancelled(self, tenant: str, session: str, turn_id: str) -> bool:
        return any(t["turn_id"] == turn_id and t["status"] == "cancelled"
                   for t in project.turns(self.store.events(tenant, session),
                                          session_id="", agent_id=None))

    def pending(self, log: list[dict]) -> str | None:
        """
        The earliest turn that has not ended.

        Not the latest: if a worker died answering turn A and turn B has since been
        queued behind it, A is answered first. Answering B would leave A open for
        ever, and the conversation the model sees would have a hole in it.
        """
        for turn in project.turns(log, session_id="", agent_id=None):
            if turn["status"] in ("queued", "in_progress"):
                return turn["turn_id"]
        return None

    def run(self, tenant: str, session: str, *, holder: str,
            remaining_ms: Callable[[], int] = lambda: 10 ** 9,
            provider_key: str | None = None) -> dict:
        # The caller's key, for every model call this run makes; never stored.
        # Nothing of the caller outlives the run: a warm environment serves the
        # next invocation, another tenant's, from this same process.
        try:
            with M.using_key(provider_key):
                return self._run(tenant, session, holder=holder,
                                 remaining_ms=remaining_ms)
        finally:
            T.forget()

    def _run(self, tenant: str, session: str, *, holder: str,
             remaining_ms: Callable[[], int]) -> dict:
        record = self.store.get_session(tenant, session)
        if record is None:
            return {"result": "gone", "steps": 0}
        if record.get("status") == E.DELETED:
            if record.get("purged"):
                return {"result": "gone", "steps": 0}
            return self.purge(tenant, session, remaining_ms)
        steps = 0
        for _ in range(MAX_PASSES):
            if not self.store.take_lease(tenant, session, holder, LEASE_SECONDS):
                # Someone else is working. Their lost-wakeup check will see anything
                # that arrived, so exiting here loses nothing.
                return {"result": "leased", "steps": steps}

            handed_off = False
            try:
                while True:
                    log = self.store.events(tenant, session)
                    turn_id = self.pending(log)
                    if turn_id is None:
                        break
                    if remaining_ms() < RESERVE_MS:
                        handed_off = True
                        break
                    self.step(tenant, session, log, turn_id)
                    steps += 1
            finally:
                self.store.release_lease(tenant, session, holder)

            if handed_off:
                if self.wake_self is None:
                    return {"result": "out_of_time", "steps": steps}
                self.wake_self(tenant, session)
                return {"result": "handed_off", "steps": steps}

            # The lost wakeup. Work that arrived after the last read above found the
            # lease taken and its own invocation exited; look once more now that the
            # lease is free, and go round again if there is something.
            if self.pending(self.store.events(tenant, session)) is None:
                return {"result": "idle", "steps": steps}
        return {"result": "flooded", "steps": steps}

    def purge(self, tenant: str, session: str,
              remaining_ms: Callable[[], int]) -> dict:
        """
        The physical cleanup a deletion promised. No lease: purging is idempotent,
        and a deleted session has no turn for anyone else to be working on — the
        deletion refused while one was under way.
        """
        done = self.store.purge(tenant, session,
                                keep_going=lambda: remaining_ms() >= RESERVE_MS)
        if done:
            return {"result": "purged", "steps": 0}
        if self.wake_self is None:
            return {"result": "out_of_time", "steps": 0}
        self.wake_self(tenant, session)
        return {"result": "handed_off", "steps": 0}

    def step(self, tenant: str, session: str, log: list[dict], turn_id: str) -> None:
        """One model call, committed before and after."""
        record = self.store.get_session(tenant, session) or {}

        def finish(events: list[dict], stop_reason: str, *, this: dict | None = None,
                   cost: float | None = None, status: str = E.TURN_OVER,
                   **fields) -> None:
            """
            End this turn — and the stretch of work, unless another message waits.

            One message, one turn, one answer; but one `running` … `idle` around
            however many are queued, as the original does: it answers each message
            in order and says idle once, when there is nothing left. So the usage
            snapshot and the idle event close the stretch, not every turn.
            """
            now = project.turns(self.store.events(tenant, session),
                                session_id="", agent_id=None)
            if any(t["turn_id"] == turn_id and t["status"] == "cancelled" for t in now):
                # Interrupted while the model was answering: the interrupt already
                # ended the turn and said idle. The request is recorded as ended;
                # its answer is not published.
                ends = [e for e in events if e["type"] == E.MODEL_REQUEST_END]
                commit(self.store, tenant, session, ends, **fields)
                return
            waiting = any(t["turn_id"] != turn_id and t["status"] == "queued" for t in now)
            if waiting:
                commit(self.store, tenant, session, events, status=E.WORKING, **fields)
                return
            if cost is None:
                cost = (record.get("agent_state") or {}).get("cost")
            closing = [E.event(E.SESSION_USAGE, turn_id=turn_id,
                               **usage_so_far(log, this, cost)),
                       E.event(E.STATUS_IDLE, turn_id=turn_id, stop_reason=stop_reason)]
            commit(self.store, tenant, session, [*events, *closing], status=status,
                   **fields)
        try:
            agent = self.agents.get(tenant, record["agent_id"],
                                    record.get("agent_version"))
        except (agentstore.NotFound, KeyError):
            agent = None
        if agent is not None:
            # What the session changed since, applied from this step on.
            agent = {**agent, **(record.get("agent_overrides") or {})}
        else:
            finish([E.event(E.STEP_FAILED, turn_id=turn_id,
                            reason="the session's agent no longer exists")],
                   "retries_exhausted")
            return

        # The budget is checked before the call, never after: the dialect's rule is
        # that the session stops *issuing* requests once the cost reaches it, since
        # what a reply costs is not known until it is paid for.
        over = over_budget(record.get("budget"),
                           (record.get("agent_state") or {}).get("cost"),
                           agent.get("model", ""),
                           getattr(self.model, "priced", lambda _name: True))
        if over:
            finish([E.event(E.STEP_FAILED, turn_id=turn_id, reason=over,
                            error_type="budget")],
                   "budget_reached", status=E.AT_CEILING)
            return

        # The agent's tools, from its MCP servers, with the credentials the session's
        # vaults hold for them. Asked each step: a server's tools can change, and a
        # step is the unit a worker survives. A server that cannot be reached, or
        # refuses, fails the turn by name rather than leaving the model to guess.
        offered: dict = {}
        rounds = sum(1 for e in log if e.get("type") == E.AGENT_TOOL_CALL
                     and e.get("turn_id") == turn_id)
        if rounds >= MAX_TOOL_ROUNDS:
            finish([E.event(E.STEP_FAILED, turn_id=turn_id,
                            reason=f"the turn called tools {rounds} times without "
                                   f"answering; it is stopped at {MAX_TOOL_ROUNDS}",
                            error_type="invalid_request")], "retries_exhausted")
            return
        if agent.get("tools"):
            try:
                offered = T.discover(agent, lambda url: self._credential(
                    tenant, record.get("vault_ids") or [], url))
            except T.Unsupported as exc:
                finish([E.event(E.STEP_FAILED, turn_id=turn_id, reason=str(exc),
                                error_type="invalid_request")], "retries_exhausted")
                return
            except T.ServerFailed as exc:
                finish([E.event(E.STEP_FAILED, turn_id=turn_id, reason=str(exc),
                                error_type=exc.kind, mcp_server=exc.server)],
                       "retries_exhausted")
                return

        # Committed before the call, so the turn reads in_progress and the messages
        # in it read processed while the model thinks — and so a worker that dies
        # during the call leaves a turn the next one knows to resume.
        start = E.event(E.MODEL_REQUEST_START, turn_id=turn_id,
                        backend=getattr(self.model, "name", "unknown"))
        # "Running" is said once per stretch of work: the API says it when a message
        # sets an idle session going; the worker says it only when picking up a turn
        # after the session had gone idle — a turn queued while another ran.
        opening = [] if project.is_running(log) else [E.event(E.STATUS_RUNNING,
                                                              turn_id=turn_id)]
        commit(self.store, tenant, session, [*opening, start], status=E.WORKING)
        start_id = start["event_id"]

        # A failed step ends with `retries_exhausted`: the dialect's only way of
        # saying "gave up", and the honest one — the library retries transient
        # failures before a failure reaches here.

        # Fold the log as it is *after* that commit, not the one read before it. A
        # message can join a turn only while the turn is queued; between the read
        # above and the commit, one may have — and folding the older log would answer
        # the turn without it while its processed_at claimed otherwise. Once the start
        # is committed the turn is in progress and nothing more can join, and the
        # conditional append settles which of the two writes came first.
        log = self.store.events(tenant, session)
        state = dict(record.get("agent_state") or {})
        messages = F.fold(log, instructions=agent.get("instructions"),
                          until_turn=turn_id)
        try:
            reply = self.model.step(messages, state,
                                    model_name=agent.get("model", ""),
                                    **({"tools": [t for _, t in offered.values()]}
                                       if offered else {}))
        except Exception as exc:  # noqa: BLE001 — any failure ends the turn, named
            finish([E.event(E.STEP_FAILED, turn_id=turn_id,
                            reason=f"{type(exc).__name__}: {exc}",
                            error_type=M.error_category(exc)),
                    E.event(E.MODEL_REQUEST_END, turn_id=turn_id, error=True,
                            model_request_start_id=start_id)],
                   "retries_exhausted", cost=state.get("cost"),
                   agent_state=persistable(state))
            return

        thought = [E.event(E.AGENT_THINKING, turn_id=turn_id)] if reply.thought else []
        if reply.calls:
            # The call is committed before it runs, so a worker that dies during it
            # leaves a call the fold answers with "outcome unknown" rather than one
            # that silently runs twice. Then each runs, and its result is committed;
            # the turn stays open and the next step shows the model what came back.
            calls = [{**c, "server": offered[c["name"]][0].name}
                     if c["name"] in offered else c for c in reply.calls]
            commit(self.store, tenant, session,
                   [*thought,
                    E.event(E.AGENT_TOOL_CALL, turn_id=turn_id, actor="agent",
                            calls=calls, text=reply.text or None),
                    E.event(E.MODEL_REQUEST_END, turn_id=turn_id,
                            usage=reply.usage, backend=reply.backend,
                            model_request_start_id=start_id)],
                   status=E.WORKING, agent_state=persistable(state))
            if self._cancelled(tenant, session, turn_id):
                return            # interrupted while the model chose: nothing runs
            results = []
            for c in calls:
                text, failed = T.call(offered, c["name"], c.get("arguments") or {})
                results.append(E.event(E.TOOL_RESULT, turn_id=turn_id,
                                       call_id=c["id"], name=c["name"],
                                       server=c.get("server"), result=text,
                                       is_error=failed))
            if self._cancelled(tenant, session, turn_id):
                # Interrupted while the tools ran: what they did is recorded, since
                # it happened, but a turn that said idle says nothing after it.
                results = [{**r, "late": True} for r in results]
                commit(self.store, tenant, session, results)
                return
            commit(self.store, tenant, session, results, status=E.WORKING)
            return

        finish([*thought,
                E.event(E.AGENT_MESSAGE, turn_id=turn_id, actor="agent",
                        text=reply.text or ""),
                E.event(E.MODEL_REQUEST_END, turn_id=turn_id,
                        usage=reply.usage, backend=reply.backend,
                        model_request_start_id=start_id)],
               "end_turn", this=reply.usage, cost=state.get("cost"),
               agent_state=persistable(state))


# ── the Lambda entry point ─────────────────────────────────────────────────

_worker: Worker | None = None


def _invoke(function_name: str, tenant: str, session: str,
            provider_key: str | None = None) -> None:
    """
    Wake the worker. The caller's provider key travels in the invocation — held by
    Lambda, encrypted, for as long as the event is queued — and nowhere else: not in
    the log, not in the table, not in a log line.
    """
    import boto3
    payload = {"tenant": tenant, "session": session}
    if provider_key:
        payload["provider_key"] = provider_key
    boto3.client("lambda").invoke(
        FunctionName=function_name, InvocationType="Event",
        Payload=json.dumps(payload).encode())


_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")


def usage_so_far(log: list[dict], this: dict | None, cost_usd: float | None) -> dict:
    """Token totals over every model request in the log, plus this one; and the cost."""
    totals = dict.fromkeys(_USAGE_FIELDS, 0)
    for ev in log:
        if ev.get("type") == E.MODEL_REQUEST_END:
            for field in _USAGE_FIELDS:
                totals[field] += int((ev.get("usage") or {}).get(field) or 0)
    for field in _USAGE_FIELDS:
        totals[field] += int((this or {}).get(field) or 0)
    return {**totals, "cost_usd": cost_usd}


def over_budget(budget: dict | None, spent_usd: float | None, model_name: str,
                priced: Callable[[str], bool]) -> str | None:
    """
    Why the next model request must not be issued, or None if it may.

    Two reasons, both the dialect's: the tracked cost has reached the limit, or the
    model has no list price — a budget that cannot measure a model cannot bound it,
    so it does not let it run. Compared in decimal cents; the limit is an integer
    string precisely so that nothing rounds it.
    """
    if not budget:
        return None
    if not priced(model_name):
        return (f"the model {model_name!r} has no list price, so the budget cannot "
                "measure it; remove the budget to use this model")
    limit = Decimal(budget["max_list_cost"]["amount"])
    spent = Decimal(str(spent_usd or 0)) * 100
    if spent >= limit:
        return (f"the session has spent ${spent / 100:.2f} of its "
                f"${limit / 100:.2f} budget; raise the budget to continue")
    return None


def wake(tenant: str, session: str, *, store: S.Store, agents: agentstore.AgentStore,
         provider_key: str | None = None, vaults=None) -> None:
    """
    Start a worker for a session: another function if one is configured, otherwise
    in this process.

    In the cloud the API invokes the worker asynchronously and returns at once. In a
    local run there is no second function, so the step runs on a thread of this
    process — the same code against the same store, and the same timing: the
    request returns first.
    """
    function_name = os.environ.get("YAIT_WORKER_FUNCTION", "").strip()
    if function_name:
        _invoke(function_name, tenant, session, provider_key)
        return
    # Locally, on a thread: the request returns before the answer, as it does in
    # the cloud. Inline, a second message could never arrive while the first was
    # being answered, and a local run would test a timing no deployment has.
    import threading
    threading.Thread(target=lambda: Worker(store, agents, M.from_environment(),
                                           vaults=vaults).run(
        tenant, session, holder=E.new_id("local"), provider_key=provider_key),
        daemon=True).start()


def handler(event, context):
    global _worker
    if _worker is None:
        _worker = Worker(S.from_environment(), agentstore.from_environment(),
                         M.from_environment(), vaults=V.from_environment())
    tenant, session = event["tenant"], event["session"]
    key = event.get("provider_key")
    # A handoff carries the key on, or the next invocation could not call the model.
    _worker.wake_self = lambda t, s: _invoke(context.function_name, t, s, key)
    try:
        result = _worker.run(tenant, session, holder=context.aws_request_id,
                             remaining_ms=context.get_remaining_time_in_millis,
                             provider_key=key)
    finally:
        # The handoff closes over the caller's provider key; it must not sit in
        # the process until the next invocation replaces it.
        _worker.wake_self = None
    print(json.dumps({"at": "worker", "tenant": tenant, "session": session, **result}))
    return result
