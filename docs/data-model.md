# Data model

The tables, keys and items [architecture.md](architecture.md) relies on, as
built (`src/store.py`, `src/agentstore.py`, `src/vault.py`, `src/tokens.py`).

Three rules hold everywhere:

1. **The tenant is a prefix of every partition key.** It comes from the caller's
   token, never from the request, and a query without one has no key to write.
2. **Ordering comes from conditional writes.** Every event is appended with
   `attribute_not_exists`, so its sequence number belongs to whichever write wins.
3. **Nothing large lives in an item.** DynamoDB's limit is 400 KB; a tool result
   is cut at 64 KB before it is written.

Three tables, all on-demand: an idle deployment pays for storage only.

| Table | Default name | Holds |
|---|---|---|
| sessions | `yait_agents_sessions` | one item per session |
| events | `yait_agents_events` | the log |
| agents | `yait_agents_agents` | agents and their versions, vaults and credentials, the installation's public keys |

Each has a string partition key `pk` and a sort key `sk`.

## `sessions`

| | |
|---|---|
| `pk` | `T#<tenant>#S#<session>` |
| `sk` | `meta` |

| Attribute | Meaning |
|---|---|
| `tenant`, `session` | the key's parts, kept as fields so a listing can filter on them |
| `status` | the **internal** state (`src/events.py`), projected per dialect on the way out — see [architecture.md](architecture.md#states). Neither dialect's enum is stored |
| `dialect` | which façade created the session; recorded, never used to change behaviour |
| `agent_id`, `agent_version` | the pinned agent version; a session never follows a moving one |
| `agent_overrides` | what an update to the session changed in the agent, applied from the next step |
| `vault_ids` | the vaults whose credentials the session's MCP calls use |
| `budget` | `{type: "limit", max_list_cost: {amount, currency}}`, amount in cents as an integer string |
| `agent_state` | the library's run counters — `steps`, `tokens`, `cost`; the budget is measured against `cost` |
| `cursor` | the highest committed event sequence |
| `lease_holder`, `lease_until` | the single-writer lease, taken with a conditional update; `lease_until` is milliseconds, 0 when released |
| `title`, `metadata` | the caller's |
| `created_at`, `updated_at`, `archived_at`, `deleted_at` | milliseconds since the epoch |
| `purged` | on a deleted session whose log is gone |

**A deleted session leaves a tombstone**: the item is replaced by its key,
`tenant`, `session`, `deleted_at`, `status: deleted` and `purged: true`. A second
delete answers as the first did, and a late wake-up knows there is nothing to do.

## `events`

The log. Append-only, and the only source of truth for what happened.

| | |
|---|---|
| `pk` | `T#<tenant>#S#<session>` — the session's own partition |
| `sk` | the sequence — a **number**, so the store's order is the sequence's order. 0 is `session.created` |

| Attribute | Meaning |
|---|---|
| `type` | our own vocabulary, not a dialect's (`src/events.py`): the user's message, the agent's message and thinking, tool calls and results, model request start and end, status, usage, interrupts, failed steps |
| `event_id` | the id clients dedupe by, separate from the sequence |
| `ts` | when it was made, milliseconds |
| `processed_at` | null while input waits for the worker; both dialects show it |
| `turn_id`, `item_id` | the turn and item it belongs to. The Anthropic dialect never shows turns; OpenAI serves `/turns` and `/items` from them, so they are always recorded |
| `actor` | `user`, `agent` or `system` |
| the payload | the event's own fields, flat: `text`, `calls`, `call_id`, `name`, `server`, `result`, `is_error`, `late`, `usage`, `backend`, `reason`, `error_type`, `mcp_server`, `stop_reason` |

A commit writes the events and the session item in **one
`TransactWriteItems`**: a `Put` per event with `attribute_not_exists(sk)`, and
one `Update` on the session item (cursor, status, `agent_state`, and whatever the
step changed). The events and the counters cannot disagree. A commit that loses a
race re-reads and writes at the new cursor.

## `agents`

One table for everything an installation configures, by key prefix.

### Agents

| | |
|---|---|
| `pk` | `T#<tenant>#A#<agent_id>` |
| `sk` | `meta` — the current version, `archived_at`; `V#<n>` — each version |

A version is never changed; an update writes `V#<n+1>` and moves `meta` with a
condition on the version it read. Each version holds the definition as the
caller gave it: `name`, `model`, `instructions` (Anthropic's `system`),
`tools`, `mcp_servers`, `skills`, `metadata`, `created_at`.

### Vaults and credentials

| | |
|---|---|
| `pk` | `T#<tenant>#VAULT#<vault_id>` |
| `sk` | `vault` — the vault; `C#<credential_id>` — each credential |

| Credential attribute | Meaning |
|---|---|
| `auth` | what may be shown: `type` (`static_bearer`) and `mcp_server_url` |
| `secret` | the token, sealed: `<key id>:<base64 of nonce + AES-256-GCM ciphertext>`, with tenant, vault and credential as associated data. **Never returned** by any API |
| `name`, `metadata`, `created_at`, `updated_at`, `archived_at` | |

The key that seals them is the secret `yait_agents/vault-key` in Secrets Manager,
not in the table ([operations.md](operations.md#rotating-keys)).

### The installation's public keys

| | |
|---|---|
| `pk` | `_installation` |
| `sk` | `jwt_key#<kid>` |

The Ed25519 public key tokens are checked with, written by `keys.py keygen` and
removed by `revoke-key`. `_installation` cannot collide with a tenant's key, which
always starts `T#`.

## Read patterns

| Question | Access |
|---|---|
| What is the conversation? | `Query` on the session partition of `events`, folded |
| What happened after event N? | the same query with `sk > N` |
| What is this session doing? | `GetItem` on `sessions` |
| This tenant's sessions, agents, vaults | a `Scan` filtered by tenant — see below |
| An agent at a version | `GetItem` on `meta`, then on `V#<n>` |
| A credential for an MCP server | `Query` the vault's partition, match `mcp_server_url`, open `secret` |
| What did this cost? | `Query` the session partition and sum `usage`; the session's `agent_state.cost` holds the running total |

## Retention

Nothing expires by itself: there is no TTL. A session's events go when the
session is deleted; an archived session keeps them. The log groups need a
retention set by hand ([operations.md](operations.md#watching-it)).

## Known limits

| | |
|---|---|
| Listings scan | listing a tenant's sessions, agents or vaults is a `Scan` with a filter — fine while listings are rare and human-driven, and the first thing to replace when a deployment grows: a per-tenant index, sharded by month so one tenant is not one hot partition |
| The fold reads the whole partition | a session of thousands of events pays for all of them on every step; a materialised fold keyed by cursor is the fix when measured |
| No idempotency keys | a POST retried by the client after a timeout can add a second message |
| One region | nothing in a key names a region; moving is an export and import ([operations.md](operations.md#regions)) |
