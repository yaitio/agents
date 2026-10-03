# Architecture

How the service works, as built. The contract it serves is [api.md](api.md) — two
dialects, one service; the tables it writes to are
[data-model.md](data-model.md). What is not built yet is at the end, and on the
[roadmap](../README.md#roadmap).

## The shape

```
                      /anthropic/v1/…  ─┐
client ──HTTPS──► API Gateway (REST)    ├─► API function ── validates, appends the
   X-Yait-Key      │                    │   (both dialects)  event, wakes the worker
                   │  /openai/v1/…    ──┘          │
                   ▼                                │ async invoke
             authorizer: the API                    ▼
             function checks the JWT,      Step worker ── model call ──► provider
             answer cached 5 min           (no dialect)     (the caller's key)
                                                 │
                                                 └── tool call ──► MCP servers
                                                     (credential from the vault)
                                                 ▼
                                   commit: events + session item, one transaction
```

| Component | AWS | Does |
|---|---|---|
| API | API Gateway REST, one stage | both dialect trees under `/anthropic` and `/openai`; a request authorizer on `X-Yait-Key` |
| API function | Lambda, 256 MB, 10 s | the JWT check (as the authorizer); every JSON endpoint of both dialects; the first write; waking the worker |
| Step worker | Lambda, 1 GB, 900 s | the model call and the tool calls; knows no dialect |
| Sessions, events, agents | DynamoDB, on demand | the session item, the event log, agents and their versions, vaults, the public signing key |
| Vault key | Secrets Manager | the AES-256-GCM key credentials are sealed with |

**The functions are divided by runtime property, not by dialect:** a request path
that answers at once, and a worker that may think for minutes. The dialect is a
serializer at the edge of the API function; the worker writes one event
vocabulary for both.

## Access

Every request carries a JWT in `X-Yait-Key`, signed with Ed25519 by the
installation's key ([deploy.md](deploy.md#6-the-installations-keys)).

- **API Gateway checks it before any endpoint runs.** The authorizer is the API
  function itself, called in authorizer mode: it verifies the signature against
  the public key in the agents table and returns the token's tenant. API Gateway
  caches the answer per token for five minutes, so a client's requests cost one
  check per five minutes, not one per request.
- **The tenant comes from the token, never from the request.** Every partition key
  starts with it, so a query without a tenant has no key to write.
- **The service can check a token, never make one.** The private key stays on the
  machine that ran `keygen`.

The provider key is the caller's own and travels where the SDK puts it
(`x-api-key`, `Authorization: Bearer`). It is used for the request and the worker
run it starts, and stored nowhere — see [Isolation](#isolation).

## The unit of work is one step

The agent's state is its conversation, so the worker reads the log, takes a step —
one model call, and the tool calls it asked for — and writes what happened.
Nothing lives in memory between steps, so any invocation can take the next one.

**The invariant is durability, not throughput.** After every step the stored
state is complete enough for a fresh invocation to continue. One invocation takes
as many steps as its time allows; that saves a cold start per step and changes
nothing about correctness.

- **Lambda's 15-minute limit is a checkpoint, not a ceiling.** A worker stops
  starting steps when less than five minutes are left (`YAIT_WORKER_RESERVE_S`,
  longer than the longest model call), releases the session and invokes itself:
  the handoff re-reads the log and goes on.
- **Waiting costs nothing.** A session waiting for the user is a status in a
  table; no invocation is alive.

## What is the truth

| Store | Job |
|---|---|
| **Event log** | the only source of truth for what happened |
| **Session item** | the status, the lease, the budget, the pinned agent version, the library's run counters (`steps`, `tokens`, `cost`) |

**The conversation is folded from the log, never stored as a second copy.** A step
reads the session's partition with one query and folds the events into the message
list the model is sent (`src/fold.py`). Saving the whole conversation each step
would put a growing document under DynamoDB's 400 KB item limit and make the log
decorative.

- **What the fold reads:** the user's messages, the agent's messages, its tool
  calls and their results, interrupts.
- **What it ignores:** status events, request start and end, thinking markers,
  usage — anything observational.
- **The log is a superset of both dialects.** It records turns and items, which
  the Anthropic dialect never shows, and both ways a tool call is addressed, so
  each dialect's view is a projection of the same log (`src/project.py`,
  `src/render.py`).
- **The fold is a pure function and is tested as one.** If it drifts, the agent's
  memory is wrong in a way no error surfaces.
- **No event outgrows an item.** A tool result is cut at 64 KB
  (`YAIT_TOOL_RESULT_CHARS`), saying how long it was.

## One step, in order

1. **Woken** by the API (a message, an interrupt) or by its own handoff, with the
   tenant and the session — and the caller's provider key, held in the invocation
   payload and nowhere else.
2. **Take the lease** — a conditional update on the session item: it succeeds if
   the lease is free or expired. A failure means another invocation is working,
   and this one exits.
3. **Find the turn** — the *earliest* turn not yet ended. A worker that died
   answering turn A leaves A first in line, even with B queued behind it.
4. **Check the ceilings** — the session's budget against the cost so far, and the
   number of tool rounds in this turn (20). Either ends the turn by name.
5. **List the tools** of the agent's MCP servers, with the session's credentials.
   A server that refuses or cannot be reached ends the turn with
   `mcp_authentication` or `mcp_connection` naming it.
6. **Commit the start** — `model_request.start`, so the turn reads in progress
   and a worker that dies during the call leaves a turn the next one resumes.
7. **Fold and ask** — the log as it is after that commit, so a message that joined
   the turn in between is in the conversation.
8. The answer is either:
   - **text** — commit the message and close the turn: usage, then `idle` with
     `end_turn` — unless another message is queued, in which case the session
     stays running and the next turn starts;
   - **tool calls** — commit the calls *before* running them, run each, commit
     the results. The turn stays open; the next step shows the model what came
     back.
9. **Go round** while there is a pending turn and time left. Then release the
   lease, and **look once more** (below).

**A tool call commits twice**, before and after. A worker that dies between the
two leaves a call with no result; the fold answers it with *the outcome of this
call is unknown*, and the model is told so plainly. A side effect of unknown
outcome is never silently repeated, and never recorded as a clean failure.

## Ordering, and one writer per session

Three mechanisms, each covering what the one before cannot.

1. **The lease on the session item** — one worker per session. It lasts longer
   than the worker's timeout (930 s), so a live worker never loses it, and a
   killed one frees the session within minutes.
2. **A conditional append** — every event is written with `attribute_not_exists`
   at the next sequence number, in one transaction with the session item. The API
   and the worker both append; the loser of a race re-reads and commits at the
   new cursor. It costs a re-read, never a second model call.
3. **The lost wakeup** — a message that arrives while a worker holds the lease
   wakes an invocation that cannot take it and exits; the holder may already have
   decided there is nothing left. So after releasing the lease the holder reads
   once more, and goes round if something is pending.

There is no queue: the API invokes the worker asynchronously, and ordering is a
property of the store. Lambda retries a failed asynchronous invocation, and the
lease and the conditional append make a retry harmless.

## States

The internal state is richer than either dialect, because each is a lossy
projection and they lose different things — see
[api.md](api.md#status-four-values-each-one-name-in-common).

| Internal | Meaning | Anthropic renders | OpenAI renders |
|---|---|---|---|
| queued | a message accepted, nothing has picked it up | `running` | `in_progress` |
| working | a worker holds the lease | `running` | `in_progress` |
| turn over | the answer is given; the user has the floor | `idle` + `stop_reason: end_turn` — **finishing is `idle`, never `terminated`** | `idle` |
| at a ceiling | the budget is spent | `idle` + `stop_reason: budget_reached` | `idle` |
| failed | the session itself failed | `terminated` + `session.error` | `failed` + `error` |
| archived | closed to new events, history kept | `terminated` | `idle` — OpenAI has delete, not archive |
| deleted | gone from the API | not found | not found |

A turn that fails — a model error, an MCP server, the tool-round ceiling — ends
the turn, not the session: `session.error` (Anthropic) or an `error` item
(OpenAI), then `idle` with `retries_exhausted`, and the next message is answered
as usual.

**Deletion** answers at once; the worker then purges the log and the session
item, handing off if it runs out of time. It is refused while a turn is under way
(409).

## Models

The worker calls the model through [`aichain`](https://github.com/yaitio/aichain):
one interface over Anthropic, OpenAI, Google, xAI, DeepSeek, Qwen, OpenRouter and
any OpenAI-compatible server named in `YAIT_ENDPOINTS`. The model name selects the
provider, and the caller's key is asked for per request, so one cached client per
model name serves every caller.

- **Thinking as on Managed Agents.** Claude thinks at a medium budget; Claude 5
  and later with adaptive thinking, which they require. Other models keep their
  provider's default.
- **The answer ceiling** is 32,768 tokens, thinking included (`YAIT_MAX_TOKENS`).
  An answer cut off at it is a failed turn saying so, not an empty answer.
- **Prompt caching.** For Claude, the last block of the conversation is marked,
  so each step reads the previous one's prefix from the cache; OpenAI caches by
  itself, and the cached tokens are read back so cost is not overstated.
- **Usage and cost** are written per call to the log, from the library's price
  registry; a bill is reconstructed from records, not estimated.

## Tools

Tools are **MCP servers**, several per agent, reached over streamable HTTP with
`aichain`'s MCP client (`src/tools.py`).

- **What an agent may declare is checked when it is made.** An unsupported tool
  type or `always_ask` is refused by name, not ignored.
- **Credentials come from the session's vaults**, matched by the server's URL, and
  are put in the request's `Authorization` header for that call only.
- **Limits:** 30 s to connect and list, 120 s per call, 64 KB per result, 20 tool
  rounds per turn.
- **A failed call is a result**, not an exception: the model sees the error and
  decides. A server that cannot be listed at all fails the turn by name.
- **The tool list is cached for one run** and dropped when it ends, since it
  carries the credential.

## Vaults

Credentials for MCP servers, in both dialects' vault APIs (`src/vault.py`).

- **Sealed with AES-256-GCM** before they reach DynamoDB, under a key in Secrets
  Manager. The associated data binds each ciphertext to its tenant, vault and
  credential, so a sealed value copied to another item does not open.
- **Never returned.** The API answers with the credential's metadata; the secret
  is opened by the worker for one MCP call and not kept.
- **Rotation** of the key keeps the old one for reading
  ([operations.md](operations.md#rotating-keys)).

## Isolation

Lambda's isolation separates AWS customers from each other. Within the service:

- **Warm environments are reused**, and the next invocation in one may be another
  tenant's. So nothing of a caller outlives its invocation:

  | May live across invocations | Must not |
  |---|---|
  | the installation's own: the public key tokens are checked with, the vault key, the named model servers, `aichain` clients by model name (they ask for the key per request) | a caller's provider key (a context variable, reset when the run ends; the worker's handoff that closes over it is cleared too) |
  | | a vault credential, opened only for the MCP call it goes on |
  | | the MCP tools a run listed, which carry that credential in their headers — kept for one run, dropped when it ends |

  Nothing is written to `/tmp`. `tests/test_worker.py` (*nothing of the caller
  outlives a run*) runs a tool turn, then searches every live object in the
  process for the caller's bearer token and provider key; it fails if either is
  found, and was checked to fail with the cleanup taken out.
- **No key is logged.** A request is logged with its tenant and token name; a
  provider key, a token or a credential never.
- **The functions' role** reaches the three tables, the vault key and the logs,
  and nothing else in the account ([deploy.md](deploy.md#which-rights-sit-on-which-identity)).
- **The worker runs no agent-supplied code.** Tools are remote MCP servers; a
  sandbox for code is a separate container, specified in
  [sandbox-tools.md](sandbox-tools.md) and not built.
- **Prompt injection is not an isolation problem.** An agent with a legitimate
  "send" tool can misuse it anywhere. Approvals before a call are the control,
  and they are on the roadmap.

## When things fail

| Failure | What happens |
|---|---|
| The worker is killed mid-step | the lease expires; the next wake-up finds the earliest open turn and resumes it; a tool call with no result is answered "outcome unknown" |
| The same wake-up arrives twice | the lease drops the second; if it slips through, the conditional append refuses it |
| A model call fails | the library retries transient errors; then the turn ends with `session.error` of a named type (`rate_limited`, `authentication`, `overloaded`, …) and the log keeps the attempt |
| The answer hits the ceiling | the turn fails saying the answer was cut off |
| A tool call fails | a result the model sees, with `is_error` |
| An MCP server refuses or is unreachable | the turn fails with `mcp_authentication_failed_error` or `mcp_connection_failed_error` naming the server |
| The budget is spent | the call is refused before it begins; the session goes `idle` with `budget_reached` |
| An interrupt during a step | the turn ends at once; a tool result that arrives later is recorded as late and not shown |

Nothing cut short is stored as a clean result.

## Money

A session's `budget` is a list-price ceiling in USD. The worker checks it before
every model call, against the cost so far from the library's price registry: what
a reply costs is not known until it is paid for, so the session stops *issuing*
calls once it is reached. A model with no known price is refused under a budget,
since a ceiling that cannot measure it must not let it run. The OpenAI dialect has
no session budget; a session made there has none.

What a task costs, and what the infrastructure adds: [economics.md](economics.md).

## Latency

Measured on the deployment, not targets:

| | |
|---|---|
| A warm turn of three tool calls | 8.8 s, of which the model is about 5 |
| Cold start of the worker | 15–20 s at 256 MB, which is why it has 1 GB |
| A task of several MCP servers, Claude Haiku, p50 | 56 s at 256 MB → 34 s at 1 GB |

## Compatibility with the official SDKs

One internal session and event model; a thin façade per dialect. The official
SDKs' types and OpenAI's published OpenAPI document are the contract tests, run on
every release against the deployment ([testing.md](testing.md)). A field this
service does not support fails with a named error rather than being ignored.

## Not built

| | |
|---|---|
| Streamed events | API Gateway buffers responses, so the stream endpoints answer a stub and clients read the event list. A streaming function of its own is on the roadmap |
| Client-side tools, approvals | `custom` / `function` tools and `always_ask` are refused when an agent is made |
| A sandbox | specified in [sandbox-tools.md](sandbox-tools.md) |
| OAuth for MCP | vaults hold `static_bearer`; `mcp_oauth` refresh is next |
| Schedules, webhooks, sub-agents, memory stores, plugins | not started; [plugins.md](plugins.md) is a draft |
| Payloads over 64 KB in S3 | results are cut at 64 KB instead |
| Snapshots of the fold | one query per step is enough so far; a session of thousands of events will need one |
