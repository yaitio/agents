# Data model — draft

The tables, keys and item shapes [architecture.md](architecture.md) relies on.
Everything here is a proposal until code exists.

Three rules hold everywhere and explain most of the shapes below:

1. **The tenant is a prefix of every partition key.** A query without a tenant
   has no key to write, so the isolation is structural rather than careful. An
   IAM role per tenant with a `dynamodb:LeadingKeys` condition makes it
   enforceable where required.
2. **Ordering comes from conditional writes, not from the queue.** Every event is
   appended with `attribute_not_exists(sk)`, so the sequence number is assigned
   by whichever write wins.
3. **Nothing large lives in an item.** DynamoDB's limit is 400 KB; anything over
   64 KB goes to S3 and the item carries a pointer. One enormous tool result can
   never make a session unreadable.

All tables are on-demand capacity: a deployment that is idle costs storage only.

## Tables

### `sessions`

One item per session — the cursor, the lease, the status and the counters.
Read and written on every step, so it is kept small.

| | |
|---|---|
| `pk` | `T#<tenant>#S#<session>` |
| `sk` | `meta` |

| Attribute | Meaning |
|---|---|
| `status` | the **internal** state, projected per dialect on the way out — see [api.md](api.md#status-four-values-each-one-name-in-common). Neither dialect's enum is stored |
| `outcome` | success or failure, held separately because Anthropic's `terminated` covers both and OpenAI's `failed` cannot otherwise be rendered |
| `dialect` | which façade created the session; recorded for audit, never to change behaviour |
| `agent_id`, `agent_version` | the pinned agent; a session never follows a moving version |
| `model` | the session's override, when it has one |
| `cursor` | the highest committed event sequence |
| `agent_state` | the library's `state` dict — `steps`, `tokens`, `cost`, `plan`, `adaptations` |
| `lease_holder`, `lease_until` | the single-writer lease; taken with a conditional update |
| `budget_limit_usd`, `budget_spent_usd` | the ceiling and an atomic counter |
| `run_id` | set while a run is suspended; the key into `runs` |
| `awaiting` | `{reason, resume_with}`, copied out for the API to render without loading the run document |
| `parked_index` | present only while parked — the sparse key of `GSI-parked` |
| `month` | `YYYY-MM`, the shard key of `GSI-recent` |
| `created_at`, `updated_at` | |

**`GSI-recent`** — `pk = T#<tenant>#<month>`, `sk = U#<updated_at>#<session>`.
Lists a tenant's sessions newest first. Sharded by month because an unsharded
per-tenant index is one partition taking every write.

**`GSI-parked`** — `pk = T#<tenant>#parked`, `sk = <expires_at>`. Sparse: only
parked sessions carry `parked_index`. This is what answers "how many parked runs
does this tenant have" and what the expiry sweep reads.

A child in a swarm is a session of its own: `pk = T#<tenant>#S#<session>#C#<child>`.
Its own partition, its own fold, its own budget draw against the parent's counter.

### `events`

The log. Append-only, and the only source of truth for what happened.

| | |
|---|---|
| `pk` | `T#<tenant>#S#<session>` |
| `sk` | `seq` — a **number**, not a padded string, so the store's ordering is the sequence's ordering and there is no padding bug to find later |

| Attribute | Meaning |
|---|---|
| `type` | our own vocabulary, not a dialect's: the user's message, the agent's message, a tool call, its *started* record, its result, an approval requested and decided, a client-supplied tool result, an interrupt, a failed step |
| `turn_id` | the turn this event belongs to. The Anthropic dialect never exposes turns; OpenAI serves `/turns` and `/items` from them, so they are recorded whether or not anyone asks |
| `item_id` | identity of the item this event produced, for the same reason |
| `call_id` | on tool calls — the second way a pending call is addressed. Anthropic keys a confirmation by the **event id**, OpenAI by `turn_id` + `call_id`; both must exist or one façade cannot resolve its own pending work |
| `ts` | when it was committed |
| `actor` | who caused it — the client key, a user, a schedule, the agent itself |
| `payload` or `payload_s3` | the body, or its pointer above 64 KB |
| `usage`, `cost_usd`, `model` | on model calls only; this is what a bill is reconstructed from |
| `placement`, `plugin` | on plugin calls — which runner served it |
| `ttl` | `created_at + retention_days` |

Written with `ConditionExpression: attribute_not_exists(sk)`. Sequence 0 is
`session.created`, so a fold never has to special-case an empty partition.

### `deltas`

Text fragments, for the stream only. **The fold never reads this table**, which
is why it is a table and not a range of the log.

| | |
|---|---|
| `pk` | `T#<tenant>#S#<session>` |
| `sk` | `<seq>#<n>` — the event being built, and the fragment's index |
| `ttl` | minutes |

Nothing here is load-bearing, and that is a property of both dialects rather than
a convenience of ours: neither replays its stream, so an expired fragment cannot
strand anything. Anthropic's deltas are requested per connection and are
explicitly never persisted; OpenAI's are ordinary events in its enumeration but
are equally unreplayable. A client that ignores deltas still receives a complete,
correct stream, and the committed event is always the authoritative record.

The rows exist only so a connection that asked for previews can be served from
the same store as everything else. They are written on the way past, read once,
and expire.

### `runs`

The library's `StateStore`, and nothing else. It holds a run document only while
the run is suspended — that narrowness is what gives the deduplication property:
`resume()` on a `run_id` the store no longer holds raises `KeyError`, which
at-least-once delivery reads as "already handled".

| | |
|---|---|
| `pk` | `T#<tenant>#R#<run_id>` |
| `sk` | `doc` |

| Attribute | Meaning |
|---|---|
| `schema_version` | the run document's format. **The library has no such field today**, and without it a format change can only be answered by breaking every stored run |
| `document` or `document_s3` | the document, or its pointer |
| `awaiting` | `{reason, resume_with}` — the shape a signal must match |
| `hint` | what should wake it: `{"cron": …}`, `{"on": …}`, `{"wake_at": …}` |
| `session` | the session this run belongs to |
| `expires_at`, `ttl` | thirty days by default; an armed run nothing ever wakes is a leak |

An armed subscription is a run in this table that has taken no step. There is no
separate subscriptions table: its `run_id` *is* the subscription.

### `agents`

| | |
|---|---|
| `pk` | `T#<tenant>#A#<agent_id>` |
| `sk` | `V#<version>`, and `meta` for the pointer to the current version |

The definition inline, or in the versioned S3 bucket when large. Versions are
never mutated; a session pins one at creation.

### `memory`

| | |
|---|---|
| `pk` | `T#<tenant>#M#<store>` |
| `sk` | `D#<path>` for the head, `D#<path>#V#<rev>` for a version |

| Attribute | Meaning |
|---|---|
| `content` or `content_s3` | |
| `content_sha256` | the precondition an update is written against |
| `rev`, `updated_at`, `redacted` | |
| `ttl` | on versions only — thirty days. **The head has no TTL** |

The vector index is a separate store over these same documents, written in the
same operation. A search that can find what a delete removed is worse than no
search.

### `audit`

Approvals with their author, plugin installs and grants, secret reads, domain
changes. A separate table from `events` for two reasons: it must outlive the
event retention, and it is read by a different role.

| | |
|---|---|
| `pk` | `T#<tenant>#<YYYY-MM>` |
| `sk` | `<ts>#<ulid>` |

No TTL by default. Append-only by IAM policy, not by convention.

### `idempotency`

A client's `Idempotency-Key` on a write, so a retried POST does not become a
second user message.

| | |
|---|---|
| `pk` | `T#<tenant>#I#<key>` |
| `sk` | `meta` |
| | the resulting event sequence, and a `ttl` of 24 h |

## The step's commit

One `TransactWriteItems` at each commit point:

- a `Put` per event, each with `attribute_not_exists(sk)`;
- one `Update` on the session item: `cursor`, `agent_state`, `status`, the lease,
  and `ADD budget_spent_usd`.

This is what makes "every state transition is written before it is acted on" true
rather than aspirational: the events and the counters cannot disagree. Two costs
are accepted knowingly — a transaction is billed at twice a write, and the limits
are 100 items and 4 MB. A step emits a handful of events, so the limits are
headroom; if one ever exceeds them, the events go first (the conditional append
makes them safe to repeat) and the session update follows.

A step with a side effect commits twice: once for `tool_call.started` before the
tool runs, once for everything else after it.

## Read patterns

| Question | Access |
|---|---|
| What is the conversation? | `Query` on the session partition of `events`, folded |
| What happened after event N? | the same query with `sk > N` — the stream and the history endpoint are one code path |
| What is this session doing? | `GetItem` on `sessions` |
| This tenant's recent sessions | `Query GSI-recent` |
| How many parked runs, and which expire next? | `Query GSI-parked` |
| Resume this signal | `GetItem` on `runs`, then the library's `resume()` |
| What did this cost? | `Query` the session partition, sum `cost_usd` — raw records, not an estimate |
| Who approved this? | `Query` the tenant's month in `audit` |

## S3

| Bucket / prefix | Holds |
|---|---|
| `payloads/T/<tenant>/S/<session>/E/<seq>` | event payloads over 64 KB |
| `runs/T/<tenant>/R/<run_id>` | run documents over the item limit |
| `artifacts/T/<tenant>/S/<session>/…` | what a run produced or consumed; this is also what a plugin's `workspace` maps to in the cloud |
| `packages/` (versioned) | agent definitions and skill packages |

A plugin never receives a host path — it receives a workspace, and the runner
decides whether that is a bind mount or one of these prefixes. That is what lets
the same image digest run on a laptop and in the account.

## Retention

| Data | Default |
|---|---|
| Events | 30 days, configurable |
| Deltas | minutes |
| Memory versions | 30 days; the head forever |
| Run documents / subscriptions | 30 days from arming |
| Audit | no expiry |
| Idempotency keys | 24 hours |

Every one of these is a TTL attribute rather than a sweeper job, except the
parked-run expiry, which needs to write a `step.failed` and notify — so it reads
`GSI-parked` on a schedule and does the work deliberately.

## Open decisions

1. **Separate tables or one.** Six purposeful tables are written above because
   per-table IAM is legible and the access patterns differ. A single-table design
   would cost fewer resources and more explaining.
2. **The fold's cost at scale.** A session with thousands of events pays for its
   whole partition on every step. A materialised fold in S3, keyed by cursor,
   fixes it and is not needed until measured.
3. **The vector backend** — pgvector on Aurora Serverless, or S3 Vectors. Some
   backends bill for capacity while idle, which is why semantic search is off by
   default.
4. **Cross-region** is out of scope for version one, and the key shape above does
   not prejudge it: nothing in a key names a region.
