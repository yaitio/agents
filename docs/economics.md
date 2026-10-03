# Economics

What a task costs here against what the same task costs on a managed service, and
which of those numbers is worth putting in a sentence.

Prices are list prices, `us-east-1`, checked September 2026; they move, so check
one before relying on it. Measured costs are from the runs in
[benchmarks.md](benchmarks.md), 29 September – 1 October 2026.

## Measured

What a solved task cost, model and infrastructure, on the same tasks:

| | per solved task |
|---|---|
| Tool tasks — Claude Haiku 4.5 on Managed Agents | $0.0139 |
| Tool tasks — Claude Haiku 4.5 here | $0.0113 |
| Tool tasks — GPT-6 Luna, gpt-oss-120b, GLM-5.3-Flash (FP8) here, all 36/36 | $0.0003 – 0.0006 |
| Hard tasks — Claude Opus 5.5 / GPT-6 Astra here | $0.0169 / $0.0251 |
| Hard tasks — Qwen3.8 Flash / GLM-5.3-Flash here, 0.97 / 0.94 solved | $0.0014 / $0.0013 |

On the tool tasks, open models that solved every one cost **8 to 55 times less**
per solved task than Haiku on Managed Agents. On the hard set, models at a tenth
of Opus's price solve as many tasks or more. The infrastructure is in these
numbers and does not show: a step costs about $0.0001 of Lambda.

**Prompt caching is worth a third of the bill on a tool task.** Before the last
block of the conversation was marked for Claude's cache, a Haiku tool task sent
13,106 fresh input tokens per turn and cost $0.0271; after, 1,807 and $0.0156,
against $0.0146 for the same task on Managed Agents.

## The overhead is not the argument

**Neither managed service charges for tokens alone.** Both add a runtime charge,
and ours is lower than both — which turns out to be almost irrelevant.

| | Charge beyond tokens | Accrues while |
|---|---|---|
| Anthropic | **$0.08 per session-hour**, to the millisecond | the session is `running` |
| OpenAI, 1 GB container | **$0.03 per 20 minutes** ⇒ $0.09/hour; per-minute billing with a five-minute minimum | the container **exists** — and it lives up to an hour after the last activity |
| OpenAI, 4 / 16 / 64 GB | $0.36 / $1.44 / $5.76 per hour | the same |
| **Here**, Lambda at 1 GB | **$0.060/hour** | an invocation is alive |

Anthropic bills the way we do — only while work is happening — which is itself
evidence they run the same shape; their engineering note describes a harness that
is woken with `wake(sessionId)` and rebuilt from the event log, and a charge
metered to the millisecond of `running` is how you price something that really
does stop. OpenAI bills the container's existence, so a session of theirs that
waits for a person keeps costing until the sandbox is reclaimed.

A worked example at list prices. One session, thirty minutes, forty model calls averaging
40 k input and 3 k output — 1.6 M input tokens and 0.12 M output:

| | Tokens | Overhead | Total | |
|---|---|---|---|---|
| Opus 5.5, managed | $11.00 | $0.040 | **$11.04** | |
| Opus 5.5, here | $11.00 | $0.030 | **$11.03** | 0.09 % less |
| Sonnet 5, here | $4.40 | $0.030 | **$4.43** | **2.5× less** |
| Haiku 4.5, here | $2.20 | $0.030 | **$2.23** | **5× less** |
| A hosted open model, illustratively $0.20 / $0.60 per Mtok | $0.39 | $0.030 | **$0.42** | **26× less** |

**The overhead is 0.27 % of the bill.** The difference between their $0.08 and our
$0.060 is one cent on an eleven-dollar task — 0.09 %.

So leading with the hourly rate offers a quarter off a quarter of a percent, and
invites the buyer to compare us on the one axis where the difference is
negligible. **The multiple is in the model, and only in the model.** That is the
sentence:

> The same API. Any model — from Opus to an open one on any OpenAI-compatible
> server. In your account, with your data.

The hourly figure belongs further down, where it answers an objection rather than
making a promise: our overhead is no higher than theirs, and it does not accrue
while a session waits.

## What we do not charge for

**Data residency.** Anthropic bills tokens at **1.1×** when an agent's
`inference_geo` is pinned to `us`. In a deployment you own, residency is a
property of where you deployed it — not an option with a surcharge. For a buyer
with data requirements that is a line in a budget, not only a line in a
compliance document.

Web search is $10 per 1000 calls on both services. Here it is an MCP server of
your choice — Exa in our tests — at whatever it charges, with nothing added.

## What the infrastructure actually costs

Two facts about Lambda that decide two settings.

**Lambda bills wall-clock time, including time blocked on the network.** While
the model generates, the step worker is alive, holding a socket, doing nothing —
and billed exactly as if it were computing. This is not a flaw to design around:
one Opus request at 50 k input and 5 k output costs $0.375, more than six hours
of the 1 GB function that waited for it. But it does mean **memory is a multiplier
on idle waiting**: the same six hours at 10 GB cost $3.60 instead of $0.36. On
Lambda memory also buys CPU, and CPU is what a cold start spends — so the setting
was measured, not chosen.

**Measured, 2026-10-01.** At 256 MB a cold step worker took 15–20 s to start —
importing the model and MCP libraries on about a sixth of a core — and used
200 MB of its 256. A warm one ran a three-tool turn in 8.8 s. So the worker gets
its own setting, `YAIT_WORKER_MEMORY_MB`, at 1024 MB; the API function stays at
256. A ten-second step at 1 GB costs about $0.00013 of Lambda against $0.00003 at
256 MB — against a model call of $0.002–0.03, noise. A task of several MCP
servers went from 56 s to 34 s at the median.

**Waiting for a person costs nothing.** No invocation is alive while a session is
idle; what remains is DynamoDB storage. A session of two hundred steps costs on
the order of a tenth of a cent in DynamoDB writes, and API Gateway $3.50 per
million requests.

**Streams will be where continuous billing bites**, when the streaming function on
the roadmap is built: a hundred open streams at 1 GB, doing nothing but polling
the log, would cost about **$4,320 a month**. That function will need an idle
close, caps per session and per token, and its own reserved concurrency.

**The ceiling is concurrency, not money.** A thousand concurrent executions per
region by default, so a thousand sessions thinking at once reach an account limit
long before they reach a surprising bill.

## The hole this positioning opens

A model reached through `aichain`'s `private/` prefix — any OpenAI-compatible
server, which is how an open model or an aggregator is used — **has no price**.
The library says so deliberately:

> No `[models]` section, deliberately. A model you host has no price per token:
> its economics are GPU-hours divided by throughput… `estimate_cost()` returns
> None for them, and that None is honest — a number here would be invented.

That is the right call for a library, and it leaves us with a consequence: **a
budget cannot measure a session on such a model**, because it is enforced from the
price registry. So a session with a budget refuses a model with no price, rather
than letting a ceiling it cannot measure pass for one that holds.

The fix is a price table per deployment, filled in by whoever runs the server,
since they alone know what a token costs them. **Open.**

## Two places the pitch is weaker than it sounds

**"Administer it yourself" is a cost**, it is just not on a price list. For a
developer who already runs AWS it is a feature. For a company it is an engineer's
time, an on-call rota and upgrades, and they will price it. There the argument is
not the hourly rate — it is that the data never leaves, and that the residency
their compliance needs costs 1.1× on the other side.

**"The same API" is not yet complete.** Without a sandbox, six of the eight tools
in `agent_toolset_20260401` — `bash`, `read`, `write`, `edit`, `glob`, `grep` —
have nothing to run on; client-side tools and approvals are not served either
([roadmap](../README.md#roadmap)). Until they are, the honest phrasing is "the
same API for agents with MCP tools", not "full parity".
