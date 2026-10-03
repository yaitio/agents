#!/usr/bin/env python3
"""
Layer three's instrument, tested — free, so it runs in CI while the eval does not.

A quality run costs money and its verdict decides a model switch, so the parts
that are not the models have to be right before the first dollar: the scorers
against known answers, the decision against records whose answer is known, and
the plumbing through our API against a local server on the echo backend.

    python3 tests/test_quality.py
"""

from __future__ import annotations

import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tests"))

import conformance_harness as harness  # noqa: E402
import quality as Q  # noqa: E402
from yait_aichain.eval._records import Record  # noqa: E402


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


CASES = Q.load_cases(ROOT / "evals" / "cases" / "starter.jsonl")


def instrument() -> int:
    res = Results("the instrument")
    checked = Q.controls(CASES)
    res.check("every oracle answer passes its scorer", checked["oracle"] == 1.0,
              f"{checked['oracle_misses']}")
    res.check("no wrong answer passes", checked["noise"] == 0.0,
              f"{checked['noise_passes']}")
    res.check("every oracle answer passes its gates", not checked["gates_on_oracle"],
              f"{checked['gates_on_oracle']}")

    by_id = {c.id: c for c in CASES}
    res.check("a worked answer is scored by its result, wherever it stands",
              Q.score(by_id["math-multiply"], "17 × 23 = 391")[0]
              and Q.score(by_id["math-multiply"], "391, since 17 × 23 = 391 and 17 < 23")[0]
              and not Q.score(by_id["math-multiply"], "17 × 23 = 381")[0])
    res.check("no number case can be passed by echoing its question",
              checked["answer_in_question"] == [], f"{checked['answer_in_question']}")

    agents = Q.load_cases(ROOT / "evals" / "cases" / "agents.jsonl")
    full = Q.controls(agents)
    res.check("the agents' task set passes its controls on every oracle phrasing",
              full["ok"], f"{full}")

    from yait_aichain.eval._records import Record as R
    fixed = Q.rescore([R(arm="a", case="math-multiply", trial=1, ok=False,
                         output="The product is 391.",
                         meta={"stop_reason": "end_turn"})], CASES)
    res.check("a ledger is scored again without calling a model",
              fixed[0].ok and fixed[0].meta["gates"] == [], f"{fixed[0]}")
    res.check("a capital letter breaks the lowercase gate",
              Q.gates(by_id["obey-lowercase"], "Jupiter", "end_turn") != [])
    res.check("prose around JSON breaks the JSON-only gate",
              Q.gates(by_id["obey-json"], 'Here: {"city": "Paris"}', "end_turn") != [])
    res.check("an empty answer breaks the answered gate",
              Q.gates(by_id["fact-canberra"], "  ", "end_turn") != [])
    res.check("so does a turn that did not end normally",
              Q.gates(by_id["fact-canberra"], "Canberra", "budget_reached") != [])
    return res.report()


def records(arm: str, passes: dict, *, trials: int = 3, cost: float = 0.01,
            broken: dict | None = None, priced: bool = True) -> list[Record]:
    """`passes[case]` of `trials` attempts pass."""
    out = []
    for case, wins in passes.items():
        for t in range(trials):
            out.append(Record(arm=arm, case=case, trial=t, ok=t < wins,
                              output="x", cost=cost,
                              meta={"priced": priced,
                                    "gates": (broken or {}).get((case, t), [])}))
    return out


def decision() -> int:
    res = Results("the decision")
    twenty = {f"c{i}": 3 for i in range(20)}

    same = Q.decide(records("a", twenty) + records("b", twenty), "a", "b")
    res.check("an identical candidate is non-inferior",
              same["verdict"].startswith("NON-INFERIOR"), same["verdict"])

    worse = {f"c{i}": (0 if i < 8 else 3) for i in range(20)}
    down = Q.decide(records("a", twenty) + records("b", worse), "a", "b")
    res.check("one that fails 8 of 20 cases the baseline passes is not",
              down["verdict"].startswith("NOT SHOWN"), down["verdict"])
    res.check("and the paired test sees it",
              down["paired_test"]["only_b"] == 8 and down["paired_test"]["p"] < 0.05,
              f"{down['paired_test']}")

    gate = Q.decide(records("a", twenty) + records("b", twenty,
                    broken={("c3", 1): ["must not match '[A-Z]'"]}), "a", "b")
    res.check("one broken invariant stops it, whatever the averages",
              gate["verdict"].startswith("STOP"), gate["verdict"])

    few = {f"c{i}": 3 for i in range(4)}
    small = Q.decide(records("a", few) + records("b", few), "a", "b")
    res.check("four cases are too few to conclude anything",
              small["verdict"].startswith("INCONCLUSIVE"), small["verdict"])

    cheap = Q.decide(records("a", twenty, cost=0.05)
                     + records("b", twenty, cost=0.01), "a", "b")
    res.check("cost per success is spend over successes",
              math.isclose(cheap["cost_per_success"]["b"], 0.01)
              and math.isclose(cheap["cost_per_success"]["a"], 0.05),
              f"{cheap['cost_per_success']}")
    free = Q.decide(records("a", twenty) + records("b", twenty, cost=0, priced=False),
                    "a", "b")
    res.check("an unpriced model is unpriced, never free",
              free["cost_per_success"]["b"] is None and "b" in free["unpriced"],
              f"{free['cost_per_success']}")
    res.check("the report says so in words",
              "unpriced" in Q.describe(free), Q.describe(free))
    return res.report()


def plumbing() -> int:
    """Through our API, on echo: every attempt made, read, gated and cleaned up."""
    res = Results("through our API")
    with harness.serve_locally(8135, "/anthropic/v1/agents") as base:
        api = Q.OurApi(base, harness.KEY)
        try:
            picked = [c for c in CASES if c.id in ("fact-canberra", "obey-number-only")]
            out = [api.attempt("echo-model", c) for c in picked]
            listed = api.c.beta.sessions.list().data
        finally:
            api.close()
    res.check("each attempt reads the agent's answer",
              all(o["output"].startswith("echo: ") for o in out), f"{out}")
    res.check("and how its turn ended", all(o["stop_reason"] == "end_turn" for o in out),
              f"{[o['stop_reason'] for o in out]}")
    res.check("an answer that obeys is not flagged", out[0]["gates"] == [],
              f"{out[0]}")
    res.check("one that breaks the case's instruction is",
              any("must match" in g for g in out[1]["gates"]), f"{out[1]}")
    res.check("a model the price table does not know is unpriced",
              all(o["priced"] is False for o in out), f"{out}")
    res.check("and no session is left behind", listed == [], f"{len(listed)} left")
    return res.report()


def pricing() -> int:
    """The price table, and pricing from it: what a report's money column rests on."""
    import datetime
    import results as R
    res = Results("prices")
    table = R.load_prices()
    bad = [f"{m} {p}" for m, ps in table.items() for p in ps
           if not {"from", "input", "output", "source"} <= set(p)]
    res.check("every price says what it is, from when, and where it was read",
              not bad, f"{bad[:3]}")

    day = datetime.date.fromisoformat
    res.check("a price in force is the one used",
              R.period(table, "gemini-3.8-flash", day("2026-10-15"))["input"] == 0.75)
    res.check("and the next one once it starts",
              R.period(table, "gemini-3.8-flash", day("2027-02-01"))["input"] == 1.50)
    early = R.period(table, "gemini-3.8-flash", day("2026-09-01"))
    res.check("a run before the table began is priced at its earliest, and says so",
              early["input"] == 0.75 and "earliest" in early["note"], f"{early}")

    haiku = R.period(table, "claude-haiku-4-5-20251001", day("2026-10-01"))
    usd, notes = R.cost({"input_tokens": 1_000_000, "output_tokens": 100_000,
                         "cache_read_tokens": 1_000_000}, haiku)
    res.check("fresh, cached and output tokens each at their own price",
              abs(usd - (1.00 + 0.50 + 0.10)) < 1e-9 and not notes, f"{usd} {notes}")
    luna = R.period(table, "gpt-6-luna", day("2026-10-01"))
    usd, notes = R.cost({"input_tokens": 0, "output_tokens": 0,
                         "cache_read_tokens": 1_000_000}, luna)
    res.check("a cache price nobody published is priced as input, and said to be",
              abs(usd - 0.10) < 1e-9 and notes, f"{usd} {notes}")

    rows, why = R.reprice(records("ours:no-such-model", {"c0": 1}, cost=0.5),
                          lambda arm: arm.split(":", 1)[1], day("2026-10-01"))
    res.check("a model with no price is unpriced, not free and not its old cost",
              all(r.cost == 0.0 and r.meta["priced"] is False for r in rows)
              and "no-such-model" in why, f"{why}")
    return res.report()


def pacing() -> int:
    """An account's limits are waited out, not scored; and they hold."""
    import threading
    import time as clock
    res = Results("pacing")
    Q.RATE_LIMIT_WAITS = (0.01, 0.01, 0.01, 0.01, 0.01)
    calls = {"n": 0}

    def flaky(case):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("model_rate_limited_error: [HTTP 429] slow down")
        return {"output": "ok"}
    out = Q.paced(flaky, None)(CASES[0])
    res.check("a 429 is tried again, and the tries are counted",
              out["output"] == "ok" and out["rate_limit_retries"] == 2, f"{out}")

    def broken(case):
        raise RuntimeError("billing_error: top up")
    try:
        Q.paced(broken, None)(CASES[0])
        res.check("any other failure is not retried", False, "it was swallowed")
    except RuntimeError:
        res.check("any other failure is not retried", True)

    busy, peak, lock = {"now": 0}, {"max": 0}, threading.Lock()

    def slow(case):
        with lock:
            busy["now"] += 1
            peak["max"] = max(peak["max"], busy["now"])
        clock.sleep(0.02)
        with lock:
            busy["now"] -= 1
        return {"output": "ok"}
    one = Q.paced(slow, 1)
    threads = [threading.Thread(target=one, args=(CASES[0],)) for _ in range(6)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    res.check("a limit of one means one at a time", peak["max"] == 1, f"{peak}")
    return res.report()


def main() -> int:
    rc = 0
    for check in (instrument, decision, plumbing, pricing, pacing):
        rc |= check()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
