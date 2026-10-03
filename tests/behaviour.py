#!/usr/bin/env python3
"""
Layer two: behaviour. The same scenario against the original and against us, and
the two traces compared.

A **scenario** is a script of client actions — one turn, two messages in one
request, archiving, deleting, a malformed request. What it observes is a
**normalised trace**: event types in order, the `stop_reason`, statuses, HTTP
status codes and error types — never ids, timestamps or the model's words, which
differ legitimately. See docs/testing.md.

A **golden** is a trace kept in `tests/goldens/<dialect>/<scenario>.json`, with
where it was recorded. Recorded from the original, a mismatch is a parity defect.
Recorded from us, it is provisional: it catches a change in our behaviour, and
the report says plainly that it has not been compared with the original yet.

    python3 tests/behaviour.py                              # compare, local, echo
    python3 tests/behaviour.py --base-url https://… --key … # compare, a deployment
    python3 tests/behaviour.py --record                     # provisional goldens
    ANTHROPIC_API_KEY=… python3 tests/behaviour.py --record --target original \\
        --dialect anthropic                                 # the real thing

Recording from the original costs money and needs access to the Agents APIs;
every scenario is run twice and must give the same trace both times, or nothing
is written — a golden that would not reproduce is worse than none.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request

import anthropic

import conformance_harness as harness

ROOT = harness.ROOT
GOLDENS = ROOT / "tests" / "goldens"

ORIGINAL = {"anthropic": "https://api.anthropic.com",
            "openai": "https://api.openai.com/v1"}
#: The originals run only their own models, and the comparison holds the model
#: fixed, so each dialect's scenarios use its own provider's cheapest current one.
MODEL = {"anthropic": os.environ.get("YAIT_BEHAVIOUR_MODEL_ANTHROPIC",
                                     "claude-haiku-4-5-20251001"),
         "openai": os.environ.get("YAIT_BEHAVIOUR_MODEL_OPENAI", "gpt-6-luna")}

#: Differences we know of, as narrowly as they can be named: event kinds the
#: original emits and we do not yet, and steps of a scenario that differ for a
#: stated reason. They are taken out of both traces before comparing, so anything
#: else in the same scenario still counts; and a rule that no longer takes anything
#: out turns the suite red until it is removed from here.
KNOWN_DIVERGENCES: dict[str, dict] = {
    "anthropic/* threads": {
        "kinds": ["session.thread_status_running", "session.thread_status_idle"],
        "why": "threads belong to multi-agent sessions; the primary thread's events "
               "come with the threads resource and subagents"},
    "anthropic/errors id-format": {
        "steps": ["retrieve missing", "send to missing", "missing agent"],
        "why": "the original rejects an id not in its format with 400; we answer "
               "404 for any id we do not hold. A well-formed missing id is 404 on "
               "both (anthropic/missing_well_formed)"},
}

WAIT_S = 120


class Trace:
    """What a scenario observed, in order, with nothing that differs legitimately."""

    def __init__(self) -> None:
        self.steps: list[list] = []

    def __call__(self, label: str, value) -> None:
        self.steps.append([label, value])


# ── Anthropic: driven by the official SDK, against either host ─────────────

class AnthropicTarget:
    dialect = "anthropic"

    def __init__(self, base: str, key: str, original: bool) -> None:
        self.original = original
        # The original takes the provider key; we take the installation key, and
        # with no provider key the deployment's own keys answer.
        self.c = (harness.anthropic_client(base, provider_key=key, original=True,
                                           max_retries=2) if original else
                  harness.anthropic_client(
                      base, yait_key=key, max_retries=2,
                      provider_key=harness.provider_key_for(MODEL["anthropic"])))
        self.cleanup: list = []
        if original:
            env = self.c.beta.environments.create(name="yait-behaviour")
            self.environment = env.id
            self.cleanup.append(lambda: self.c.beta.environments.delete(env.id))
        else:
            self.environment = "env_none"

    def agent(self) -> str:
        made = self.c.beta.agents.create(model=MODEL["anthropic"], name="behaviour",
                                         system="Answer in one short sentence.")
        self.cleanup.append(lambda: self.c.beta.agents.archive(made.id))
        return made.id

    def session(self, agent_id: str, **extra):
        made = self.c.beta.sessions.create(agent=agent_id,
                                           environment_id=self.environment, **extra)
        self.cleanup.append(lambda: self.c.beta.sessions.delete(made.id))
        return made

    def say(self, text: str) -> dict:
        return {"type": "user.message", "content": [{"type": "text", "text": text}]}

    def idle(self, session_id: str, turns: int) -> str:
        """
        Wait until `turns` turns have ended; the session's status then.

        Counted by the idle events, not read off the status: right after a message
        is sent, a host that has not taken it up yet still says idle, and a wait
        that trusts that returns before the turn has begun.
        """
        status = ""
        for _ in range(WAIT_S * 2):
            ended = sum(1 for e in self.c.beta.sessions.events.list(session_id, order="asc")
                        if e.type == "session.status_idle")
            status = self.c.beta.sessions.retrieve(session_id).status
            if ended >= turns and status not in ("running", "rescheduling"):
                return status
            time.sleep(0.5)
        return f"still {status} after {WAIT_S}s"

    def events(self, session_id: str) -> list[str]:
        out = []
        for ev in self.c.beta.sessions.events.list(session_id, order="asc"):
            raw = ev.model_dump()
            kind = raw["type"]
            if kind == "session.status_idle":
                kind += ":" + (raw.get("stop_reason") or {}).get("type", "?")
            elif kind == "session.error":
                kind += ":" + (raw.get("error") or {}).get("type", "?")
            out.append(kind)
        return out

    def refused(self, call) -> list:
        """An expected error: its status and type. A success is recorded as such."""
        try:
            call()
            return ["ok"]
        except anthropic.APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            return [exc.status_code, (body.get("error") or body).get("type")]

    def close(self) -> None:
        for undo in reversed(self.cleanup):
            try:
                undo()
            except Exception:  # noqa: BLE001 — best effort; already gone is fine
                pass


def a_one_turn(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    trace("created", s.status)
    t.c.beta.sessions.events.send(s.id, events=[t.say("Say hello.")])
    trace("after", t.idle(s.id, 1))
    trace("events", t.events(s.id))


def a_two_messages_one_request(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    t.c.beta.sessions.events.send(s.id, events=[t.say("Say hello."), t.say("Briefly.")])
    trace("after", t.idle(s.id, 1))
    trace("events", t.events(s.id))


def a_two_turns(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    for n, text in enumerate(("Say hello.", "Say goodbye."), start=1):
        t.c.beta.sessions.events.send(s.id, events=[t.say(text)])
        trace("after", t.idle(s.id, n))
    trace("events", t.events(s.id))


def a_interrupt_idle(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    trace("interrupt", t.refused(lambda: t.c.beta.sessions.events.send(
        s.id, events=[{"type": "user.interrupt"}])))
    trace("after", t.idle(s.id, 0))
    trace("events", t.events(s.id))


def a_archive(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    t.c.beta.sessions.events.send(s.id, events=[t.say("Say hello.")])
    t.idle(s.id, 1)
    trace("archived", t.c.beta.sessions.archive(s.id).status)
    trace("send after", t.refused(lambda: t.c.beta.sessions.events.send(
        s.id, events=[t.say("Still there?")])))
    trace("archive again", t.refused(lambda: t.c.beta.sessions.archive(s.id)))
    trace("retrieve", t.c.beta.sessions.retrieve(s.id).status)


def a_delete(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent())
    t.c.beta.sessions.events.send(s.id, events=[t.say("Say hello.")])
    t.idle(s.id, 1)
    trace("deleted", t.c.beta.sessions.delete(s.id).type)
    trace("retrieve", t.refused(lambda: t.c.beta.sessions.retrieve(s.id)))
    trace("delete again", t.refused(lambda: t.c.beta.sessions.delete(s.id)))
    trace("send after", t.refused(lambda: t.c.beta.sessions.events.send(
        s.id, events=[t.say("Hello?")])))


def a_update_metadata(t: AnthropicTarget, trace: Trace) -> None:
    s = t.session(t.agent(), metadata={"a": "1", "b": "2"})
    got = t.c.beta.sessions.update(s.id, metadata={"b": None, "c": "3"}, title="renamed")
    trace("metadata", dict(sorted(got.metadata.items())))
    trace("title", got.title)


def a_two_messages_apart(t: AnthropicTarget, trace: Trace) -> None:
    """Two messages sent one after the other, not waiting: one answer, or two?"""
    s = t.session(t.agent())
    t.c.beta.sessions.events.send(s.id, events=[t.say("Say hello.")])
    t.c.beta.sessions.events.send(s.id, events=[t.say("Now say goodbye.")])
    trace("after", t.idle(s.id, 1))
    time.sleep(3)              # if a second answer is coming, let it come
    trace("after", t.idle(s.id, 1))
    trace("events", t.events(s.id))


def a_missing_well_formed(t: AnthropicTarget, trace: Trace) -> None:
    """Ids in the original's own format that name nothing: 404, or 400?"""
    trace("retrieve missing session", t.refused(
        lambda: t.c.beta.sessions.retrieve("sesn_01" + "A" * 22)))
    trace("send to missing session", t.refused(lambda: t.c.beta.sessions.events.send(
        "sesn_01" + "A" * 22, events=[t.say("hi")])))
    trace("retrieve missing agent", t.refused(
        lambda: t.c.beta.agents.retrieve("agent_01" + "A" * 22)))


def a_errors(t: AnthropicTarget, trace: Trace) -> None:
    agent_id = t.agent()
    trace("retrieve missing", t.refused(
        lambda: t.c.beta.sessions.retrieve("sesn_does_not_exist")))
    trace("send to missing", t.refused(lambda: t.c.beta.sessions.events.send(
        "sesn_does_not_exist", events=[t.say("hi")])))
    trace("unknown field", t.refused(lambda: t.c.beta.sessions.create(
        agent=agent_id, environment_id=t.environment, extra_body={"colour": "red"})))
    trace("missing agent", t.refused(lambda: t.c.beta.sessions.create(
        agent="agent_does_not_exist", environment_id=t.environment)))


def needs_model(*scenarios) -> None:
    """Scenarios whose trace includes a model's turn — they need a provider key."""
    for scenario in scenarios:
        scenario.needs_model = True


needs_model(a_one_turn, a_two_messages_one_request, a_two_turns, a_archive, a_delete,
            a_two_messages_apart)

#: A trace that depends on the model taking real time: the second message must
#: arrive while the first is being answered. The echo backend answers in
#: milliseconds, so locally it cannot; against a deployment it runs.
a_two_messages_apart.needs_real_latency = True

ANTHROPIC_SCENARIOS = [a_one_turn, a_two_messages_one_request, a_two_messages_apart,
                       a_missing_well_formed, a_two_turns,
                       a_interrupt_idle, a_archive, a_delete, a_update_metadata,
                       a_errors]


# ── OpenAI: raw HTTP — their SDK has no Agents API yet ─────────────────────

class OpenAITarget:
    dialect = "openai"

    def __init__(self, base: str, key: str, original: bool) -> None:
        self.original = original
        self.base = base.rstrip("/") if original else f"{base.rstrip('/')}/openai/v1"
        self.key = key
        self.cleanup: list = []

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, method=method)
        for name, value in self._headers().items():
            req.add_header(name, value)
        if body is not None:
            req.add_header("content-type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=60) as reply:
                status, raw = reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {}

    def agent(self) -> str:
        _, made = self.call("POST", "/agents", {"model": MODEL["openai"],
                                                "name": "behaviour",
                                                "instructions": "Answer in one short sentence."})
        self.cleanup.append(lambda: self.call("DELETE", f"/agents/{made.get('id')}"))
        return made.get("id", "missing")

    def session(self, agent_id: str, **extra) -> tuple[int, dict]:
        status, made = self.call("POST", "/agents/sessions",
                                 {"agent_id": agent_id, "environment": {"type": "none"},
                                  **extra})
        if made.get("id"):
            self.cleanup.append(
                lambda: self.call("DELETE", f"/agents/sessions/{made['id']}"))
        return status, made

    def _headers(self) -> dict:
        return (harness.openai_headers(provider_key=self.key) if self.original
                else harness.openai_headers(
                    yait_key=self.key,
                    provider_key=harness.provider_key_for(MODEL["openai"])))

    def say(self, text: str) -> dict:
        return {"type": "agent.session.input.message",
                "input": [{"role": "user", "type": "message",
                           "content": [{"type": "input_text", "text": text}]}]}

    def idle(self, session_id: str, turns: int) -> str:
        """Wait until `turns` turns have ended — counted, not read off the status."""
        status = ""
        for _ in range(WAIT_S * 2):
            listed = self.call("GET", f"/agents/sessions/{session_id}/turns?order=asc")[1]
            done = [x for x in listed.get("data") or []
                    if x.get("status") in ("completed", "failed", "cancelled", "waiting")]
            status = self.call("GET", f"/agents/sessions/{session_id}")[1].get("status", "")
            if len(done) >= turns and status not in ("in_progress", ""):
                return status
            time.sleep(0.5)
        return f"still {status} after {WAIT_S}s"

    def watch(self, session_id: str):
        """
        The events that happen from now until `stop()` — what a client sees.

        The original's GET /events is a **live** stream: it carries what happens
        after it is opened and replays nothing, so it is opened before the send and
        read on a thread. Ours answers with the history, so for ours the events
        already there are counted now and those after them taken at the end. Either
        way the trace is "what this send caused", on both hosts alike.
        """
        return _StreamWatch(self, session_id) if self.original else _PageWatch(self, session_id)

    def kinds(self, raw: list[dict]) -> list[str]:
        out = []
        for ev in raw:
            kind = ev.get("type", "?")
            if kind == "agent.session.turn.failed":
                kind += ":" + ((ev.get("turn") or {}).get("error") or {}).get("code", "?")
            # How many deltas a text arrives in depends on its length and the
            # network, not on behaviour: a run of them is one step in a trace.
            if kind.endswith(".delta") and out and out[-1] == kind:
                continue
            out.append(kind)
        return out

    def pages(self, session_id: str) -> list[dict]:
        out, after = [], ""
        while True:
            _, page = self.call("GET", f"/agents/sessions/{session_id}/events?order=asc"
                                       + (f"&after={after}" if after else ""))
            out += page.get("data") or []
            if not page.get("has_more"):
                return out
            after = page.get("last_id", "")

    def refused(self, status: int, body: dict) -> list:
        if status < 400:
            return [status]
        error = body.get("error") or {}
        return [status, error.get("type"), error.get("code")]

    def close(self) -> None:
        for undo in reversed(self.cleanup):
            try:
                undo()
            except Exception:  # noqa: BLE001
                pass


class _PageWatch:
    def __init__(self, target: "OpenAITarget", session_id: str) -> None:
        self.target, self.session_id = target, session_id
        self.before = len(target.pages(session_id))

    def stop(self) -> list[str]:
        return self.target.kinds(self.target.pages(self.session_id)[self.before:])


class _StreamWatch:
    def __init__(self, target: "OpenAITarget", session_id: str) -> None:
        import threading
        self.target, self.events, self.reply = target, [], None
        req = urllib.request.Request(f"{target.base}/agents/sessions/{session_id}/events")
        for name, value in target._headers().items():
            req.add_header(name, value)
        req.add_header("accept", "text/event-stream")
        self.reply = urllib.request.urlopen(req, timeout=WAIT_S)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            for line in self.reply:
                line = line.decode(errors="replace").strip()
                if line.startswith("data:"):
                    try:
                        self.events.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        except Exception:  # noqa: BLE001 — closed from stop(), or timed out: done
            pass

    def stop(self) -> list[str]:
        time.sleep(1.0)            # the last events of a turn trail its status by a beat
        try:
            self.reply.close()
        except Exception:  # noqa: BLE001
            pass
        self.thread.join(timeout=5)
        return self.target.kinds(self.events)


def o_one_turn(t: OpenAITarget, trace: Trace) -> None:
    status, s = t.session(t.agent(), input="Say hello.")
    trace("created", [status, s.get("status")])
    trace("after", t.idle(s.get("id", ""), 1))
    # No events here: the turn began with the create, before any stream could be
    # opened on it. The events of a turn are traced in o_second_message.
    _, turns = t.call("GET", f"/agents/sessions/{s.get('id')}/turns?order=asc")
    trace("turns", [x.get("status") for x in turns.get("data") or []])
    _, items = t.call("GET", f"/agents/sessions/{s.get('id')}/items?order=asc")
    trace("items", [[x.get("type"), x.get("role"), x.get("phase")]
                    for x in items.get("data") or []])


def o_second_message(t: OpenAITarget, trace: Trace) -> None:
    _, s = t.session(t.agent(), input="Say hello.")
    t.idle(s.get("id", ""), 1)
    watch = t.watch(s.get("id", ""))
    status, _ = t.call("POST", f"/agents/sessions/{s.get('id')}/events",
                       {"events": [t.say("Say goodbye.")]})
    trace("sent", status)
    trace("after", t.idle(s.get("id", ""), 2))
    trace("events of the second turn", watch.stop())
    _, turns = t.call("GET", f"/agents/sessions/{s.get('id')}/turns?order=asc")
    trace("turns", [x.get("status") for x in turns.get("data") or []])


def o_cancel_idle(t: OpenAITarget, trace: Trace) -> None:
    _, s = t.session(t.agent(), input="Say hello.")
    t.idle(s.get("id", ""), 1)
    trace("cancel", t.refused(*t.call(
        "POST", f"/agents/sessions/{s.get('id')}/events",
        {"events": [{"type": "agent.session.input.cancel"}]})))
    trace("after", t.idle(s.get("id", ""), 1))


def o_delete(t: OpenAITarget, trace: Trace) -> None:
    _, s = t.session(t.agent(), input="Say hello.")
    t.idle(s.get("id", ""), 1)
    status, gone = t.call("DELETE", f"/agents/sessions/{s.get('id')}")
    trace("deleted", [status, gone.get("object"), gone.get("deleted")])
    trace("get", t.refused(*t.call("GET", f"/agents/sessions/{s.get('id')}")))
    trace("delete again", t.refused(*t.call("DELETE", f"/agents/sessions/{s.get('id')}")))
    # Whether "deleted" is remembered, or any id is deleted: an id in the original's
    # format that was never a session.
    trace("delete unknown", t.refused(*t.call("DELETE", "/agents/sessions/sess_" + "0" * 50)))


def o_update_metadata(t: OpenAITarget, trace: Trace) -> None:
    _, s = t.session(t.agent(), input="Say hello.", metadata={"a": "1"})
    t.idle(s.get("id", ""), 1)
    status, got = t.call("POST", f"/agents/sessions/{s.get('id')}",
                         {"metadata": {"b": "2"}})
    trace("replaced", [status, got.get("metadata")])
    status, got = t.call("POST", f"/agents/sessions/{s.get('id')}", {"metadata": None})
    trace("cleared", [status, got.get("metadata")])


def o_errors(t: OpenAITarget, trace: Trace) -> None:
    agent_id = t.agent()
    trace("get missing", t.refused(*t.call("GET", "/agents/sessions/sess_does_not_exist")))
    trace("unknown field", t.refused(*t.session(agent_id, input="hi", colour="red")))
    trace("no input without an environment", t.refused(*t.session(agent_id)))
    trace("missing agent", t.refused(*t.session("agent_does_not_exist", input="hi")))


needs_model(o_one_turn, o_second_message, o_cancel_idle, o_delete, o_update_metadata)

OPENAI_SCENARIOS = [o_one_turn, o_second_message, o_cancel_idle, o_delete,
                    o_update_metadata, o_errors]


# ── recording and comparing ────────────────────────────────────────────────

def run_scenario(make, scenario) -> list:
    target = make()
    trace = Trace()
    try:
        scenario(target, trace)
    except Exception as exc:  # noqa: BLE001
        # A call that failed is behaviour too, and the rest of the recording is
        # worth having: it becomes the scenario's last step, as a client sees it.
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        kind = ((body or {}).get("error") or {}).get("type") if isinstance(body, dict) else None
        trace("raised", [type(exc).__name__, status, kind])
    finally:
        target.close()
    return trace.steps


def golden_path(dialect: str, scenario) -> pathlib.Path:
    return GOLDENS / dialect / f"{scenario.__name__[2:]}.json"


def record(dialect: str, scenarios, make, source: dict) -> int:
    rc = 0
    for scenario in scenarios:
        first, second = run_scenario(make, scenario), run_scenario(make, scenario)
        name = scenario.__name__[2:]
        if first != second:
            print(f"  UNSTABLE   {dialect}/{name} — not written; the two runs differ:")
            print(_diff(first, second, "first run", "second run"))
            rc = 1
            continue
        path = golden_path(dialect, scenario)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": source, "trace": first}, indent=1,
                                   ensure_ascii=False) + "\n")
        print(f"  recorded   {dialect}/{name}")
    return rc


def compare(dialect: str, scenarios, make, *, without_model: bool = False,
            local: bool = False) -> int:
    rows = []
    for scenario in scenarios:
        name = f"{dialect}/{scenario.__name__[2:]}"
        path = golden_path(dialect, scenario)
        if local and getattr(scenario, "needs_real_latency", False):
            rows.append((name, "skipped", "the echo backend answers before a second "
                                          "message can arrive; run against a deployment"))
            continue
        if without_model and getattr(scenario, "needs_model", False):
            # Against a deployment a model is called only with the caller's own
            # key; with none, this trace would show a refusal, not the behaviour.
            rows.append((name, "skipped", f"no provider key for {MODEL[dialect]} — "
                                          "run it as a client with one"))
            continue
        if not path.exists():
            rows.append((name, "NO GOLDEN", "record one: --record"))
            continue
        golden = json.loads(path.read_text())
        ours = run_scenario(make, scenario)
        rules = _divergences(name)
        used = [r for r in rules if _removes(r, golden["trace"], ours)]
        same = _without(ours, rules) == _without(golden["trace"], rules)
        provisional = golden["source"].get("target") != "original"
        stale = [r["why"] for r in rules if r not in used
                 and r.get("steps") and name.split("/")[0] + "/" in name]
        if same and used:
            state, detail = "same, but for known", "; ".join(r["why"] for r in used)
        elif same:
            state = "same as ours" if provisional else "same as original"
            detail = ""
        else:
            state = "CHANGED" if provisional else "DIFFERS"
            detail = _diff(_without(golden["trace"], rules), _without(ours, rules),
                           f"golden ({golden['source'].get('target')})", "now")
        if stale and not provisional:
            state, detail = "STALE RULE", ("a known divergence no longer removes "
                                           "anything; remove it: " + "; ".join(stale))
        rows.append((name, state, detail))

    width = max(len(n) for n, _, _ in rows)
    for name, state, detail in rows:
        print(f"  {state:17}  {name:<{width}}")
        if detail and state not in ("same as ours", "same as original", "skipped",
                                    "same, but for known"):
            print("\n".join(f"  {'':17}  {line}" for line in detail.splitlines()))
    failing = {"NO GOLDEN", "CHANGED", "DIFFERS", "STALE RULE"}
    parity = sum(1 for _, s, _ in rows if s in ("same as original",
                                                 "same, but for known"))
    provisional = sum(1 for _, s, _ in rows if s == "same as ours")
    skipped = [n for n, s, _ in rows if s == "skipped"]
    print(f"\n{dialect}: {parity} match the original, {provisional} match a provisional "
          f"golden recorded from us — not yet compared with the original"
          + (f"; {len(skipped)} skipped" if skipped else ""))
    return 1 if any(s in failing for _, s, _ in rows) else 0


def _divergences(name: str) -> list[dict]:
    """The rules that apply to this scenario: its own, and its dialect's."""
    dialect = name.split("/")[0]
    return [rule for key, rule in KNOWN_DIVERGENCES.items()
            if key.split()[0] in (name, f"{dialect}/*")]


def _without(trace: list, rules: list[dict]) -> list:
    """A trace with the named divergences taken out, and nothing else."""
    kinds = {k for r in rules for k in r.get("kinds", [])}
    steps = {s for r in rules for s in r.get("steps", [])}
    out = []
    for label, value in trace:
        if label in steps:
            continue
        if isinstance(value, list) and kinds:
            value = [v for v in value
                     if not (isinstance(v, str) and v.split(":")[0] in kinds)]
        out.append([label, value])
    return out


def _removes(rule: dict, *traces) -> bool:
    """Whether a rule takes anything out of these traces."""
    return any(_without(t, [rule]) != t for t in traces)


def _diff(a: list, b: list, name_a: str, name_b: str) -> str:
    lines_a = [json.dumps(step, ensure_ascii=False) for step in a]
    lines_b = [json.dumps(step, ensure_ascii=False) for step in b]
    return "\n".join(difflib.unified_diff(lines_a, lines_b, name_a, name_b, lineterm="",
                                          n=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialect", choices=("anthropic", "openai", "both"), default="both")
    ap.add_argument("--target", choices=("ours", "original"), default="ours")
    ap.add_argument("--base-url")
    ap.add_argument("--key")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--port", type=int, default=8133)
    ap.add_argument("--env-file", action="append",
                    help="read provider keys from here (default: .env here and in "
                         "../aichain); never printed")
    a = ap.parse_args()
    for name in harness.load_keys(a.env_file or harness.DEFAULT_ENV_FILES):
        print(f"key     {name}")

    if a.target == "original" and not a.record:
        print("--target original is for recording goldens; comparing is against us",
              file=sys.stderr)
        return 2
    if a.base_url is not None and not a.base_url.strip():
        print("--base-url was given but empty; refusing to fall back to a local "
              "server.", file=sys.stderr)
        return 2
    dialects = ("anthropic", "openai") if a.dialect == "both" else (a.dialect,)

    def work(base: str | None) -> int:
        rc = 0
        for dialect in dialects:
            original = a.target == "original"
            if original:
                key = a.key or os.environ.get(
                    "ANTHROPIC_API_KEY" if dialect == "anthropic" else "OPENAI_API_KEY", "")
                if not key:
                    print(f"{dialect}: no key for the original", file=sys.stderr)
                    return 2
                host = ORIGINAL[dialect]
            else:
                key, host = a.key or harness.KEY, base
            kind = AnthropicTarget if dialect == "anthropic" else OpenAITarget
            scenarios = ANTHROPIC_SCENARIOS if dialect == "anthropic" else OPENAI_SCENARIOS

            def make(kind=kind, host=host, key=key, original=original):
                return kind(host, key, original)
            if a.record:
                source = {"target": a.target, "host": host if original else
                          ("local" if a.base_url is None else "deployment"),
                          "model": MODEL[dialect] if original or a.base_url
                          else "echo backend",
                          "anthropic_sdk": anthropic.__version__,
                          "recorded_on": datetime.date.today().isoformat()}
                rc |= record(dialect, scenarios, make, source)
            else:
                rc |= compare(dialect, scenarios, make,
                              without_model=bool(a.base_url) and not original
                              and harness.provider_key_for(MODEL[dialect]) is None,
                              local=not a.base_url)
        return rc

    if a.target == "original" or a.base_url:
        return work(a.base_url)
    with harness.serve_locally(a.port, "/anthropic/v1/agents") as base:
        return work(base)


if __name__ == "__main__":
    raise SystemExit(main())
