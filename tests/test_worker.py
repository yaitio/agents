#!/usr/bin/env python3
"""
The step worker, with the three failure modes it is built around made to happen.

A happy-path test would pass against a worker that has all three bugs, because none
of them show up unless something arrives or dies at the wrong moment. So the models
below are instruments: one appends a message while it is "thinking", one raises, one
counts its calls, and a clock runs out on demand.

    python3 tests/test_worker.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agentstore as A  # noqa: E402
import events as E  # noqa: E402
import fold as F  # noqa: E402
import model as M  # noqa: E402
import project  # noqa: E402
import render  # noqa: E402
import sessions as SS  # noqa: E402
import store as S  # noqa: E402
import worker as W  # noqa: E402
from worker import Worker, commit  # noqa: E402

T = "t_test"


class Results:
    def __init__(self, label: str) -> None:
        self.label, self.passed, self.failures = label, 0, []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        print(f"\n{self.label}: {self.passed}/{self.passed + len(self.failures)}"
              " checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


# ── instruments ────────────────────────────────────────────────────────────

class Counting(M.EchoModel):
    """Echo, and remember how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def step(self, messages, state, **kw):
        self.calls += 1
        return super().step(messages, state, **kw)


class Interjecting(Counting):
    """While thinking about the first turn, the user sends another message."""

    def __init__(self, svc: SS.Sessions, session: str) -> None:
        super().__init__()
        self.svc, self.session, self.done = svc, session, False

    def step(self, messages, state, **kw):
        if not self.done:
            self.done = True
            self.svc.send(T, self.session, SS.ANTHROPIC, [msg("while you think")])
        return super().step(messages, state, **kw)


class Failing(Counting):
    def step(self, messages, state, **kw):
        self.calls += 1
        raise RuntimeError("provider is down")


class InsufficientCreditsError(Exception):
    """Named as the library names it: the category is read from the class name."""


class OutOfCredits(Counting):
    def step(self, messages, state, **kw):
        self.calls += 1
        raise InsufficientCreditsError("top up the account")


class Calling(Counting):
    def step(self, messages, state, **kw):
        self.calls += 1
        return M.Reply(calls=[{"id": "toolu_1", "name": "search", "arguments": {}}],
                       backend="calling")


def msg(text):
    return {"type": "user.message", "content": [{"type": "text", "text": text}]}


def setup():
    store, agents = S.MemoryStore(), A.MemoryAgentStore()
    svc = SS.Sessions(store, agents)       # no wake: the tests drive the worker
    aid = agents.create(T, model="m", instructions="be brief")["agent_id"]
    sid = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})[0]["session"]
    return store, agents, svc, sid


def texts(store, sid, kind):
    return [e.get("text") for e in store.events(T, sid) if e["type"] == kind]


def turn_states(store, sid):
    return [t["status"] for t in project.turns(store.events(T, sid),
                                               session_id=sid, agent_id=None)]


# ── checks ─────────────────────────────────────────────────────────────────

def basic() -> int:
    res = Results("one turn")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    model = Counting()
    out = Worker(store, agents, model).run(T, sid, holder="w1")

    res.check("the worker reports it went idle", out["result"] == "idle", f"{out}")
    res.check("one model call for one turn", model.calls == 1, f"{model.calls}")
    res.check("the answer quotes the conversation the fold built",
              texts(store, sid, E.AGENT_MESSAGE) == ["echo: hello"],
              f"{texts(store, sid, E.AGENT_MESSAGE)}")
    res.check("the turn is completed", turn_states(store, sid) == ["completed"],
              f"{turn_states(store, sid)}")
    res.check("the session hands the floor back",
              store.get_session(T, sid)["status"] == E.TURN_OVER)
    res.check("the counters survive the invocation",
              store.get_session(T, sid)["agent_state"].get("steps") == 1,
              f"{store.get_session(T, sid)['agent_state']}")

    log = store.events(T, sid)
    processed = project.processed_times(log)
    user = next(e for e in log if e["type"] == E.USER_MESSAGE)
    start = next(e for e in log if e["type"] == E.MODEL_REQUEST_START)
    res.check("the message was committed unprocessed", user["processed_at"] is None,
              "the log must stay append-only")
    res.check("and reads as processed when its turn started",
              processed.get(user["event_id"]) == start["ts"], f"{processed}")

    again = Worker(store, agents, model).run(T, sid, holder="w2")
    res.check("a second run with nothing pending does nothing",
              again["steps"] == 0 and model.calls == 1, f"{again}")

    svc.send(T, sid, SS.ANTHROPIC, [msg("again")])
    Worker(store, agents, model).run(T, sid, holder="w3")
    res.check("the counters accumulate across turns",
              store.get_session(T, sid)["agent_state"].get("steps") == 2)
    res.check("the fold of the whole log has no dangling call",
              F.unanswered(F.fold(store.events(T, sid))) == [])
    return res.report()


def joined() -> int:
    res = Results("messages queued together")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("one")])
    svc.send(T, sid, SS.ANTHROPIC, [msg("two")])
    model = Counting()
    Worker(store, agents, model).run(T, sid, holder="w")
    res.check("each queued message gets its own answer, as the original gives",
              model.calls == 2
              and texts(store, sid, E.AGENT_MESSAGE) == ["echo: one", "echo: two"],
              f"{model.calls} {texts(store, sid, E.AGENT_MESSAGE)}")
    kinds = [e["type"] for e in store.events(T, sid)]
    res.check("in one stretch of running: running once, usage and idle once, at the end",
              kinds.count(E.STATUS_RUNNING) == 1 and kinds.count(E.STATUS_IDLE) == 1
              and kinds[-2:] == [E.SESSION_USAGE, E.STATUS_IDLE], f"{kinds}")

    # Interrupted while the model answers: the answer is not published after it.
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("one")])

    class Interrupted(Counting):
        def step(self, messages, state, **kw):
            svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.interrupt"}])
            return super().step(messages, state, **kw)
    Worker(store, agents, Interrupted()).run(T, sid, holder="w")
    kinds = [e["type"] for e in store.events(T, sid)]
    res.check("an answer to an interrupted turn is not published",
              E.AGENT_MESSAGE not in kinds and kinds.count(E.STATUS_IDLE) == 1,
              f"{kinds}")
    return res.report()


def lease() -> int:
    res = Results("the lease")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    store.take_lease(T, sid, "someone-else", 60)
    model = Counting()
    out = Worker(store, agents, model).run(T, sid, holder="w")
    res.check("a held session is left alone", out["result"] == "leased", f"{out}")
    res.check("and nothing was asked or written",
              model.calls == 0 and texts(store, sid, E.AGENT_MESSAGE) == [])
    store.release_lease(T, sid, "someone-else")
    Worker(store, agents, model).run(T, sid, holder="w")
    res.check("once free, the work is done", model.calls == 1)
    return res.report()


def lost_wakeup() -> int:
    """A message arrives while the worker holds the lease."""
    res = Results("the lost wakeup")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("first")])
    model = Interjecting(svc, sid)
    out = Worker(store, agents, model).run(T, sid, holder="w")

    res.check("the late message was answered in the same invocation",
              turn_states(store, sid) == ["completed", "completed"],
              f"{turn_states(store, sid)} — the invocation for it could not take the "
              "lease, and this one must not have gone home")
    res.check("each turn got exactly one call", model.calls == 2, f"{model.calls}")
    res.check("the first answer did not include the late message",
              texts(store, sid, E.AGENT_MESSAGE)[0] == "echo: first",
              f"{texts(store, sid, E.AGENT_MESSAGE)}")
    res.check("and the second answered it",
              texts(store, sid, E.AGENT_MESSAGE)[1] == "echo: while you think",
              f"{texts(store, sid, E.AGENT_MESSAGE)}")
    res.check("the log has no gaps", [e["seq"] for e in store.events(T, sid)]
              == list(range(len(store.events(T, sid)))))
    res.check("the worker reports idle", out["result"] == "idle", f"{out}")
    return res.report()


def orphan() -> int:
    """A worker died after starting turn A; turn B has been queued since."""
    res = Results("an orphaned turn")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("A")])
    turn_a = project.open_turn(store.events(T, sid))
    commit(store, T, sid, [E.event(E.MODEL_REQUEST_START, turn_id=turn_a)],
           status=E.WORKING)                     # ...and then it died
    svc.send(T, sid, SS.ANTHROPIC, [msg("B")])

    Worker(store, agents, Counting()).run(T, sid, holder="w")
    answers = texts(store, sid, E.AGENT_MESSAGE)
    res.check("the orphaned turn is answered first",
              answers[:1] == ["echo: A"], f"{answers}")
    res.check("and the queued one after it", answers[1:] == ["echo: B"], f"{answers}")
    res.check("both turns end", turn_states(store, sid) == ["completed", "completed"],
              f"{turn_states(store, sid)}")
    return res.report()


def handoff() -> int:
    res = Results("the handoff")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("late in the invocation")])
    woken = []
    model = Counting()
    worker = Worker(store, agents, model, wake_self=lambda t, s: woken.append(s))
    out = worker.run(T, sid, holder="w", remaining_ms=lambda: 1_000)

    res.check("no step is started without time to finish it",
              model.calls == 0, f"{model.calls}")
    res.check("the next invocation is woken", woken == [sid], f"{woken}")
    res.check("and the lease is released for it",
              store.take_lease(T, sid, "next", 60), "the handoff kept the lease")
    store.release_lease(T, sid, "next")
    Worker(store, agents, model).run(T, sid, holder="next")
    res.check("which finds the work intact",
              texts(store, sid, E.AGENT_MESSAGE) == ["echo: late in the invocation"])
    return res.report()


def failures() -> int:
    res = Results("failures")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, Failing()).run(T, sid, holder="w")
    failed = [e for e in store.events(T, sid) if e["type"] == E.STEP_FAILED]
    res.check("a failing model fails the turn, with the reason",
              failed and "provider is down" in failed[0]["reason"], f"{failed}")
    res.check("the turn reads failed", turn_states(store, sid) == ["failed"],
              f"{turn_states(store, sid)}")
    res.check("the session stays usable",
              store.get_session(T, sid)["status"] == E.TURN_OVER)

    svc.send(T, sid, SS.ANTHROPIC, [msg("retry")])
    Worker(store, agents, Counting()).run(T, sid, holder="w")
    # The failed turn's input stays in the conversation: the user did say it, and
    # the model never saw it because the provider failed. The failure itself is an
    # operational event and is not shown to the model.
    res.check("the next message is answered, with the failed turn's words in view",
              texts(store, sid, E.AGENT_MESSAGE) == ["echo: hello / retry"],
              f"{texts(store, sid, E.AGENT_MESSAGE)}")

    res.check("an unclassified failure is of an unknown kind",
              failed and failed[0].get("error_type") == "unknown", f"{failed}")

    class NoKey(Counting):
        def step(self, messages, state, **kw):
            raise M.MissingKey("no key for openai: add OPENAI_API_KEY")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, NoKey()).run(T, sid, holder="w")
    failed = [e for e in store.events(T, sid) if e["type"] == E.STEP_FAILED]
    res.check("a missing provider key is an authentication failure, named",
              failed and failed[0]["error_type"] == "authentication"
              and "OPENAI_API_KEY" in failed[0]["reason"], f"{failed}")

    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, OutOfCredits()).run(T, sid, holder="w")
    failed = [e for e in store.events(T, sid) if e["type"] == E.STEP_FAILED]
    res.check("running out of credits is recorded as billing",
              failed and failed[0].get("error_type") == "billing", f"{failed}")

    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("use a tool")])
    Worker(store, agents, Calling()).run(T, sid, holder="w")
    log = store.events(T, sid)
    failed = [e for e in log if e["type"] == E.STEP_FAILED]
    results = [e for e in log if e["type"] == E.TOOL_RESULT]
    res.check("a call to a tool the agent does not have is answered, as an error",
              results and results[0]["is_error"] and "no tool" in results[0]["result"],
              f"{results[:1]}")
    res.check("and a model that keeps calling is stopped, by name",
              failed and "stopped at" in failed[0]["reason"]
              and len(results) == W.MAX_TOOL_ROUNDS, f"{failed} / {len(results)}")
    res.check("and no call is left dangling",
              F.unanswered(F.fold(store.events(T, sid))) == [])
    return res.report()


def mcp_tools() -> int:
    """The tool loop, with a stand-in for the MCP server: call, result, answer."""
    res = Results("MCP tools")
    import tools as TL
    store, agents, svc, _ = setup()
    agent = agents.create(T, model="m", name="researcher",
                          mcp_servers=[{"type": "url", "name": "exa",
                                        "url": "https://mcp.example/mcp"}],
                          tools=[{"type": "mcp_toolset", "mcp_server_name": "exa"}])
    made, _ = svc.create(T, SS.ANTHROPIC, {"agent": agent["agent_id"],
                                           "environment_id": "e"})
    sid = made["session"]

    class Search:
        name = "search"
        def run(self, input=None, **_):
            return f"found: {input['q']}"

    seen = {}
    def discover(agent_record, credential_for):
        seen["credential"] = credential_for("https://mcp.example/mcp")
        return {"search": (TL.Server("exa", "https://mcp.example/mcp", None), Search())}

    class Researcher:
        name = "scripted"
        def priced(self, _):
            return True
        def step(self, messages, state, *, model_name="", tools=None):
            seen["tools"] = [t.name for t in tools or []]
            if messages[-1].get("role") == "tool":
                return M.Reply(text="done: " + messages[-1]["parts"][0]["text"])
            return M.Reply(calls=[{"id": "toolu_9", "name": "search",
                                   "arguments": {"q": "aichain"}}])

    original, TL.discover = TL.discover, discover
    try:
        svc.send(T, sid, SS.ANTHROPIC, [msg("find it")])
        Worker(store, agents, Researcher()).run(T, sid, holder="w")
    finally:
        TL.discover = original
    log = store.events(T, sid)
    kinds = [e["type"] for e in log]
    call = next((e for e in log if e["type"] == E.AGENT_TOOL_CALL), {})
    result = next((e for e in log if e["type"] == E.TOOL_RESULT), {})
    res.check("the model is offered the server's tools", seen.get("tools") == ["search"],
              f"{seen}")
    res.check("the call is recorded with its server, before its result",
              call.get("calls", [{}])[0].get("server") == "exa"
              and kinds.index(E.AGENT_TOOL_CALL) < kinds.index(E.TOOL_RESULT), f"{kinds}")
    res.check("the result is the tool's", result.get("result") == "found: aichain"
              and not result.get("is_error"), f"{result}")
    answer = [e for e in log if e["type"] == E.AGENT_MESSAGE]
    res.check("and the turn ends with the answer the result led to",
              answer and answer[0]["text"] == "done: found: aichain", f"{answer}")
    res.check("a session with no vault gives the server no credential",
              seen.get("credential") is None)

    huge = TL._bounded("x" * 1_000_000)
    res.check("a huge result is cut to what an event and a model can hold, saying so",
              len(huge) < TL.MAX_RESULT_CHARS + 200 and "truncated" in huge)
    import render
    late = {**E.event(E.TOOL_RESULT, turn_id="t", call_id="c", server="exa",
                      name="search", result="r", late=True), "seq": 9}
    res.check("a result that came after an interrupt is kept, and not shown",
              render.events([late], render.ANTHROPIC, session_id="s") == []
              and render.events([late], render.OPENAI, session_id="s") == [])
    failed = {**E.event(E.STEP_FAILED, turn_id="t", reason="401",
                        error_type="mcp_authentication", mcp_server="exa"), "seq": 3}
    shown = render.events([failed], render.ANTHROPIC, session_id="s")[0]["error"]
    res.check("a refused credential is the dialect's MCP error, naming the server",
              shown["type"] == "mcp_authentication_failed_error"
              and shown["mcp_server_name"] == "exa", f"{shown}")
    return res.report()


def nothing_outlives_a_run() -> int:
    """
    A warm environment serves the next invocation — another tenant's — from the
    same process. After a run, no credential of the caller may be anywhere in it:
    not the vault's bearer token the MCP tools carried, not the provider key.
    """
    res = Results("nothing of the caller outlives a run")
    import gc
    import os
    import secrets
    import tools as TL
    import vault as VA
    import yait_aichain.tools as library_tools

    os.environ["YAIT_VAULT_KEY"] = VA.new_key()
    VA.reset_cache()
    store, agents, svc, _ = setup()
    vaults = VA.Vaults(VA.MemoryVaults())
    url = "https://mcp.example/mcp"
    v = vaults.create(T, name="v", metadata=None)
    vaults.add(T, v["vault_id"], auth={"type": "static_bearer", "mcp_server_url": url,
                                       "token": "bearer-" + secrets.token_hex(8) + "-mark"})
    agent = agents.create(T, model="m", name="r",
                          mcp_servers=[{"type": "url", "name": "s", "url": url}],
                          tools=[{"type": "mcp_toolset", "mcp_server_name": "s"}])
    svc.vaults = vaults
    made, _ = svc.create(T, SS.ANTHROPIC, {"agent": agent["agent_id"],
                                           "environment_id": "e",
                                           "vault_ids": [v["vault_id"]]})

    class Tool:
        name, description = "search", "search"
        parameters = {"type": "object", "properties": {}}

        def __init__(self, headers):
            self.headers = headers          # as aichain's MCPTool keeps them

        def run(self, input=None, **_):
            return "found"

        def schema(self):
            return {"type": "function", "function": {"name": self.name,
                    "description": self.description, "parameters": self.parameters}}

    class Model:
        name = "scripted"
        def priced(self, _):
            return True
        def step(self, messages, state, *, model_name="", tools=None):
            if messages[-1].get("role") == "tool":
                return M.Reply(text="done")
            return M.Reply(calls=[{"id": "c1", "name": "search", "arguments": {}}])

    original = library_tools.MCPTools
    library_tools.MCPTools = lambda url, headers=None, **_: [Tool(dict(headers or {}))]
    try:
        svc.send(T, made["session"], SS.ANTHROPIC, [msg("go")])
        Worker(store, agents, Model(), vaults=vaults).run(
            T, made["session"], holder="w",
            # Built at run time: a constant would live in the code object itself.
            provider_key="sk-provider-" + secrets.token_hex(4) + "-mark")
    finally:
        library_tools.MCPTools = original

    answered = [e for e in store.events(T, made["session"]) if e["type"] == E.AGENT_MESSAGE]
    res.check("the turn ran, with the vault's token on the tool",
              answered and answered[0]["text"] == "done", f"{answered}")
    res.check("the listed tools are gone when the run ends", TL._listed == {},
              f"{list(TL._listed)}")
    res.check("and no key is current", M._key_for("ANTHROPIC_API_KEY") == "")

    gc.collect()
    found = []
    everything = gc.get_objects()
    for obj in everything:
        if obj is everything:
            continue                     # the search's own list, not the process
        values = (obj.values() if isinstance(obj, dict) else
                  obj if isinstance(obj, (list, tuple)) else
                  [obj.cell_contents] if type(obj).__name__ == "cell"
                  and getattr(obj, "cell_contents", None) is not None else [])
        try:
            for value in values:
                if isinstance(value, str) and value.endswith("-mark") and \
                        ("bearer-" in value or "sk-provider-" in value):
                    # Which secret, and who holds the container: the failure
                    # has to say where to look.
                    holders = [getattr(getattr(r, "f_code", None), "co_qualname", None)
                               or type(r).__name__ for r in gc.get_referrers(obj)
                               if r is not found][:4]
                    found.append(f"{type(obj).__name__} of {len(obj)} "
                                 f"({value.split('-')[0]}…) held by {holders}")
        except (RuntimeError, ValueError):
            continue
    res.check("no credential of the caller is left anywhere in the process",
              not found, f"found in {found}")
    os.environ.pop("YAIT_VAULT_KEY", None)
    VA.reset_cache()
    return res.report()


def openai_events() -> int:
    """The log the worker wrote, as OpenAI's turn and status events."""
    res = Results("OpenAI turn and status events")

    def rendered(store, sid):
        log = store.events(T, sid)
        timeline = project.turn_timeline(log, session_id=sid, agent_id="a")
        session = {"id": sid, "status": "idle", "last_active_at": 0}
        return log, render.events(log, render.OPENAI, session_id=sid,
                                  session=session, timeline=timeline)

    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, Counting()).run(T, sid, holder="w")
    log, evs = rendered(store, sid)
    kinds = [e["type"].removeprefix("agent.session.") for e in evs]
    res.check("one turn reads as the dialect's lifecycle, in order",
              # The original's order (tests/goldens/openai/second_message.json).
              kinds == ["created", "turn.created", "turn.item.added", "in_progress",
                        "turn.in_progress", "turn.item.added", "turn.content_part.added",
                        "turn.output_text.delta", "turn.output_text.done",
                        "turn.content_part.done", "turn.item.done", "turn.completed",
                        "idle"], f"{kinds}")
    res.check("every event has its own id",
              len({e["event_id"] for e in evs}) == len(evs),
              f"{[e['event_id'] for e in evs]}")
    snapshots = {e["type"]: e["turn"]["status"] for e in evs if "turn" in e}
    res.check("each turn event carries the turn as it stood then",
              snapshots == {"agent.session.turn.created": "queued",
                            "agent.session.turn.in_progress": "in_progress",
                            "agent.session.turn.completed": "completed"}, f"{snapshots}")
    final = render.turn(project.turns(log, session_id=sid, agent_id="a")[0])
    ended = next(e for e in evs if e["type"] == "agent.session.turn.completed")
    res.check("and the last one is what /turns says", ended["turn"] == final,
              f"{ended['turn']} vs {final}")
    indexes = [e["output_index"] for e in evs if e["type"].startswith("agent.session.turn.item")]
    res.check("input has no output index; the answer is output 0",
              indexes == [None, 0, 0], f"{indexes}")

    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, OutOfCredits()).run(T, sid, holder="w")
    _, evs = rendered(store, sid)
    failed = [e for e in evs if e["type"] == "agent.session.turn.failed"]
    res.check("a failed turn is turn.failed, with the dialect's code",
              failed and failed[0]["turn"]["error"]["code"] == "credit_balance_exhausted",
              f"{[e['type'] for e in evs]}")
    res.check("and the session still goes idle after it",
              evs[-1]["type"] == "agent.session.idle", f"{evs[-1]['type']}")
    return res.report()


def overrides() -> int:
    res = Results("session overrides")
    store, agents, svc, sid = setup()

    class Named(Counting):
        def step(self, messages, state, **kw):
            self.names = getattr(self, "names", []) + [kw.get("model_name")]
            return super().step(messages, state, **kw)
    model = Named()
    svc.send(T, sid, SS.ANTHROPIC, [msg("one")])
    Worker(store, agents, model).run(T, sid, holder="w")
    svc.update(T, sid, SS.OPENAI, {"agent": {"model": "claude-sonnet-5"}})
    svc.send(T, sid, SS.ANTHROPIC, [msg("two")])
    Worker(store, agents, model).run(T, sid, holder="w")
    res.check("a model changed on the session is used from the next turn",
              model.names == ["m", "claude-sonnet-5"], f"{model.names}")
    return res.report()


def budget() -> int:
    res = Results("the budget")

    def limit(cents):
        return {"type": "limit", "max_list_cost": {"amount": cents, "currency": "USD"}}

    class Costing(Counting):
        """Echo that costs 30 cents a step, as the library would count it."""
        def step(self, messages, state, **kw):
            state["cost"] = (state.get("cost") or 0.0) + 0.30
            return super().step(messages, state, **kw)

    store, agents, svc, sid = setup()
    svc.update(T, sid, SS.ANTHROPIC, {"budget": limit("50")})
    model = Costing()
    for text in ("one", "two", "three"):
        svc.send(T, sid, SS.ANTHROPIC, [msg(text)])
        Worker(store, agents, model).run(T, sid, holder="w")
    res.check("requests are issued until the cost reaches the budget, then not",
              model.calls == 2, f"{model.calls} calls — 0, 30, then 60 of 50 cents")
    log = store.events(T, sid)
    idle = [e.get("stop_reason") for e in log if e["type"] == E.STATUS_IDLE]
    res.check("the turn that was not run ends idle with budget_reached",
              idle[-1:] == ["budget_reached"], f"{idle}")
    failed = [e for e in log if e["type"] == E.STEP_FAILED]
    res.check("recorded as a budget failure, with what was spent",
              failed and failed[0]["error_type"] == "budget"
              and "$0.60 of its $0.50" in failed[0]["reason"], f"{failed}")
    res.check("no request was started for it",
              sum(1 for e in log if e["type"] == E.MODEL_REQUEST_START) == 2)
    res.check("the session is at its ceiling",
              store.get_session(T, sid)["status"] == E.AT_CEILING)
    unread = [e for e in log if e["type"] == E.USER_MESSAGE and e["turn_id"] ==
              failed[0]["turn_id"]]
    res.check("and its message still reads as processed",
              all(project.processed_times(log).get(e["event_id"]) for e in unread),
              "a message the session decided on must not read as pending forever")

    svc.update(T, sid, SS.ANTHROPIC, {"budget": limit("100")})
    svc.send(T, sid, SS.ANTHROPIC, [msg("four")])
    Worker(store, agents, model).run(T, sid, holder="w")
    res.check("raising the budget lets the next turn run", model.calls == 3,
              f"{model.calls}")

    class Unpriced(Counting):
        def priced(self, model_name):
            return False
    store, agents, svc, sid = setup()
    svc.update(T, sid, SS.ANTHROPIC, {"budget": limit("100000")})
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    unpriced = Unpriced()
    Worker(store, agents, unpriced).run(T, sid, holder="w")
    reason = [e["reason"] for e in store.events(T, sid) if e["type"] == E.STEP_FAILED]
    res.check("a model the budget cannot measure is not run under one",
              unpriced.calls == 0 and reason and "no list price" in reason[0],
              f"{unpriced.calls} {reason}")
    svc.update(T, sid, SS.ANTHROPIC, {"budget": None})
    svc.send(T, sid, SS.ANTHROPIC, [msg("again")])
    Worker(store, agents, unpriced).run(T, sid, holder="w")
    res.check("and runs once the budget is removed", unpriced.calls == 1)
    return res.report()


def caller_key() -> int:
    res = Results("the caller's provider key")
    store, agents, svc, sid = setup()

    class Seeing(Counting):
        def step(self, messages, state, **kw):
            self.seen = M._key_for("ANTHROPIC_API_KEY")
            return super().step(messages, state, **kw)
    model = Seeing()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    Worker(store, agents, model).run(T, sid, holder="w", provider_key="sk-caller")
    res.check("the model is called with the key the caller brought",
              model.seen == "sk-caller", f"{model.seen!r}")
    res.check("and nothing keeps it after the run", M._key_for("ANTHROPIC_API_KEY") == "")
    res.check("nor does it reach the log",
              not any("sk-caller" in str(e) for e in store.events(T, sid)))
    res.check("nor the session record", "sk-caller" not in str(store.get_session(T, sid)))
    return res.report()


def purge() -> int:
    res = Results("the purge after a delete")
    store, agents, svc, sid = setup()
    svc.send(T, sid, SS.ANTHROPIC, [msg("hello")])
    model = Counting()
    Worker(store, agents, model).run(T, sid, holder="w")
    svc.delete(T, sid)
    out = Worker(store, agents, model).run(T, sid, holder="w")
    res.check("the worker purges a deleted session", out["result"] == "purged", f"{out}")
    res.check("nothing of it is left but the tombstone",
              store.events(T, sid) == []
              and set(store.get_session(T, sid)) <= {"pk", "sk", "tenant", "session",
                                                     "deleted_at", "status", "purged"},
              f"{store.get_session(T, sid)}")
    res.check("and no model was asked on the way", model.calls == 1, f"{model.calls}")
    again = Worker(store, agents, model).run(T, sid, holder="w")
    res.check("a late wake-up for it finds nothing to do",
              again["result"] == "gone", f"{again}")

    class Slow(S.MemoryStore):
        def purge(self, tenant, session, *, keep_going=lambda: True):
            return keep_going() and super().purge(tenant, session)
    store = Slow()
    agents = A.MemoryAgentStore()
    svc = SS.Sessions(store, agents)
    aid = agents.create(T, model="m")["agent_id"]
    sid = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})[0]["session"]
    svc.delete(T, sid)
    woken = []
    out = Worker(store, agents, model, wake_self=lambda t, s: woken.append(s)).run(
        T, sid, holder="w", remaining_ms=lambda: 1_000)
    res.check("out of time, the purge hands off to the next invocation",
              out["result"] == "handed_off" and woken == [sid], f"{out} {woken}")
    res.check("leaving the tombstone for it",
              (store.get_session(T, sid) or {}).get("status") == E.DELETED)
    return res.report()


def main() -> int:
    rc = 0
    for check in (basic, joined, lease, lost_wakeup, orphan, handoff, failures,
                  openai_events, overrides, budget, caller_key, purge, mcp_tools, nothing_outlives_a_run):
        rc |= check()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
