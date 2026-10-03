# Operating

How a running deployment is changed, updated, watched and taken down. Two kinds
of change, and the split is the point:

- **Data and keys** change in the running service, at once, with no release.
- **Settings and code** change through a release, which tests itself first.

## Without a release

| Change | How | Takes effect |
|---|---|---|
| A new user | `python3 scripts/keys.py issue --tenant <who> --name <name>`, on the machine that holds the signing key | at once; tokens do not expire |
| Cutting tokens off | `python3 scripts/keys.py revoke-key --kid <kid> --region <region>`: removes a public key, and with it every token it signed | within five minutes — API Gateway caches its answer per token, and the function the key it read |
| Provider keys | nothing to change: each caller sends their own with every request | the installation holds none |
| A model | the agents API — a new agent version, or an override on one session | the next turn; a session pins the version it started with |
| A new model of a provider `aichain` knows | nothing — use its name | at once |
| End users' MCP credentials | the vaults API | the next tool call |
| A session's spending | its `budget` when it is created or updated | before the next model call: a session at its ceiling issues no more |

**A tenant is a token's `--tenant`.** Everything a tenant makes — agents,
sessions, vaults — is keyed by it; two tokens with the same tenant see the same
things, two tenants see nothing of each other.

## Through a release

Every setting in [deploy.md](deploy.md#4-settings-optional) — memory, named model
servers (`YAIT_ENDPOINTS`), the answer ceiling, names — is an Actions variable.
Change it, then run the **Deploy** workflow or push to `main`. The release runs
every suite locally, checks its permissions, applies, and tests the deployment it
made; a release that fails a check before applying changes nothing.

A new **named model server** (`mygpu=https://…`) is one of these: it is checked
when the release starts, and a bad line stops it with the line named.

## Updates

1. **Sync your fork** with the upstream repository — a release tag, not whatever
   `main` holds that day.
2. **The release tests before and after it applies**, as above. There is no canary
   or automatic rollback: to go back, release the previous commit.
3. **The tables need no migration so far.** They are DynamoDB with string keys;
   new fields appear on new items and old items are read as they are. A release
   that needed a migration would say so in its notes.
4. **The model library is pinned** (`yait-aichain` in `src/requirements.txt`).
   Its new models and fixes arrive when a release moves the pin.

## Rotating keys

| Key | How | Mind |
|---|---|---|
| The signing key | `keys.py keygen --kid <new>`; issue new tokens from it (`--kid`, or by default the last key by name); hand them out; then `revoke-key --kid <old>` | old tokens keep working until their key is revoked, so there is no gap |
| The vault key | edit `yait_agents/vault-key` in Secrets Manager: add a second key under `keys` and point `current` at it | new credentials are sealed with the new key; existing ones still open with the old one, so **keep it** until every credential has been rotated through the vaults API |
| The deploy user's access key | make a second key, put it in the fork's secrets, delete the first | the release reads it only while it runs |

The vault key's secret is JSON:
`{"current": "k2", "keys": {"k1": "<base64>", "k2": "<base64>"}}`. Removing a key
that still seals a credential makes that credential unreadable, and the tool calls
that need it fail.

## Watching it

| | Where |
|---|---|
| Every request — method, path, status, tenant, token name | the API function's log, `/aws/lambda/yait_agents_api_functions`, one JSON line each |
| Every worker run — tenant, session, outcome, steps | the worker's log, `/aws/lambda/yait_agents_worker` |
| An MCP server that could not be used | the worker's log, with the whole traceback; the session gets `mcp_connection_failed_error` or `mcp_authentication_failed_error` |
| Cold starts and memory | the `REPORT` lines Lambda writes to both logs |

```bash
aws logs tail /aws/lambda/yait_agents_worker --since 30m --region us-west-1
```

No key, token or credential is ever logged; a token appears by its name.

**The log groups never expire** unless you set a retention — the deploy does not,
since it would need three more permissions. Set one by hand in a long-lived
account.

## Regions

State — sessions, events, agents, vaults — lives in one region, so the region is
chosen at installation.

- **A second region** is a second installation: another fork, or the same fork with
  a second `AWS_REGION` and its own keys. The two share nothing.
- **Moving** is a migration: export and import the three tables, copy the vault
  key, then change every client's `base_url`.
- **Data residency** follows from the region; say so to anyone who must comply.

## Not yet

| | |
|---|---|
| Your own domain | put an API Gateway custom domain or CloudFront in front of the stage, by hand |
| Streamed events | API Gateway buffers responses, so the stream endpoint answers a stub; clients read the event list instead. A streaming function is on the [roadmap](../README.md#roadmap) |
| A console | everything above is a script or an API call |

## Taking it down

```bash
python3 scripts/deploy.py --region us-west-1 --delete
```

removes the functions, their roles and the API. The tables stay — they hold the
event log — and so do the vault key, the data policy and the deploy user: delete
those by hand if you mean to.
