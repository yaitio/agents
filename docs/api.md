# API

The contract. Two dialects over one service, so a client already written against
Anthropic's Managed Agents or OpenAI's Agents API moves by changing `base_url`.

The dialect facts are transcribed from the references rather than recalled — see
[Where these facts come from](#where-these-facts-come-from). **The API shape is
not ours to design.** The only decisions we own are which subset we serve and how
we refuse the rest.

## What is served

| | Anthropic `/anthropic/v1/…` | OpenAI `/openai/v1/…` |
|---|---|---|
| Agents | list, create, get, update, archive, versions | list, create, get, update, delete |
| Sessions | list, create, get, update, archive, delete | list, create, get, update, delete |
| Events | list, send | list, send |
| Turns and items | — (the dialect has none) | turns, one turn, items |
| Vaults | list, create, get, update, archive, delete | list, create, get, delete |
| Credentials | list, create, get, update, archive, delete | list, create, get, rotate, delete |
| Stream | `…/events/stream` answers a stub: API Gateway buffers responses | `stream: true` is refused by name |

27 operations in the Anthropic dialect, 24 in OpenAI's; every one is checked on
each release against the official SDK's types and OpenAI's OpenAPI document
([testing.md](testing.md)). Memory stores, skills, files, schedules, environments
and subagents are not served.

**Events a client may send:**

| | Anthropic | OpenAI |
|---|---|---|
| Accepted | `user.message`, `user.interrupt` | `input.message`, `input.cancel` |
| Refused by name | `user.tool_confirmation`, `user.custom_tool_result`, `user.define_outcome`, `system.message` | `input.tool_result` |

**Tools an agent may declare:** MCP servers — Anthropic's `mcp_servers` with an
`mcp_toolset` per server, OpenAI's `mcp` tool — with `always_allow`. A tool of any
other type, or `always_ask`, is refused when the agent is made. Credentials for
them come from the session's vaults (`static_bearer`).

## Two dialects cannot share a root

They collide. **29 method+path pairs are claimed by both dialects with
incompatible schemas** — among them:

```
GET  /v1/agents          GET  /v1/agents/{id}     POST /v1/agents
GET  /v1/vaults          GET  /v1/vaults/{id}/credentials
GET  /v1/skills          GET  /v1/models          GET  /v1/files
```

`GET /v1/agents` returns an object with `version`, `system` and ISO-8601
timestamps in one dialect, and one with `object: "agent"`, `instructions`,
`multi_agent` and integer Unix seconds in the other. No handler can serve both.

So each dialect gets a path prefix:

| Dialect | Prefix | What the user puts in the SDK |
|---|---|---|
| Anthropic | `/anthropic/v1/…` | `Anthropic(base_url="https://<host>/anthropic")` |
| OpenAI | `/openai/v1/…` | `OpenAI(base_url="https://<host>/openai/v1")` |

**The asymmetry is a documentation trap and gets its own line in the guide:** the
Anthropic SDK's paths already begin with `/v1`, so the prefix is given without
it; the OpenAI SDK carries `/v1` inside `base_url`, so the prefix is given with
it. Getting this wrong produces a 404 that looks like a deployment failure.

**The prefix is not stripped before the function.** It is passed through and its
first segment selects the dialect, so the dialect appears in every log line and
every audit record. With two façades over one event log, that is the only way to
tell later whose client did what.

**Two keys, two headers, two jobs.** The standard header — `x-api-key` from the
Anthropic SDK, `Authorization: Bearer` from the OpenAI SDK — carries the caller's
**own provider key**. It is not checked, not stored and not logged: it travels with
the turn to the worker, which calls the model with it, so the caller is billed by
their provider directly. **The installation never pays for tokens**, and a turn
sent without a provider key fails as an authentication error rather than being paid
for by anyone else.

`X-Yait-Key` carries the **installation token**: a JWT the installation signed,
saying whether this caller may use the deployment at all and whose sessions are
whose (its tenant). API Gateway checks it before the request reaches the function
([architecture.md](architecture.md#access)). One token per user, one user or a
thousand. Both SDKs set it once:

```python
Anthropic(base_url="https://…", api_key=MY_ANTHROPIC_KEY,
          default_headers={"X-Yait-Key": MY_YAIT_KEY})
```

So a client moves by changing `base_url` and adding one default header; its code,
its provider key and its bill stay as they were.

### Which model

The agent's `model` names it, and the provider key in the standard header pays for
it:

| `model` | Reaches | Key the caller brings |
|---|---|---|
| a provider's own name — `claude-haiku-4-5-20251001`, `gpt-6-luna`, `gemini-3.8-flash`, `grok-4.3`, `deepseek-flash`, `kimi-k2.6`, `qwen3.8-flash` | that provider, routed by the library | that provider's |
| `openrouter/<vendor>/<model>` — `openrouter/z-ai/glm-5.3-flash` | OpenRouter, built in | OpenRouter's |
| `<name>/<model>` for a server the owner named in `YAIT_ENDPOINTS` — `mygpu/llama-3.3-70b` | that server, OpenAI-compatible | that server's, if it asks for one |

The address of a named server is the owner's to set, never the caller's: a worker
that went wherever a request pointed it could be pointed at the platform itself.

## What each dialect calls things

| | Anthropic | OpenAI |
|---|---|---|
| Operations in the agent family | **74** | **42** over 25 paths |
| Reusable agent | `/v1/agents`, `/{id}`, **`/{id}/versions`**, archive | `/agents`, `/agents/{agent_id}` — no versions endpoint |
| Session | `/v1/sessions…` at the root | `/agents/sessions…`, nested under agents |
| Send / list events | `POST\|GET /v1/sessions/{id}/events` | `POST\|GET /agents/sessions/{id}/events` |
| Pagination | opaque `next_page` / `prev_page` cursors passed back as `page`; `order` is encoded in the cursor and cannot change mid-walk | `order`, `limit` |
| Live stream | separate `GET …/events/stream` | `?stream=true` on events, **and `stream: true` on session create** |
| Turns | no resource | `/turns`, `/turns/{turn_id}` |
| Items | no resource — the event list is the history | `/items` |
| Artifacts | no resource — session `resources` instead | `/artifacts`, `/artifacts/{id}/content` |
| Subagents | `/threads` + `/events` + `/stream` | `/subagents` + `/items` + `/turns` + `/turns/{id}/items` |
| Environment | separate object; `environment_id` **required** | inline object **required**, or a template; `type: none \| openai_hosted \| self_hosted` |
| Vaults | `/v1/vaults` + credentials + archive + `mcp_oauth_validate` | `/vaults` + credentials, 4 paths |
| Memory stores | `/v1/memory_stores` + memories + versions + redact | **none** |
| Schedules | `/v1/deployments` + pause/unpause/run/archive, `/v1/deployment_runs` | **none** |
| Session budget | `{type: "limit", max_list_cost: {amount, currency}}`, create-only | **none** in the create parameters |

`turns`, `items` and `artifacts` are **read projections** over the same log, as
`threads` are for subagents. Their absence on one side is not less
functionality; it is the same facts read differently.

`environment.type: "none"` is worth noting: OpenAI makes a sandbox-free agent a
first-class configuration, while Anthropic requires an `environment_id`
regardless. Without a sandbox, this service takes the Anthropic session's
`environment_id` and runs no environment behind it.

## Status: four values each, one name in common

| Anthropic | OpenAI |
|---|---|
| `running` | `in_progress` |
| `idle` | `idle` |
| `rescheduling` | — |
| `terminated` — an unrecoverable error, **or** an archive. A session that finishes its work goes `idle`, not `terminated` | `failed` — error only; an archive has no counterpart |
| `requires_action` lives inside `idle`, as `stop_reason: {type, event_ids}` | `requires_action` is **its own status** |

Both are lossy projections of one internal state, which must therefore be richer
than either:

- **`terminated` covers an error and an archive alike**, and normal completion is
   not one of them — a finished session is `idle`. So the internal state carries
   why it ended, or we cannot tell OpenAI's `failed` from a session someone
   archived. OpenAI has no archive at all, only delete, which is a small gap of
   its own.
- **`rescheduling` has no counterpart.** Anthropic exposes the state between
  invocations; the OpenAI façade renders it as `in_progress`. That is honest: a
  turn is in progress, nothing is running this instant.
- **Pending work is addressed differently** — by event id (`sevt_…`) in one
  dialect, by `turn_id` + `call_id` in the other. Both must exist on every tool
  call we record.

## Events

| | Anthropic | OpenAI |
|---|---|---|
| Naming | two levels: `agent.message`, `session.status_idle`, `span.model_request_start` | a tree: `agent.session.turn.output_text.delta` |
| Received types | ~25 | **37** |
| Sent types | **6** — `user.message`, `user.interrupt`, `user.tool_confirmation`, `user.custom_tool_result`, `user.define_outcome`, `system.message` | **3** — `input.message`, `input.cancel`, `input.tool_result` |
| Names in common | **none** | |
| Deltas | opt-in per connection (`event_deltas[]`), `event_start` / `event_delta`, **never persisted** | ordinary members of the same event enumeration |

**Neither dialect replays its stream.** This is stated in both references, and it
is what makes serving no stream yet tolerable — the event list is the complete
record either way:

- Anthropic prescribes **consolidation** — open the stream, then fetch
  `GET /v1/sessions/{id}/events`, and dedupe by `event.id`.
- OpenAI prescribes retrieving the session, inspecting `required_actions`, then
  following events.

There is no `Last-Event-ID` resume in either. A client that only holds the stream
is always incomplete, in both dialects.

One place where OpenAI's shape is better: because Anthropic has no replay, its
guide has to warn clients to open the stream *before* sending an event.
`stream: true` on session create removes that race outright.

## What is shared inside, and what is per-façade

| Shared — one implementation | Per-façade — a serializer |
|---|---|
| The step loop: read the log, call the model, run a tool, append | URL layout and path prefix |
| The event log and the session record | Event names and delta vocabulary |
| Turn boundaries and item identity | Status projection |
| The versioned agent, vaults, the environment abstraction | How pending work is addressed |
| Usage and cost | Timestamps: ISO-8601 strings against Unix seconds |
| Budget; later approvals, memory, schedules | `object` discriminators and id prefixes |

**The internal log must be a superset from the first write.** It records turn
boundaries and item identity even though one dialect never exposes them, and both
addressings of a tool call. A log designed to one dialect's shape makes the other
dialect's read projections underivable, and the second façade stops being a
serializer and becomes a storage migration.

## What cannot be expressed

**Approvals have no representation in the OpenAI dialect.** Its
`required_actions` has exactly two variants — `function_call` and
`environment_connection` — both meaning "do this work", not "permit this". A
session paused for human approval has nowhere legal to go. Three options, none
free:

1. approvals are not offered on sessions created through the OpenAI façade;
2. an approval is rendered as a `function_call` — it works, and it lies to the
   client;
3. a variant of our own is added — and their SDKs build that union from `oneOf`
   with a discriminator, so an unknown `type` is likely to raise.

This is a product decision, not a technical one, and it is not taken yet:
approvals are not served in either dialect, so `always_ask` is refused when an
agent is made.

**Session budgets** exist only in the Anthropic dialect; a session made through
the OpenAI one has none.

## Failing loudly

A field we do not support fails the request with a named error. That needs an
explicit list rather than good intentions: each create and update has the fields
it accepts (`src/sessions.py`, `src/agentstore.py`, `src/tools.py`), and anything
else is refused with its name. `agent_toolset_20260401` is refused as a whole —
its `bash`, `read`, `write`, `edit`, `glob` and `grep` need a sandbox
([sandbox-tools.md](sandbox-tools.md)), and `web_fetch` / `web_search` are not
built; an MCP search server does the same job.

## Where these facts come from

**Only one side publishes a machine-readable specification**, and this asymmetry
has a consequence for us.

| | OpenAI | Anthropic |
|---|---|---|
| Spec | OpenAPI 3.1, public, one file, version 2.3.0, 224 paths, Agents API included | none found |
| Available instead | — | the Stainless-generated `api.md` in the SDK repositories; `.stats.yml` reports 202 endpoints |
| Consequence | validation, types and routes can be generated | the schema is ours to author |

So the OpenAI responses are validated against its document, and the Anthropic
ones against the types of the official SDK. Both are moving targets; Anthropic at
least versions its surface with the `managed-agents-2026-04-01` beta header.

Reading the guides was not enough: they showed no reusable agent object and no
vaults for OpenAI, and the specification has both. Facts about either dialect are
taken from the reference, never from memory.

## Open decisions

1. **Approvals on the OpenAI façade** — refuse, misrepresent, or extend. See
   above; nothing about it is comfortable.
2. **Prefix naming.** `/anthropic` and `/openai` are the clearest, and naming a
   compatibility path is not calling the product by someone else's name. Kept.
