# Plugins and placement — draft

How anything the agent can use is packaged, installed, granted and run. Shared by
`agents` and `assistant`: the two products differ in which plugins are installed
and in the interface around them, not in how plugins work.

Everything here is a proposal until code exists. This revision follows the
specification's plugin section. The earlier draft — seven plugin kinds and a
dependency graph between plugins — is superseded; what was dropped, and where each
dropped thing went, is recorded under [What this replaces](#what-this-replaces).

## A plugin is an MCP server

> **A plugin is an MCP server. Nothing else.** The agent sees tools; how the
> server got there is not the agent's business.

There is no plugin kind for code that runs inside our process. The reason is
portability before security: an in-process module cannot be moved from a laptop to
Lambda without rewriting, and the product's whole promise is that it can.

So there is no `kind` field. A plugin varies along two axes, and both belong to
the runner rather than to the agent:

| Axis | Values |
|---|---|
| Where the server comes from | an image we run, or an endpoint someone else runs |
| How long one lives | `per-call`, `session` |

`service` is a neighbour, not a plugin: something that runs whether or not an
agent exists is deployed beside us and reached as a remote endpoint.

## What travels

1. **Envelope** — an OCI image, referenced by **digest**, never by tag. A remote
   plugin has no envelope; it has a URL.
2. **Protocol** — MCP. stdio locally, streamable HTTP remotely. One protocol, two
   transports; the agent does not know which.
3. **Deployment descriptor** — what the container does not say about itself. Read
   by the runner, **never by the agent**.

```yaml
name: pdf.extract
image: ghcr.io/yaitio/plugin-pdf@sha256:…   # or: endpoint: https://…
provides:
  - capability: document.extract
    kind: tool
    risk: read                  # the library's risk vocabulary
requires:
  workspace: rw                 # a contract on data, never a path
  network: []                   # empty means the runner gives it no network
  secrets: []                   # by name; values resolved per request
  resources: { cpu: 1, memory: 512Mi, gpu: none }
lifecycle: per-call
latency_budget: 5s
health: /healthz
```

Unknown descriptor keys fail validation. A silently ignored key is a permission
nobody reviewed.

## Two forms, one definition

Both forms are MCP servers and reach the agent as ordinary tools. They differ in
what we can promise about them, and the difference is shown at install rather than
smoothed over.

| | **Hosted** (`image:`) | **Remote** (`endpoint:`) |
|---|---|---|
| Integrity | the digest covers the code | none — a URL is whatever it serves today |
| `network: []` | enforced by the runner | not applicable: the call is egress to that host, and the allowlist is exactly that host |
| `workspace`, `resources`, `lifecycle`, `health` | apply | do not apply; the provider's uptime is the health |
| Credentials | `secrets` by name, injected into MCP headers | the same, and this is the usual case — OAuth tokens from the vault |
| Data leaves the account | no | yes, to that provider — stated at install |

The remote form is not a concession. Every SaaS MCP server becomes a plugin by
writing a descriptor for it, and version one ships the vault and OAuth refresh
precisely for this case.

## Three fields that are contracts, not hints

- **`workspace`** — locally a bind mount, in the cloud a volume or an S3 prefix
  under the run's key. The plugin never receives a host path, so the same image
  runs in both places unchanged.
- **`secrets`** — names only. The orchestrator resolves them per request and
  injects them into MCP headers, never into the model's context and never into the
  run document. This mirrors the library's `Model(api_key=callable)` over
  `RunContext`.
- **`network`** — for a hosted plugin this is enforced by the runner, not trusted.
  This is the one declaration where a dishonest descriptor must not be able to
  lie.

## Risk

A tool's risk class decides whether it runs, asks or is refused, so it may never
be inferred by accident.

1. The descriptor's `risk` is authoritative: it is what the owner read and granted
   at install.
2. Where the descriptor is silent, MCP tool annotations
   (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) map to
   the library's classes.
3. **A tool with neither is refused at install**, naming the tool.

The third rule exists because of what the library does today:
`tools/_permissions.py` reads `getattr(tool, "risk", WRITE)`, MCP discovery never
sets that attribute, and `write` is `allow` in the default policy. A delete tool
from a third-party server would therefore be called without a question. Mapping
the annotations is a small library fix and blocks any honest permission story for
third-party plugins; refusing an unclassified tool is what keeps the hole shut
afterwards.

## Capability broker

One door. The broker resolves descriptor → placement → transport → session and
hands the agent ordinary tools.

```
Catalogue ─ descriptors, digests, conformance results
    │ install
    ▼
Installer ─ resolves the descriptor, computes placement, shows what the plugin
    │       will be able to do, records the grant
    ▼
Plugin host ─ starts runtimes on runners
    │
    ▼
Capability broker ─ the only door between the agent and plugins
    │   ├─ policy: the grant's scope; risk class → allow / ask / deny
    │   ├─ vault: secrets resolved per request into MCP headers
    │   ├─ egress: network only to the descriptor's allowlist
    │   └─ audit: every call, its input, result, cost and placement
    ▼
Agent runtime (aichain Agent) ─ sees only granted capabilities
```

**A plugin never calls another plugin.** Composition happens above them: the agent
holds tools from several plugins and passes data between them, or the data passes
through the workspace. This is the part that changed most from the earlier draft —
see below.

## Placement

Anyone can write a plugin, so where it runs cannot be left to its author or to a
guess.

```
placement = descriptor needs ∩ installation runners ∩ trust policy ∩ data locality
```

Runners in version one:

| Runner | Longest call | Lifecycle | Data lives | For |
|---|---|---|---|---|
| `container` (local Docker) | any | per-call, session | the laptop | every hosted plugin, locally |
| `container` (cloud, per-call) | 15 min | per-call | the account | the default for hosted plugins |
| `container` (cloud, session) | hours | session | the account | a plugin that must keep state between calls |
| `remote` | not ours | not ours | the provider | every `endpoint:` plugin — nothing to start |

`gpu` and `device` runners are later, and `device` is a component of its own.

Several runners may fit; the installation's preference order picks one, and the
choice is recorded with every call in the audit log. The result is shown at
install and is never silent — a plugin with nowhere to run is refused with the
reason rather than installed and left broken.

## Conformance is measured, not claimed

A published suite a plugin passes to be listed: the capability list is stable,
schemas are valid, the declared network is respected, `health` answers, a
`per-call` lifecycle survives a cold start, and **parity holds between local and
cloud** — the same image digest and the same descriptor expose the same capability
list and pass the same suite in both places, differing only in latency and in
where the workspace comes from.

That parity is one of the two tests the product is defined by. A plugin that
passes locally and fails in the cloud is a product failure, not a plugin failure.

```
pdf-tools 1.2.0   ✓ container (local)   ✓ container (cloud)   ✗ session (no state kept)
```

## Version one

Remote plugins and hosted plugins on a per-call container: the broker, grants,
vault, audit, and `network: []` enforced. One of our own MCP servers (`folio` or
`statiq`) shipped as an image with a descriptor is the milestone that proves it.

The `session` lifecycle follows; `gpu` and `device` after that.

Note that a per-call container running an image we published is not the bash
sandbox, which remains out of scope: arbitrary code the agent writes at runtime is
a different problem from a signed image the owner installed.

## What this replaces

The earlier draft had seven plugin kinds — `skill`, `mcp`, `app`, `connector`,
`trigger`, `ui` — and let plugins declare dependencies on each other's
capabilities. Both are gone. Where each part went:

| Dropped | Where it went |
|---|---|
| `kind: skill` | a YAML skill is a library artifact, exposed to the agent as tools. It was never packaged, granted or placed, so calling it a plugin only blurred the word |
| `kind: app` | a hosted plugin. Any code, in an image, speaking MCP |
| `kind: connector` | a channel adapter in the product, not a plugin. Telegram and email are the product's inbound and outbound edges |
| `kind: trigger` | a trigger adapter in the product: cron, webhook, queue — see the specification's trigger section |
| `kind: ui` | the host renders; a plugin contributes content with a declared media type. A settings or approval interface is a consumer of the event log, never a plugin kind |
| `requires: [capability]` between plugins | composition at the agent's tool list and through the workspace. The resolver, the provider graph and the substitution of one provider for another are all gone with it |
| `config:` and generated settings screens | deployment configuration and `secrets` by name. Per-plugin settings interfaces become the host's work — which lands on `assistant`, where they are visible product surface |
| Trust labels (official / verified / community) | mechanics rather than labels: a digest, a conformance result, an enforced `network`, and code from a registry that never runs in our process |
| Plugins written by an agent in a session | later, and it now needs an image built in a runner. The path from "the agent wrote a tool" to "the tool is installed" is no longer one step |

The two deliberate losses worth saying plainly: **a plugin can no longer be
swapped for another provider of the same capability by the installer**, and
**agent-authored plugins are no longer a version-one story**. Both bought the same
thing — a plugin that moves between a laptop and Lambda without being rewritten.

## Where each part lives

| Part | Repository |
|---|---|
| Capability names, the risk vocabulary, MCP clients, a loader that runs plugins locally | `aichain` |
| Plugin host, broker, runners, placement, the descriptor's schema | `agents` |
| Catalogue and its conformance CI | a separate repository |
| Settings and approval interfaces | `assistant`, and the `agents` console |

## Open decisions

1. **Where the descriptor lives** — OCI labels on the image, so the digest covers
   the descriptor and the image is self-sufficient, or an index beside the
   registry, which is easier to search and is a second source of truth.
   Recommended: labels, with an index used only for discovery.
2. **Provenance for hosted plugins.** A digest gives integrity, not origin.
   Signing (cosign or the like) is unspecified, and without it "code from a known
   publisher" has no mechanism behind it.
3. **Provenance for remote plugins** has no mechanism at all: a URL and a vault
   credential is everything we know. Whether that is stated and accepted, or
   restricted to an allowlist the owner maintains, is a policy decision.
4. **Capability vocabulary.** A small standard set for common domains — mail,
   calendar, files, spreadsheets, chat, documents — and free names for the rest.
   With the dependency graph gone, these names are now labels for audit and
   grants rather than a resolution key, which lowers the cost of getting them
   wrong.
5. **Default `lifecycle`** — recommended `per-call`: it costs the least in
   placement and matches the library's synchronous, connection-per-call MCP
   client.
6. **Who runs the catalogue and its conformance CI** — centrally, or each
   publisher with a signature.
7. **Catalogue repository name** — `plugins`, or together with `skills`.
