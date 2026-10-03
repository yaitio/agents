#!/usr/bin/env python3
"""
The store, one suite against both implementations.

`MemoryStore` always; `DynamoStore` when table names are given. The same checks
either way — a fake with different semantics tests nothing, and the semantics under
test are exactly the ones that make a queue unnecessary: a conditional append that
refuses a taken sequence, and a lease that expires.

    python3 tests/test_store.py
    python3 tests/test_store.py --dynamo --region us-west-1 \
        --sessions-table yait_agents_sessions --events-table yait_agents_events
"""

from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import events as E  # noqa: E402
from fold import fold, unanswered  # noqa: E402
from store import Conflict, DynamoStore, MemoryStore  # noqa: E402

TENANT = "t_test"


class Results:
    def __init__(self, label: str) -> None:
        self.label = label
        self.passed = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print(f"\n{self.label}: {self.passed}/{total} checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


def run(store, label: str) -> int:
    res = Results(label)
    sid = f"s_{uuid.uuid4().hex[:12]}"

    # ── creation ───────────────────────────────────────────────────────────

    created = store.create_session(TENANT, sid, agent_id="ag_1", model="m")
    res.check("create returns the item", created.get("session") == sid, f"{created}")
    res.check("a new session starts at cursor 0", created.get("cursor") == 0,
              f"{created.get('cursor')}")

    got = store.get_session(TENANT, sid)
    res.check("the session reads back", got and got["session"] == sid, f"{got}")
    res.check("the tenant is a prefix of the key", got["pk"].startswith(f"T#{TENANT}#"),
              f"{got['pk']}")

    try:
        store.create_session(TENANT, sid)
        res.check("creating it twice is a conflict", False, "it succeeded")
    except Conflict:
        res.check("creating it twice is a conflict", True)

    log = store.events(TENANT, sid)
    res.check("sequence 0 is session.created",
              len(log) == 1 and log[0]["type"] == E.SESSION_CREATED
              and log[0]["seq"] == 0, f"{log}")

    # ── the conditional append ─────────────────────────────────────────────

    store.append(TENANT, sid, 1, E.event(E.USER_MESSAGE, text="one"))
    try:
        store.append(TENANT, sid, 1, E.event(E.USER_MESSAGE, text="two"))
        res.check("a taken sequence is refused", False, "the second write won")
    except Conflict:
        res.check("a taken sequence is refused", True)

    log = store.events(TENANT, sid)
    res.check("the loser did not overwrite the winner",
              [e["seq"] for e in log] == [0, 1]
              and log[1].get("text") == "one", f"{log}")

    # The interface promises `seq` and no storage key. Both stores derive it, and
    # this is the check that keeps them from drifting: the first thing that differed
    # was DynamoDB calling it `sk` and leaving `pk` on every item.
    sample = store.events(TENANT, sid)[0]
    res.check("an event carries seq", "seq" in sample, f"{sorted(sample)}")
    res.check("an event carries no storage keys",
              not ({"pk", "sk"} & set(sample)), f"{sorted(sample)}")

    # Nested numbers must come back as plain numbers. DynamoDB returns Decimal at
    # every depth, and a Decimal inside `usage` once turned a whole endpoint into a
    # 502 — json.dumps refuses it. The in-memory store never makes one, so only this
    # check, run against DynamoDB, can see it.
    import json as _json
    nested_seq = store.append_next(TENANT, sid, E.event(
        E.MODEL_REQUEST_END, usage={"input_tokens": 3, "output_tokens": 5},
        calls=[{"id": "c", "n": 2}]))
    nested = [e for e in store.events(TENANT, sid) if e["seq"] == nested_seq][0]
    try:
        _json.dumps(nested)
        serialisable = True
    except TypeError:
        serialisable = False
    res.check("nested numbers come back JSON-serialisable", serialisable,
              f"{nested.get('usage')}")
    # A float must survive the write. DynamoDB's resource API refuses one outright;
    # the echo backend never made one, a real model's cost did.
    float_seq = store.append_next(TENANT, sid, E.event(
        E.MODEL_REQUEST_END, usage={"cost_usd": 0.0031}))
    back = [e for e in store.events(TENANT, sid) if e["seq"] == float_seq][0]
    res.check("a float is written and read back as the same number",
              abs(back.get("usage", {}).get("cost_usd", 0) - 0.0031) < 1e-12,
              f"{back.get('usage')}")
    res.check("and as the numbers they were",
              nested.get("usage") == {"input_tokens": 3, "output_tokens": 5}
              and nested.get("calls") == [{"id": "c", "n": 2}], f"{nested}")

    seq = store.append_next(TENANT, sid, E.event(E.AGENT_MESSAGE, text="reply"))
    res.check("append_next takes the next free number", seq == float_seq + 1,
              f"{seq}")

    res.check("reading after a cursor skips what came before",
              [e["seq"] for e in store.events(TENANT, sid, after=1)]
              == [nested_seq, float_seq, seq],
              f"{[e['seq'] for e in store.events(TENANT, sid, after=1)]}")

    # This is the guarantee that replaces a queue: many writers, no lost events and
    # no duplicated sequence. Without it, ordering would have to come from SQS.
    workers = 8
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(store.append_next, TENANT, sid,
                               E.event(E.USER_MESSAGE, text=f"c{i}"))
                   for i in range(workers)]
        taken = sorted(f.result() for f in futures)
    res.check(f"{workers} concurrent appends take {workers} distinct numbers",
              len(set(taken)) == workers, f"{taken}")
    log = store.events(TENANT, sid)
    res.check("the log has no gaps and no repeats",
              [e["seq"] for e in log] == list(range(len(log))),
              f"{[e['seq'] for e in log]}")

    # ── the lease ──────────────────────────────────────────────────────────

    res.check("a free lease can be taken", store.take_lease(TENANT, sid, "a", 60))
    res.check("another holder is refused",
              not store.take_lease(TENANT, sid, "b", 60), "b took it from a")
    res.check("the holder may extend its own",
              store.take_lease(TENANT, sid, "a", 60), "a could not extend")
    store.release_lease(TENANT, sid, "a")
    res.check("after release another may take it",
              store.take_lease(TENANT, sid, "b", 1), "b still refused")

    # A lease expires because the holder can die; a lock would need releasing and
    # nobody would be left to release it.
    time.sleep(1.2)
    res.check("an expired lease is available again",
              store.take_lease(TENANT, sid, "c", 60), "it never expired")
    store.release_lease(TENANT, sid, "c")

    # ── the commit ─────────────────────────────────────────────────────────

    before = store.get_session(TENANT, sid)["cursor"]
    cursor = store.commit(
        TENANT, sid,
        [E.event(E.AGENT_TOOL_CALL, calls=[{"id": "toolu_9", "name": "n",
                                            "arguments": {}}]),
         E.event(E.TOOL_RESULT, call_id="toolu_9", result="ok")],
        cursor=max(e["seq"] for e in store.events(TENANT, sid)),
        status=E.BETWEEN_STEPS, agent_state={"steps": 3})
    item = store.get_session(TENANT, sid)
    res.check("the commit moves the cursor to the last event written",
              item["cursor"] == cursor and cursor > before, f"{item['cursor']} {cursor}")
    res.check("the commit writes the session fields too",
              item["status"] == E.BETWEEN_STEPS
              and item["agent_state"] == {"steps": 3}, f"{item}")
    res.check("the events and the cursor agree",
              max(e["seq"] for e in store.events(TENANT, sid)) == item["cursor"],
              "a cursor past the log means a step would be skipped")

    # ── the store and the fold together ────────────────────────────────────

    messages = fold(store.events(TENANT, sid), instructions="be brief")
    res.check("a log straight from the store folds without a dangling call",
              unanswered(messages) == [], f"{unanswered(messages)}")

    # ── purging ────────────────────────────────────────────────────────────

    other = f"s_{uuid.uuid4().hex[:12]}"
    store.create_session(TENANT, other)
    store.append_next(TENANT, other, E.event(E.USER_MESSAGE, text="keep me"))
    theirs = len(store.events(TENANT, other))
    res.check("a purge reports it finished", store.purge(TENANT, sid) is True)
    res.check("and leaves no event", store.events(TENANT, sid) == [],
              f"{len(store.events(TENANT, sid))} left")
    left = store.get_session(TENANT, sid) or {}
    res.check("and of the session item only a tombstone: that it was, and was deleted",
              left.get("status") == "deleted" and left.get("purged") is True
              and not ({"agent_id", "metadata", "cursor", "agent_state"} & set(left)),
              f"{left}")
    res.check("and touches no other session",
              store.get_session(TENANT, other) is not None
              and len(store.events(TENANT, other)) == theirs > 0)
    res.check("purging what is already gone is harmless",
              store.purge(TENANT, sid) is True)
    store.purge(TENANT, other)

    return res.report()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dynamo", action="store_true")
    ap.add_argument("--sessions-table")
    ap.add_argument("--events-table")
    ap.add_argument("--region")
    a = ap.parse_args()

    rc = run(MemoryStore(), "memory")

    if a.dynamo:
        if not (a.sessions_table and a.events_table):
            print("--dynamo needs --sessions-table and --events-table",
                  file=sys.stderr)
            return 2
        if a.region:
            import os
            os.environ.setdefault("AWS_DEFAULT_REGION", a.region)
        rc |= run(DynamoStore(a.sessions_table, a.events_table), "dynamodb")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
