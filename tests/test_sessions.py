#!/usr/bin/env python3
"""
Sessions, and the projections the second dialect is built from.

The headline claim is slice one's promise coming due: the log records `turn_id` and
`item_id` although one dialect never shows them, so that the other dialect's
`/turns` and `/items` can be folded from the same events instead of needing a
second store. These checks are where that is either true or it is not.

    python3 tests/test_sessions.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agentstore as A  # noqa: E402
import events as E  # noqa: E402
import paging as P  # noqa: E402
import project  # noqa: E402
import render as Rn  # noqa: E402
import sessions as SS  # noqa: E402
import store as S  # noqa: E402

T = "t_test"


class Results:
    def __init__(self, label: str) -> None:
        self.label, self.passed, self.failures = label, 0, []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def raises(self, name: str, exc: type, fn, contains: str = "") -> None:
        try:
            fn()
        except exc as e:
            self.check(name, contains in str(e), f"message was {str(e)!r}")
            return
        except Exception as e:  # noqa: BLE001
            self.check(name, False, f"raised {type(e).__name__}: {e}")
            return
        self.check(name, False, "nothing was raised")

    def report(self) -> int:
        print(f"\n{self.label}: {self.passed}/{self.passed + len(self.failures)}"
              " checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


def fresh():
    agents = A.MemoryAgentStore()
    svc = SS.Sessions(S.MemoryStore(), agents)
    agent = agents.create(T, model="claude-opus-5", instructions="be brief")
    return svc, agents, agent["agent_id"]


def text(t):
    return {"type": "user.message", "content": [{"type": "text", "text": t}]}


def creation_checks() -> int:
    res = Results("create")
    svc, agents, aid = fresh()

    rec, committed = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})
    res.check("a new session is idle", rec["status"] == E.TURN_OVER, rec["status"])
    res.check("it pins the agent's version",
              rec["agent_version"] == 1 and rec["agent_id"] == aid, f"{rec}")
    res.check("nothing is committed without initial events", committed == [])

    res.raises("environment_id is required", SS.Invalid,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": aid}), "environment_id")
    res.raises("an unknown field is refused by name", SS.Invalid,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e",
                                                     "bogus": 1}), "bogus")
    res.raises("a known but unsupported field says so", SS.Invalid,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e",
                                                     "resources": [{"type": "file"}]}),
               "not supported")
    res.raises("vault_ids with no vaults behind the service is refused by name", SS.Invalid,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e",
                                                     "vault_ids": ["v"]}),
               "vault_ids")
    res.raises("overrides are refused, not ignored", SS.Invalid,
               lambda: svc.create(T, SS.ANTHROPIC, {
                   "agent": {"type": "agent_with_overrides", "id": aid},
                   "environment_id": "e"}), "not supported")
    res.raises("a missing agent is not found", SS.NotFound,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": "agent_nope",
                                                     "environment_id": "e"}))

    agents.update(T, aid, instructions="v2")
    pinned, _ = svc.create(T, SS.ANTHROPIC, {"agent": {"type": "agent", "id": aid,
                                                        "version": 1},
                                              "environment_id": "e"})
    res.check("a pinned version stays pinned", pinned["agent_version"] == 1,
              f"{pinned['agent_version']}")
    latest, _ = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})
    res.check("a bare id takes the latest", latest["agent_version"] == 2,
              f"{latest['agent_version']}")

    agents.archive(T, aid)
    res.raises("an archived agent cannot start a session", SS.Conflict,
               lambda: svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"}),
               "archived")

    # ── the OpenAI door ────────────────────────────────────────────────────
    svc, agents, aid = fresh()
    res.raises("only environment type none is honoured", SS.Invalid,
               lambda: svc.create(T, SS.OPENAI, {"agent_id": aid,
                                                  "environment": {"type": "openai_hosted"},
                                                  "input": "hi"}), "none")
    res.raises("input is required without an environment", SS.Invalid,
               lambda: svc.create(T, SS.OPENAI, {"agent_id": aid,
                                                  "environment": {"type": "none"}}),
               "input")
    res.raises("a streamed create is refused with the reason", SS.Invalid,
               lambda: svc.create(T, SS.OPENAI, {"agent_id": aid, "stream": True,
                                                  "environment": {"type": "none"},
                                                  "input": "hi"}), "API Gateway")
    res.raises("agent_id with an inline agent is refused", SS.Invalid,
               lambda: svc.create(T, SS.OPENAI, {"agent_id": aid,
                                                  "agent": {"model": "m"},
                                                  "environment": {"type": "none"},
                                                  "input": "hi"}), "not supported")

    rec, committed = svc.create(T, SS.OPENAI, {"agent": {"model": "gpt-x",
                                                         "instructions": "inline"},
                                               "environment": {"type": "none"},
                                               "input": "start"})
    res.check("an inline agent is stored anyway",
              agents.get(T, rec["agent_id"])["instructions"] == "inline",
              f"{rec}")
    res.check("the required input opens a queued turn",
              rec["status"] == E.QUEUED and len(committed) == 1
              and committed[0]["processed_at"] is None, f"{rec['status']} {committed}")
    return res.report()


def sending_checks() -> int:
    res = Results("send")
    svc, _, aid = fresh()
    sid = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})[0]["session"]

    first = svc.send(T, sid, SS.ANTHROPIC, [text("one")])
    second = svc.send(T, sid, SS.ANTHROPIC, [text("two")])
    res.check("a message is accepted, unprocessed", first[0]["processed_at"] is None,
              f"{first}")
    res.check("each message is its own turn, as the original answers each",
              first[0]["turn_id"] != second[0]["turn_id"], f"{first} {second}")
    log = svc.store.events(T, sid)
    res.check("running is said once, before the first message",
              [e["type"] for e in log][1:4] == [E.STATUS_RUNNING, E.USER_MESSAGE,
                                               E.USER_MESSAGE],
              f"{[e['type'] for e in log]}")
    res.check("the session is queued", svc.get(T, sid)["status"] == E.QUEUED)

    res.raises("a tool confirmation with nothing pending is refused", SS.Invalid,
               lambda: svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.tool_confirmation"}]),
               "none")
    res.raises("an image block is refused by name", SS.Invalid,
               lambda: svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.message",
                                                        "content": [{"type": "image"}]}]),
               "image")

    res.raises("a running session cannot be archived", SS.Conflict,
               lambda: svc.archive(T, sid), "interrupt")
    stopped = svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.interrupt"}])
    res.check("an interrupt is one event for the client",
              [e["type"] for e in stopped] == [E.USER_INTERRUPT], f"{stopped}")
    states = [t["status"] for t in project.turns(svc.store.events(T, sid),
                                                  session_id=sid, agent_id=None)]
    res.check("and ends every waiting turn, not only the last",
              states == ["cancelled", "cancelled"], f"{states}")
    res.check("and says idle, since it had said running",
              svc.store.events(T, sid)[-1]["type"] == E.STATUS_IDLE)
    res.check("and hands the floor back", svc.get(T, sid)["status"] == E.TURN_OVER)
    idle_stop = svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.interrupt"}])
    res.check("interrupting an idle session is recorded, as the original records it",
              [e["type"] for e in idle_stop] == [E.USER_INTERRUPT], f"{idle_stop}")
    res.check("but belongs to no turn and changes nothing",
              idle_stop[0].get("turn_id") is None
              and svc.get(T, sid)["status"] == E.TURN_OVER, f"{idle_stop}")

    third = svc.send(T, sid, SS.ANTHROPIC, [text("three")])
    res.check("after a cancelled turn, a new message opens a new turn",
              third[0]["turn_id"] != first[0]["turn_id"])

    svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.interrupt"}])
    res.check("archive works once idle", svc.archive(T, sid)["status"] == E.ARCHIVED)
    res.raises("an archived session accepts nothing", SS.Conflict,
               lambda: svc.send(T, sid, SS.ANTHROPIC, [text("four")]), "archived")

    # The OpenAI spellings reach the same internal events.
    osid = svc.create(T, SS.OPENAI, {"agent_id": aid, "environment": {"type": "none"},
                                     "input": "a"})[0]["session"]
    more = svc.send(T, osid, SS.OPENAI, [{"type": "agent.session.input.message",
                                          "input": [{"role": "user", "content": [
                                              {"type": "input_text", "text": "b"}]}]}])
    res.check("OpenAI input becomes the same internal event",
              more[0]["type"] == E.USER_MESSAGE and more[0]["text"] == "b", f"{more}")
    svc.send(T, osid, SS.OPENAI, [{"type": "agent.session.input.cancel"}])
    res.check("cancel is an interrupt", svc.get(T, osid)["status"] == E.TURN_OVER)
    return res.report()


def projection_checks() -> int:
    """The claim: /turns and /items fold from the log and need nothing else."""
    res = Results("projections")

    def ev(kind, turn, seq, **kw):
        return dict(E.event(kind, turn_id=turn, **kw), seq=seq)

    call = {"id": "toolu_1", "name": "search", "arguments": {"q": "x"}}
    log = [
        dict(E.event(E.SESSION_CREATED), seq=0),
        ev(E.USER_MESSAGE, "turn_a", 1, text="hi", processed=False),
        ev(E.MODEL_REQUEST_START, "turn_a", 2),
        ev(E.AGENT_TOOL_CALL, "turn_a", 3, calls=[call]),
        ev(E.TOOL_RESULT, "turn_a", 4, call_id="toolu_1", result="found"),
        ev(E.AGENT_MESSAGE, "turn_a", 5, text="done"),
        ev(E.USER_MESSAGE, "turn_b", 6, text="next", processed=False),
        ev(E.APPROVAL_REQUESTED, "turn_b", 7, call_id="toolu_2"),
        ev(E.USER_MESSAGE, "turn_c", 8, text="later", processed=False),
        ev(E.STEP_FAILED, "turn_c", 9, reason="boom"),
    ]
    turns = project.turns(log, session_id="s", agent_id="a")
    res.check("one record per turn, in the order they began",
              [t["turn_id"] for t in turns] == ["turn_a", "turn_b", "turn_c"],
              f"{[t['turn_id'] for t in turns]}")
    res.check("an answered turn is completed", turns[0]["status"] == "completed",
              turns[0]["status"])
    res.check("it records when work started and ended",
              turns[0]["started_at"] is not None and turns[0]["completed_at"] is not None)
    res.check("a turn waiting on approval is waiting", turns[1]["status"] == "waiting",
              turns[1]["status"])
    res.check("a failed step fails its turn, with the reason",
              turns[2]["status"] == "failed" and turns[2]["error"] == "boom",
              f"{turns[2]}")

    items = project.items(log)
    res.check("items are messages, calls and results only",
              [i["type"] for i in items] == ["message", "function_call",
                                             "function_call_output", "message",
                                             "message", "message"],
              f"{[i['type'] for i in items]}")
    res.check("items carry their turn", all(i["turn_id"] for i in items))
    res.check("item ids are stable across reads",
              [i["id"] for i in project.items(log)] == [i["id"] for i in items])
    res.check("a call's arguments are a JSON string, as the dialect sends them",
              items[1]["arguments"] == '{"q": "x"}', items[1]["arguments"])

    res.check("a queued turn can be joined",
              project.open_turn(log[:2]) == "turn_a", f"{project.open_turn(log[:2])}")
    res.check("a started turn cannot be joined", project.open_turn(log[:3]) is None)
    res.check("but a started turn can be interrupted",
              project.current_turn(log[:3]) == "turn_a")
    return res.report()


def render_checks() -> int:
    res = Results("render sessions")
    base = {"session": "sess_1", "agent_id": "a", "created_at": 1_700_000_000_000,
            "updated_at": 1_700_000_000_000, "environment": {"id": "e", "type": "none"}}

    expected = {
        E.QUEUED: ("running", "in_progress"),
        E.WORKING: ("running", "in_progress"),
        E.OWES_ANSWER: ("idle", "requires_action"),
        E.TURN_OVER: ("idle", "idle"),
        E.FAILED: ("terminated", "failed"),
        E.ARCHIVED: ("terminated", "idle"),
    }
    for state, (a_want, o_want) in expected.items():
        a = Rn.session({**base, "status": state}, Rn.ANTHROPIC, None)["status"]
        o = Rn.session({**base, "status": state}, Rn.OPENAI, None)["status"]
        res.check(f"{state} projects to {a_want} / {o_want}",
                  (a, o) == (a_want, o_want), f"got {a} / {o}")

    res.check("an Anthropic session has type, not object",
              Rn.session({**base, "status": E.TURN_OVER}, Rn.ANTHROPIC, None)["type"]
              == "session")
    res.check("an OpenAI session has object agent.session",
              Rn.session({**base, "status": E.TURN_OVER}, Rn.OPENAI, None)["object"]
              == "agent.session")

    created = dict(E.event(E.SESSION_CREATED), seq=0)
    res.check("session.created is not an Anthropic event",
              Rn.event(created, Rn.ANTHROPIC, session_id="s") == [])
    msg = dict(E.event(E.USER_MESSAGE, turn_id="t", text="hi", processed=False), seq=1)
    a = Rn.event(msg, Rn.ANTHROPIC, session_id="s")[0]
    res.check("an unprocessed message has processed_at null",
              a["processed_at"] is None, f"{a}")
    o = Rn.event(msg, Rn.OPENAI, session_id="s")[0]
    res.check("OpenAI renders a message as a turn item",
              o["type"] == "agent.session.turn.item.added"
              and o["item"]["content"][0]["type"] == "input_text", f"{o}")
    res.check("OpenAI events carry event_id, not id",
              "event_id" in o and "id" not in o, f"{sorted(o)}")

    reply = Rn.message_item({"id": "i", "role": "assistant", "text": "x"})
    res.check("what the agent wrote is output_text",
              reply["content"][0]["type"] == "output_text")
    return res.report()


def paging_checks() -> int:
    res = Results("paging")
    items = [{"k": f"{i:04d}"} for i in range(23)]
    key = lambda i: i["k"]  # noqa: E731

    for order in ("asc", "desc"):
        seen, page = [], None
        while True:
            r = P.anthropic(items, key=key, limit=5, order=order, page=page)
            seen += [i["k"] for i in r["data"]]
            if not r["next_page"]:
                break
            page = r["next_page"]
        res.check(f"an {order} walk sees every item exactly once",
                  sorted(seen) == [i["k"] for i in items] and len(seen) == 23,
                  f"{len(seen)} items")

    r = P.anthropic(items, key=key, limit=5)
    r2 = P.anthropic(items, key=key, limit=5, page=r["next_page"])
    back = P.anthropic(items, key=key, limit=5, page=r2["prev_page"])
    res.check("prev_page returns to the previous page",
              [i["k"] for i in back["data"]] == [i["k"] for i in r["data"]])
    res.raises("a cursor from another order is refused", P.BadCursor,
               lambda: P.anthropic(items, key=key, limit=5, order="desc",
                                   page=r["next_page"]), "order")
    res.raises("a forged cursor is refused", P.BadCursor,
               lambda: P.anthropic(items, key=key, limit=5, page="not-a-cursor"))

    o = P.openai(items, key=key, id_of=key, limit=5, order="asc", after="0004")
    res.check("after continues past the named item",
              o["first_id"] == "0005" and o["has_more"], f"{o['first_id']}")
    res.raises("after an unknown id is refused", P.BadCursor,
               lambda: P.openai(items, key=key, id_of=key, limit=5, after="nope"))
    return res.report()


def update_checks() -> int:
    res = Results("update")
    svc, _, aid = fresh()
    sid = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e",
                                       "metadata": {"a": "1", "b": "2"}})[0]["session"]

    got = svc.update(T, sid, SS.ANTHROPIC, {"metadata": {"b": None, "c": "3"},
                                            "title": "t"})
    res.check("Anthropic metadata is a patch: null deletes, a string upserts",
              got["metadata"] == {"a": "1", "c": "3"}, f"{got['metadata']}")
    res.check("and the title is set", got["title"] == "t")
    got = svc.update(T, sid, SS.OPENAI, {"metadata": {"z": "9"}})
    res.check("OpenAI metadata replaces the whole map",
              got["metadata"] == {"z": "9"}, f"{got['metadata']}")
    got = svc.update(T, sid, SS.OPENAI, {"metadata": None})
    res.check("and null clears it", got["metadata"] == {}, f"{got['metadata']}")
    got = svc.update(T, sid, SS.ANTHROPIC, {})
    res.check("an empty update changes nothing",
              got["metadata"] == {} and got["title"] == "t")

    got = svc.update(T, sid, SS.OPENAI, {"agent": {"model": "claude-sonnet-5"}})
    res.check("an agent setting is kept on the session",
              got["agent_overrides"] == {"model": "claude-sonnet-5"}, f"{got}")
    res.check("not on the agent, which other sessions share",
              svc.agents.get(T, aid)["model"] == "claude-opus-5")
    got = svc.update(T, sid, SS.ANTHROPIC, {"agent": {"tools": []}})
    res.check("overrides accumulate across updates",
              got["agent_overrides"] == {"model": "claude-sonnet-5", "tools": []},
              f"{got['agent_overrides']}")

    for name, dialect, body, says in (
            ("vaults", SS.ANTHROPIC, {"vault_ids": ["vlt_1"]}, "vault_ids"),
            ("a reasoning effort", SS.OPENAI, {"agent": {"reasoning": {"effort": "high"}}},
             "effort"),
            ("a service tier", SS.OPENAI, {"agent": {"service_tier": "flex"}}, "tier"),
            ("the other dialect's agent field", SS.ANTHROPIC,
             {"agent": {"model": "x"}}, "model"),
            ("a field neither has", SS.OPENAI, {"title": "t"}, "title")):
        res.raises(f"{name} is refused by name", SS.Invalid,
                   lambda: svc.update(T, sid, dialect, body), says)
    res.raises("seventeen metadata keys are refused", SS.Invalid,
               lambda: svc.update(T, sid, SS.OPENAI,
                                  {"metadata": {str(i): "v" for i in range(17)}}), "16")

    # Two patches at once: the second reads the metadata after the first, because
    # the first took the next sequence and the second re-reads.
    commit, raced = svc.store.commit, []

    def other_first(tenant, session, events, **kw):
        if not raced and any(e["type"] == E.SESSION_UPDATED for e in events):
            raced.append(True)
            svc.update(T, sid, SS.ANTHROPIC, {"metadata": {"first": "1"}})
        return commit(tenant, session, events, **kw)
    svc.store.commit = other_first
    got = svc.update(T, sid, SS.ANTHROPIC, {"metadata": {"second": "2"}})
    svc.store.commit = commit
    res.check("two patches at once both land",
              got["metadata"] == {"first": "1", "second": "2"}, f"{got['metadata']}")

    limit = {"type": "limit", "max_list_cost": {"amount": "2500", "currency": "USD"}}
    got = svc.update(T, sid, SS.ANTHROPIC, {"budget": limit})
    res.check("a budget is set by an update", got.get("budget") == limit, f"{got}")
    got = svc.update(T, sid, SS.ANTHROPIC, {"budget": None})
    res.check("and removed by null", got.get("budget") is None, f"{got.get('budget')}")
    for name, bad, says in (
            ("a currency other than USD", {"amount": "1", "currency": "EUR"}, "USD"),
            ("a decimal amount", {"amount": "25.00", "currency": "USD"}, "minor units"),
            ("a leading zero", {"amount": "007", "currency": "USD"}, "leading zeros"),
            ("a number, not a string", {"amount": 2500, "currency": "USD"}, "string")):
        res.raises(f"a budget with {name} is refused", SS.Invalid,
                   lambda bad=bad: svc.update(T, sid, SS.ANTHROPIC, {"budget": {
                       "type": "limit", "max_list_cost": bad}}), says)
    res.raises("OpenAI has no budget to set", SS.Invalid,
               lambda: svc.update(T, sid, SS.OPENAI, {"budget": limit}), "budget")
    made = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e",
                                        "budget": limit})[0]
    res.check("a budget is set at creation", made.get("budget") == limit, f"{made}")

    svc.archive(T, sid)
    res.raises("an archived session is not updated", SS.Conflict,
               lambda: svc.update(T, sid, SS.ANTHROPIC, {"title": "late"}), "archived")
    return res.report()


def deletion_checks() -> int:
    res = Results("delete")
    svc, _, aid = fresh()
    woken = []
    svc.wake = lambda t, s: woken.append(s)
    sid = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})[0]["session"]
    keep = svc.create(T, SS.ANTHROPIC, {"agent": aid, "environment_id": "e"})[0]["session"]

    svc.send(T, sid, SS.ANTHROPIC, [text("one")])
    res.raises("a session with a turn under way is not deleted", SS.Conflict,
               lambda: svc.delete(T, sid), "cancel")
    res.check("and it is still there", svc.get(T, sid)["status"] == E.QUEUED)

    svc.send(T, sid, SS.ANTHROPIC, [{"type": "user.interrupt"}])
    woken.clear()
    svc.delete(T, sid)
    res.raises("once the turn has ended, it is gone from the API", SS.NotFound,
               lambda: svc.get(T, sid))
    res.check("and from the list, but not the others with it",
              [r["session"] for r in svc.list(T)] == [keep],
              f"{[r['session'] for r in svc.list(T)]}")
    res.raises("a message to it is refused as not found", SS.NotFound,
               lambda: svc.send(T, sid, SS.ANTHROPIC, [text("hello?")]))
    res.raises("deleting it again is not found", SS.NotFound, lambda: svc.delete(T, sid))
    res.check("the worker is woken to purge it", woken == [sid], f"{woken}")
    tail = svc.store.events(T, sid)[-1]
    res.check("the deletion is the last event in its log",
              tail["type"] == E.SESSION_DELETED, f"{tail['type']}")

    # The race: a message and the deletion read the same log, and the message
    # takes the next sequence first. The deletion must re-read and refuse, not
    # delete a session that now has work queued in it.
    svc2, _, aid2 = fresh()
    sid2 = svc2.create(T, SS.ANTHROPIC, {"agent": aid2, "environment_id": "e"})[0]["session"]
    commit = svc2.store.commit
    raced = []

    def message_first(tenant, session, events, **kw):
        if not raced and any(e["type"] == E.SESSION_DELETED for e in events):
            raced.append(True)
            svc2.send(T, sid2, SS.ANTHROPIC, [text("just in time")])
        return commit(tenant, session, events, **kw)
    svc2.store.commit = message_first
    res.raises("a message that wins the race stops the deletion", SS.Conflict,
               lambda: svc2.delete(T, sid2), "cancel")
    res.check("and the session keeps its message",
              svc2.get(T, sid2)["status"] == E.QUEUED)
    return res.report()


def main() -> int:
    rc = 0
    for check in (creation_checks, sending_checks, projection_checks, render_checks,
                  paging_checks, update_checks, deletion_checks):
        rc |= check()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
