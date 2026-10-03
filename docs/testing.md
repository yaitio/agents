# Testing — how we know it behaves like the original

The promise is that a client written against Anthropic's Managed Agents or OpenAI's
Agents API moves here by changing `base_url`, and that an agent moved from one model
to another still does its job. Both are claims, and a claim is only as good as the
test that could have refuted it. This document is how.

## The one rule: change one variable at a time

There are two questions, and mixing them makes both unanswerable.

| Question | Held fixed | Varied | A difference means |
|---|---|---|---|
| **Is our API the same as the original?** | the model | the API: original ↔ ours | a defect here |
| **Is model B as good as model A for our agents?** | the API — ours | the model | a property of the model |

Comparing "the original with Claude" against "ours with DeepSeek" changes two things
at once, and no result of it can say which one mattered.

## Three layers

### 1. Shape — the contract

**Every response parses as the original's own types.** For the Anthropic dialect the
reference is the official SDK: each response is validated against the SDK's model for
that call, so a missing required field, a wrong type or an enum value the SDK does not
know fails. For the OpenAI dialect the reference is OpenAI's published OpenAPI
document — the Agents API is in it, but not yet in their Python SDK — and each
response is validated against the schema for its operation. Many of those schemas are
`additionalProperties: false`, so an extra field fails too.

The model backend is `echo`: deterministic and free, so a failure here can only be
ours. Runs on every commit and every deploy.

| Dialect | Suite | Reference |
|---|---|---|
| Anthropic | `tests/conformance_anthropic.py` | the `anthropic` SDK, pinned in `tests/requirements.txt` |
| OpenAI | `tests/conformance_openai.py` | `tests/reference/openai-agents.json` — the `/agents` part of OpenAI's `openapi.yaml`, with its source and the hash of what it was cut from |

The OpenAI suite checks the status code as well as the body — the schema is declared
per status, so a 200 where the original answers 201 fails — and holds the scenario's
own requests to the document, so it can only send what a real client could. Paths the
echo backend never takes (a failed turn, a cancelled one) are built as logs and
rendered directly, so they are held to the same schemas.

Moving a reference is a deliberate re-baseline, and a visible diff:
`pip install` a newer SDK and change the pin, or re-cut the OpenAI document with
`python3 tests/reference/prune_openai.py`.

**What layer one does not cover.** It checks that each thing we say has the right
shape, not that we say everything the original does, in the order it does — no shape
check can notice an event that is absent or misplaced. That is layer two. Until its
goldens are recorded, the order we emit is pinned by our own test
(`tests/test_worker.py`, *OpenAI turn and status events*), which is our reading of
the document, not a recording of the original:

    session.created → turn.created → turn.item.added (input)
      → session.in_progress → turn.in_progress
      → turn.item.added, turn.item.done (answer) → turn.completed | turn.failed
      → session.idle

Not emitted yet: the streaming deltas (`output_text.delta`, `content_part.*`) — we
do not stream — and anything for tools, subagents or environments, which do not
exist here yet.

**Known gaps are listed, not hidden.** A shape we know we do not match yet is recorded
with its reason as an expected failure. The suite stays green while the gap is
tracked, and turns red if an expected failure starts passing — so a fix cannot go
unnoticed and a gap cannot be forgotten.

### 2. Behaviour — the state machine

A **scenario** is a script of client actions: one turn; two quick messages; an
interrupt mid-turn; archiving a running session; a request with an unknown field. The
observable is a **normalised trace** — event types in order, status transitions,
`stop_reason`, HTTP status codes — with ids, timestamps and reply text masked out,
because those differ legitimately.

The original's trace is **recorded once**, on the same model, and kept as a golden
file. Comparing ours with it is then free on every deploy. A golden is re-recorded
when the API version changes — `anthropic-beta: managed-agents-2026-04-01` names it.

The same scenarios run locally and against the deployment; that pair is the scenario
parity the specification calls P1.

**How it is built.** `tests/behaviour.py` holds the scenarios — eight for Anthropic,
driven by the official SDK against either host, and six for OpenAI over HTTP — and
`tests/goldens/<dialect>/<scenario>.json` the goldens, each with where it was
recorded. A golden's `source.target` decides what a mismatch means:

| Golden recorded from | A mismatch is | The report says |
|---|---|---|
| `original` | a parity defect — we differ from the API we claim to be | `DIFFERS` |
| `ours` | a change in our behaviour, not yet compared with anything | `CHANGED`, and counts it as *not yet compared with the original* |

The goldens in the repository are **recorded from the originals** (2026-09-30):
Anthropic's Managed Agents on claude-haiku-4-5-20251001, OpenAI's Agents API on
gpt-6-luna. What they showed, and what was done about it:

| Found | Done |
|---|---|
| `status_running` precedes the message that set it going | the same |
| each message gets its own answer, in order, within one `running` … `idle` | the same: one message, one turn |
| a queued message is listed when taken up, after the answer before it | the same, in the Anthropic event list |
| `session.usage` closes a stretch of work | the same |
| an interrupt with nothing running is still recorded | the same |
| a state conflict is 400 `invalid_request_error` (Anthropic) | the same |
| `error.code` repeats the type (OpenAI) | the same |
| deleting a deleted session answers 200 (OpenAI); an id never seen, 404 | the same, while the tombstone lasts |
| an answer is `item.added` → `content_part.added` → `output_text.delta`… → `output_text.done` → `content_part.done` → `item.done` (OpenAI) | the same, with the text in one delta |
| OpenAI's GET /events is a **live** stream: it replays nothing | traced as "what a send causes" on both hosts |
| `thread_status_*` (Anthropic) | **known divergences**, named in `KNOWN_DIVERGENCES` with why |
| 400 for an id not in the original's format | **known divergence**: a well-formed missing id is 404 on both |

A known divergence is as narrow as it can be named — an event kind, a step — and is
taken out of both traces before comparing, so nothing else in the same scenario
escapes. A rule that no longer takes anything out fails the run until removed.

Recording from the original is a local command, run as a client with its own
provider key — `python3 tests/behaviour.py --record --target original`. Every
scenario runs twice and is written only if both traces agree; committing the result
is a person's decision.

**Provider keys never go to GitHub.** The installation pays for no tokens, and a
release checks the infrastructure: against a deployment with no provider key, the
checks that need a model's answer report *skipped*, and one checks instead that such
a turn is refused rather than paid for. Everything that needs a model runs from a
client that brings its own key, as a user would.

**The one-variable rule, applied.** The scenarios are text-only — no tools — so a
turn's trace should not depend on which model answered it. That is what lets a
golden recorded on Claude be compared with our echo backend in CI. It is an
assumption the first recording can refute: if the original's trace varies with the
model, the recorder sees it as instability, and the comparison moves to the
deployment running the same model. Against the deployment, the model is the
agent's own — `claude-haiku-4-5-20251001` and `gpt-6-luna` by default, the same names the
recording uses.

Known divergences are listed in `KNOWN_DIVERGENCES`, with the same strict
semantics as the known gaps of layer one.

### 3. Model quality — Claude to DeepSeek

`aichain.eval` already does this: arms × cases × trials, every attempt in a durable
ledger, and a report with `mean` (accuracy), `pass^k` (reliability — a case counts
only if it passed every trial), flips and cost, with paired tests between arms.

Here the arms are the same agent with a different `model`, **run through our API**, so
it measures the product rather than the library. A model switch is accepted on
**non-inferiority** against the current one within a margin δ, together with **cost
per successful task** — the multiple lives in the model, so a slightly weaker model
at a fifth of the price can be the right call, and the report has to show both.

Alongside the statistics, **invariants any model must hold**, as gates rather than
averages: tool-call arguments valid against the tool's schema, no dangling call, the
instructions obeyed, a refusal handled. A model that is 3% less accurate is a pricing
question. A model that breaks the tool-call format is a stop.

Case sources: our own tasks, from what our agents are actually for; and τ²-bench,
agentic tasks with tools and a simulated user, which `aichain` already adapts.

**How it is built.** `evals/quality.py` runs it; `evals/cases/*.jsonl` holds the
cases, each with its scorer, its expected answer, a known-right `oracle` answer and,
where the case sets an instruction, the patterns the answer `must` and `must_not`
match. Every attempt is a fresh session through our API, on an agent with the
arm's model and the case's instructions. Its cost is measured from the tokens the
session reports per request, priced by the same table the server's budget uses — a
model that table does not know is reported as **unpriced**, never as free.

It never runs whole unannounced:

1. **Controls**, free: the scorers fed each case's oracle answer and a known-wrong
   one. The oracle must score 1.0 and pass every gate, the wrong answer 0.0, or the
   run stops — the metric is broken, not the model.
2. **Smoke**, cents: two cases through every arm once, and an estimate of the full
   run at list prices.
3. **The run**, with `--run`: arms × cases × trials into a ledger under
   `evals/runs/` that resumes where it stopped, then the tables — overall and per
   group — and, for each candidate against the first arm, a verdict:

| Verdict | When |
|---|---|
| `INVALID` | an arm's data failed the validity guards — too many errors or empty answers |
| `STOP` | the candidate broke an invariant, even once |
| `INCONCLUSIVE` | fewer than 10 paired cases: no margin worth having is testable |
| `NON-INFERIOR within δ` | the 95% one-sided lower bound of the paired accuracy difference is above −δ |
| `NOT SHOWN NON-INFERIOR` | it is not |

Cost per successful task is printed beside the verdict for both arms, and never
folded into it.

`evals/cases/starter.jsonl` is a **starter set** — facts, arithmetic, and four
instruction-following cases that carry gates. It exercises the instrument; it does
not represent what our agents are for, which is the open question below.
`tests/test_quality.py` checks the instrument itself in CI, for free.

`evals/cases/agents.jsonl` (36 cases) is the set both model runs of 2026-09-30
used. Most models solve nearly all of it, so it compares platforms well but ranks
models poorly. `evals/cases/medium.jsonl` and `hard.jsonl` (12 each, text only) are
for ranking. They are written by `evals/cases/build_sets.py`, which computes every
answer key from the task's own data, so no key is written by hand. They are
calibrated on the providers' own agent APIs, never on ours. A case that every
calibration model solves is rewritten, not kept: it only adds cost. When the
strongest model alone disagrees with the key, the key is checked first. That is how
h-policy-b's key was corrected.

### Where results live, and what they cost

Every run is a directory under **`evals/results/<date>-<name>/`**, on the machine
that ran it; the repository ignores it. The results are summarised in
[benchmarks.md](benchmarks.md); the runs themselves are yours to repeat:

| File | What |
|---|---|
| `ledger.jsonl` | every attempt, written as it happens: arm, case, trial, the answer, pass or fail, the invariants broken, time per turn and the model's own time, fresh, cached and output tokens |
| `passport.json` | what the run was run under: when, the commit (and whether the working tree had changes), library versions, the case set's hash, each arm's platform and host, trials, the prices in force |
| `report.md` | the tables and verdicts |

**Prices come from `evals/prices.json`**, never from a library's table. Each price
says where it was read and when, and a price that is temporary says what follows it
(Gemini 3.8 Flash's introductory price ends on 2026-12-31). A report prices a ledger
from its tokens on the run's date, so `--rescore evals/results/<run>` reprices and
rescores without calling a model. A model with no price is *unpriced*, never free;
a cache price nobody published is charged as fresh input, and the report says so.

**Time is split** where the API reports the model's own spans: the model's time,
and the platform's — the rest of the turn. That is the number an "original against
ours" comparison is about.

## When each runs

| Layer | When | Cost |
|---|---|---|
| 1 — shape | every commit, every deploy | free |
| 2 — behaviour | every deploy against recorded goldens; recording once per API version | recording costs once |
| 3 — model quality | on demand, before changing a model; never in CI | real money — `smoke()` first |

## Open

1. **Recording the goldens** from the originals — both are reachable now, with the
   owner's keys, from a local run. Until then layer two holds our behaviour still
   but says nothing about parity.
2. **Our own cases for layer 3** — which tasks represent what our agents are for.
   The starter set proves the instrument, not a model. Tool-using cases, and
   τ²-bench through `aichain`'s adapter, wait for tools to be wired.
3. **Thresholds** — the margin δ for a model switch (the default of 0.05 is a
   placeholder, not a decision), and how cost per successful task weighs against
   pass rate.
