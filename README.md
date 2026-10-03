# agents

**The API of Claude Managed Agents and of OpenAI's Agents API, on any model, in
your own AWS account.**

Anthropic's Managed Agents and OpenAI's Agents API run an agent loop on the
provider's side: server-side sessions, an event log, tools, credentials. Each
binds that loop to its own models and keeps the data on its own infrastructure.
`agents` is the same kind of service, deployed from this repository into your AWS
account, running on any model the [`aichain`](https://github.com/yaitio/aichain)
library supports — the major cloud providers, OpenRouter, and any
OpenAI-compatible server you host.

- **The official SDKs work, both of them.** Change `base_url` and add one header.
  The Anthropic SDK talks to `https://<host>/anthropic`, the OpenAI one to
  `https://<host>/openai/v1`, both over one service.
- **Any model, and that is where the money is.** Change the model name; the agent,
  its tools and its sessions stay. On our tool tasks, open models that solved
  every one cost 8 to 55 times less per solved task than Claude Haiku through
  Managed Agents ([results](#results)).
- **Your account, your data.** Sessions, agents and credentials live in your AWS
  account. You pay AWS for the infrastructure and each model provider directly,
  with your own key: the installation never pays for tokens.
- **Measured against the originals.** Every response is checked against the
  official SDK's types and OpenAI's published OpenAPI document, on every deploy;
  the same model on the same tasks scores no worse here than on the provider's
  own agent API.

## What works

| | |
|---|---|
| Agents and sessions | create, version, update, archive, delete, in both dialects |
| Events | the session's event log, turns and items as each dialect shows them; interrupt |
| Models | any `aichain` model, named servers (`openrouter/…`, your `mygpu/…`); Claude thinks as on Managed Agents; prompt caching |
| Tools | **MCP servers**, several per agent, with the session's credentials |
| Vaults | MCP credentials (`static_bearer`), sealed with AES-256-GCM, never returned |
| Access | a JWT per user (`X-Yait-Key`), checked by API Gateway before the function runs |
| Budget | a money ceiling per session |

Not yet: client-side tools, approvals, a sandbox, OAuth refresh for MCP — see the
[roadmap](#roadmap). Anything a request asks for that is not served is refused
by name, never ignored.

## Quick start

You need an AWS account, a fork of this repository, and Python 3.12.

1. **Set up the AWS account once**, with credentials that may manage IAM: the
   deploy user and the policies it may attach
   ([docs/deploy.md](docs/deploy.md#2-set-up-with-credentials-that-may-manage-iam)).
2. **Put the deploy user's credentials in your fork** as Actions secrets
   (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`), and the region as the
   Actions variable `AWS_REGION`.
3. **Deploy**: push to `main`. The workflow checks the permissions, prints the
   plan, applies it, prints the service URL, and runs the contract suite against
   the deployment.
4. **Make the installation's keys**, on your machine:

   ```bash
   python3 scripts/keys.py keygen --region us-west-1     # the signing key; the public half goes to AWS
   python3 scripts/keys.py vault-key --region us-west-1  # the key vault secrets are sealed with
   python3 scripts/keys.py issue --tenant acme --name alice   # a token for one user
   ```

   Issue one more, `--name ci`, and add it to the fork as the Actions secret
   `YAIT_CLIENT_KEY`: from then on every release also tests itself against the
   deployment it made.

5. **Use it** with the official SDK — your provider key stays where it was:

   ```python
   from anthropic import Anthropic

   client = Anthropic(base_url="https://<service-url>/anthropic",
                      api_key="<your Anthropic key>",
                      default_headers={"X-Yait-Key": "<token from step 4>"})
   agent = client.beta.agents.create(model="claude-haiku-4-5-20251001", name="hello")
   ```

   Any other model takes the same call with its own provider's key — for
   example `model="openrouter/z-ai/glm-5.3-flash"` with an OpenRouter key.

## Documentation

| | |
|---|---|
| [docs/deploy.md](docs/deploy.md) | from a fork to a running service, and what ends up in your account |
| [docs/operations.md](docs/operations.md) | users and tokens, model servers, limits, regions, domains, updates |
| [docs/api.md](docs/api.md) | the two dialects: where they agree, where they collide |
| [docs/architecture.md](docs/architecture.md) | the step loop, the event log, ordering, isolation, failure |
| [docs/economics.md](docs/economics.md) | what a task costs here, and where the money goes |
| [docs/testing.md](docs/testing.md) | the four layers of tests, and how to run them |
| [docs/benchmarks.md](docs/benchmarks.md) | how the models were compared, and the results in full |

## Results

Measured from 29 September to 1 October 2026; 3 attempts per task. Method, task
sets, every table and how to repeat them: [docs/benchmarks.md](docs/benchmarks.md).

**The same model, the provider's agent API against ours** (share of tasks solved):

| | medium | hard |
|---|---|---|
| Claude Opus 5.5 — Managed Agents / ours | 1.00 / 1.00 | 0.92 / 0.92 |
| GPT-6 Astra — OpenAI Agents API / ours | 1.00 / 1.00 | 1.00 / 1.00 |
| GPT-6 Luna — OpenAI Agents API / ours | 0.97 / 0.97 | 0.75 / 0.89 |
| Claude Haiku 4.5 — Managed Agents / ours | 0.81 / 0.83 | 0.58 / 0.75 |

**Every model we ran through our API** — the same tasks: easy (36), medium (12)
and hard (12), share solved, and what a solved hard task cost:

| Model | Easy | Medium | Hard | $ per solved (hard) |
|---|---|---|---|---|
| GPT-6 Astra | — | 1.00 | 1.00 | $0.0251 |
| Qwen3.8 Max | — | 0.97 | 0.97 | $0.0195 |
| DeepSeek Flash | 0.98 | 0.97 | 0.97 | $0.0030 |
| Qwen3.8 Flash | 1.00 | 0.92 | 0.97 | $0.0014 |
| Qwen3.8 27B (FP8) | 1.00 | 0.92 | 0.97 | $0.0060 |
| DeepSeek V4 Pro | — | 0.94 | 0.94 | $0.0110 |
| GLM-5.3-Flash | 1.00 | 0.97 | 0.94 | $0.0013 |
| Claude Opus 5.5 | — | 1.00 | 0.92 | $0.0169 |
| Gemini 3.8 Flash | 0.97 | 1.00 | 0.92 | $0.0084 |
| GPT-6 Luna | 1.00 | 0.97 | 0.89 | $0.0004 |
| GLM-5.3-Flash (FP8) | 1.00 | 0.97 | 0.89 | $0.0007 |
| Grok 4.3 | 0.94 | 0.89 | 0.81 | $0.0029 |
| Muse Glimmer 30B | 0.96 | 0.92 | 0.78 | $0.0041 |
| Claude Haiku 4.5 | 0.98 | 0.83 | 0.75 | $0.0174 |
| gpt-oss-120b | 0.93 | 0.86 | 0.72 | $0.0004 |
| Kimi K2.6 (FP8) | 1.00 | 0.97 | 0.69 | $0.0118 |
| gpt-oss-20b | 0.93 | 0.83 | 0.56 | $0.0006 |
| Gemma 4 26B | 1.00 | 0.67 | 0.28 | $0.0009 |
| Llama 4 Maverick | 0.98 | 0.44 | 0.19 | $0.0021 |

The easy set separates models little — most solve nearly all of it — which is
why the medium and hard sets were written and calibrated on the providers' own
agent APIs.

**Tools: 12 tasks on three MCP servers** (orders, agent messaging, web search):

| | solved | cost per solved task |
|---|---|---|
| Claude Haiku 4.5, Managed Agents | 36/36 | $0.0139 |
| Claude Haiku 4.5, ours | 35/36 | $0.0113 |
| GPT-6 Luna, OpenAI Agents API | 23/36 | not reported |
| GPT-6 Luna, ours | 36/36 | $0.0005 |
| gpt-oss-120b, ours | 36/36 | $0.0003 |
| GLM-5.3-Flash (FP8), ours | 36/36 | $0.0006 |
| Qwen3.8 Flash, ours | 36/36 | $0.0018 |

**The platform itself**: a warm turn of three tool calls takes 8.8 s, of which
the model is about 5; a step costs about $0.0001 of Lambda against $0.002–0.03 for
the model call it waits for.

## Roadmap

**Next**

1. OAuth for MCP: refreshing `mcp_oauth` credentials in the vault.
2. A managed sandbox: a container per session with `bash`, `read`, `write`,
   `edit`, `glob` and `grep` ([specification](docs/sandbox-tools.md)).

**Then**

3. Client-side tools: Anthropic's `custom`, OpenAI's `function`.
4. Approvals before a call (`always_ask`).
5. Effort: how long the model thinks, set per agent.
6. Streamed events through a function of their own, past API Gateway's limits.
7. The answer ceiling fitted to the worker's time limit.

## Licence

[Apache License 2.0](LICENSE). A distribution of this code, or of a work derived
from it, keeps the [NOTICE](NOTICE) file: it says what the work is built on. The
licence grants no rights to the name "yait".
