# Benchmarks

How this deployment was compared with the providers' own agent APIs and how
models were compared on it — the method, the task sets, the results, and how to
repeat them. Measured from 29 September to 1 October 2026.

The raw runs are not in this repository: the instrument, the tasks and the test
server are, so anyone can produce their own. [docs/testing.md](testing.md) covers
the other layers of testing — contract, behaviour, reliability — that run on
every deploy.

## The question

1. **Does the same model do as well here as on the provider's agent API?**
   Claude on Anthropic's Managed Agents, GPT on OpenAI's Agents API, against the
   same model through this deployment: same tasks, same instructions, same keys.
2. **What can other models do here, and at what cost?** Any model `aichain`
   reaches, on the same tasks.

## Method

**An arm is a platform and a model**: `anthropic:claude-haiku-4-5-20251001` is
Managed Agents, `openai:gpt-6-luna` the OpenAI Agents API, `ours:<model>` this
deployment. One run gives every arm every task, **three attempts each**, through
the official SDK (Anthropic) or the published HTTP API (OpenAI), each attempt in
a fresh session.

**Scoring is exact where it can be.** Each task names its scorer:

| Scorer | Passes when |
|---|---|
| `json`, `json_exact` | the JSON answer has the expected fields (or exactly them), compared as values — 1284.5 equals "1284.50" |
| `number` | the expected number appears in the answer, and not in the question |
| `regex` | the answer matches |
| `output` | a program's output, line by line |
| `checker` | a rule decides: a schedule that keeps every constraint, a translation that keeps keys and placeholders |

**Gates** must all hold too: the agent answered (stop reason `end_turn`, a
non-empty answer), required patterns appear and forbidden ones do not, and — for
tool tasks — the required tools were called and the needless ones were not.

**Controls run before every run, free:** each scorer is fed the task's known-
right answers (several phrasings where one could fool it) and a known-wrong one,
and a number task is checked not to contain its answer. A run whose controls
fail does not start. A **smoke** — two tasks, every arm, once — must pass before
the full run is paid for.

**Work done outside the answer is checked where it landed.** A task that
publishes a file or sends a message carries a per-attempt nonce; afterwards a
client of our own, with the key of the agent that acted or was written to,
reads the file or the channel. What was published is removed after the check.

**Cost is priced from tokens**, by a dated table (`evals/prices.json`) with
where each price was read — never from a library's — with cache reads and
writes priced apart. **Time** is wall-clock per turn (p50, p90), and the model's
own share from the API's request spans, so the platform's overhead is visible.

**Every run records what it ran under**: the commit, whether the tree was dirty,
library versions, a hash of the task file, the arms and hosts, the prices.

## Task sets

All text-only unless they name tools; every answer key is fixed or computed.

| Set | Tasks | What it holds |
|---|---|---|
| `agents.jsonl` (easy) | 36 | extraction, triage, long documents, summaries, reasoning, multi-turn memory, instruction following, scope |
| `medium.jsonl` | 12 | the same kinds, harder: corrections inside the text, a condition implied rather than stated, rounding at each step, code output, SQL results |
| `hard.jsonl` | 12 | policy rules with precedence, scheduling under constraints, log counting with lookalikes, nested config merge, a stock ledger with corrections, multi-currency invoices, partial refusal, record translation |
| `tools.jsonl` | 12 | MCP tools on three servers: orders and failures on the test server, messaging between two agents on idntty Parley, web search and reading on Exa |
| `multi.jsonl` | 2 | one agent, two or three MCP servers: research → brief → publish; orders → report → publish → message |

**Medium and hard are generated** by `evals/cases/build_sets.py`, which computes
every key from the task's own data, and **calibrated on the providers' own agent
APIs** with Claude Haiku, Claude Opus, GPT-6 Luna and GPT-6 Astra. A task every
calibration model solved was rewritten, since it separates nothing; where the
strongest model alone disagreed with a key, the key was checked first — one was
wrong and corrected.

**The test MCP server** (`tests/mcp_faults/`) answers known data and fails on
purpose: `lookup_order`, `add`, `fail`, `flaky` (fails every other call),
`slow`, `big` (megabytes), and `whoami`, which says which credential a call
carried. It runs behind HTTPS so the original agent APIs can reach it too.

## Results

### 1. The same model, the provider's agent API against ours

Share of attempts solved.

| Model | Easy | Medium | Hard |
|---|---|---|---|
| Claude Haiku 4.5 — Managed Agents / ours | 0.98 / 0.98 | 0.81 / 0.83 | 0.58 / 0.75 |
| Claude Opus 5.5 — Managed Agents / ours | — | 1.00 / 1.00 | 0.92 / 0.92 |
| GPT-6 Luna — OpenAI Agents API / ours | 0.95 / 1.00 | 0.97 / 0.97 | 0.75 / 0.89 |
| GPT-6 Astra — OpenAI Agents API / ours | — | 1.00 / 1.00 | 1.00 / 1.00 |

No model did worse here. Where ours is higher — Haiku and Luna on hard — 36
attempts cannot tell that from chance; what the table supports is that this
deployment costs a model nothing in quality.

### 2. Every model through our API

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

OpenRouter models are named by their host's model id; FP8 means OpenRouter was
pinned to FP8 hosts. Qwen3.8 Max, DeepSeek V4 Pro, Opus and Astra were not run on
the easy set.

### 3. Tools — 12 tasks on three MCP servers

| Platform and model | Solved | p50 / p90 | $ per solved |
|---|---|---|---|
| Claude Haiku 4.5, Managed Agents | 36/36 | 6.3 / 9.5 s | $0.0139 |
| Claude Haiku 4.5, ours | 35/36 | 8.1 / 17.9 s | $0.0113 |
| GPT-6 Luna, OpenAI Agents API | 23/36 | 16.6 / 23.2 s | not reported |
| GPT-6 Luna, ours | 36/36 | 9.4 / 18.6 s | $0.0005 |
| gpt-oss-120b, ours | 36/36 | 16 / 27 s | $0.0003 |
| GLM-5.3-Flash (FP8), ours | 36/36 | 16 / 32 s | $0.0006 |
| GLM-5.3-Flash, ours | 36/36 | 10 / 28 s | $0.0011 |
| Qwen3.8 Flash, ours | 36/36 | 11 / 29 s | $0.0018 |
| Gemma 4 26B, ours | 35/36 | 12 / 23 s | $0.0006 |
| DeepSeek Flash, ours | 33/36 | 8 / 17 s | $0.0028 |
| gpt-oss-20b, ours | 28/36 | 12 / 31 s | $0.0003 |
| Llama 4 Maverick, ours | 24/36 | 8 / 14 s | $0.0016 |

On OpenAI's own API, Luna often called OpenAI's helper functions
`list_mcp_resources` and `list_mcp_resource_templates` instead of the server's
tools, then said the tool was unavailable; given the same server's tools
directly, it solved all 36. The weak cheap models fail the same two ways: an
empty answer after a tool result, and a tool called where none was needed.

### 4. Several MCP servers in one agent

| Platform and model | Research → publish | Orders → publish → message | $ per attempt | p50 |
|---|---|---|---|---|
| Claude Haiku 4.5, Managed Agents | 1/3 | 3/3 | $0.0146 | 24 s |
| Claude Haiku 4.5, ours | 7/8 attempts across both | | $0.0156 | 34 s |
| GPT-6 Luna, OpenAI Agents API | 3/3 | 2/3 | not reported | 39 s |
| GPT-6 Luna, ours | 8/8 attempts across both | | $0.0019 | 28 s |
| GLM-5.3-Flash (both), Qwen3.8 Flash, DeepSeek Flash, Gemma 4, ours | 3/3 each | 3/3 each | $0.002–0.009 per solved | 37–60 s |
| gpt-oss-120b, ours | 3/3 | 2/3 | $0.0013 per solved | 67 s |
| gpt-oss-20b, Llama 4 Maverick, ours | 1/3 | 0/3 | — | 45–59 s |

Haiku's misses on research were the same on both platforms: a brief published
without the figure it was asked to find.

### 5. The platform

| | |
|---|---|
| Warm turn, three tool calls | 8.8 s, of which the model about 5; a tool call about 0.5 s; between steps about 0.3 s |
| Cold start of the step worker | 15–20 s at 256 MB; the worker now has 1 GB |
| Prompt cache, Haiku, multi-server turn | fresh input 13,106 → 1,807 tokens a turn once the cache mark moved to the step's last block; Managed Agents sends about 26 |
| Lambda per step | about $0.0001 at 1 GB, against $0.002–0.03 for the model call |

## What the measurements found

Each of these was a defect the benchmarks surfaced, fixed and re-measured:

| Found | By | Fixed |
|---|---|---|
| Claude answered without thinking here, with it on Managed Agents: Haiku 0.33 against 0.58 on hard | parity on hard | thinking on, as the original |
| Reasoning models cut off at the library's 4,096-token default, answering nothing | empty answers on hard | a 32,768 ceiling, and a cut-off answer said as one |
| Every MCP server failed in Lambda only | the first tool run in the cloud | the package kept the libraries' metadata |
| gpt-6 could not call tools | tools through ours | gpt-6 through the Responses API |
| A 1 MB tool result was stored and sent whole | the fault server | results cut at 64 KB |
| The prompt cache missed on tool turns | token counts against the original | the mark on the step's last block |
| Cold starts dominated tool turns | event timestamps | 1 GB for the step worker |

## Caveats

- **Three attempts per task**: 36 attempts per set. A difference of one or two is
  noise; the tables are for orders of magnitude and for failures that repeat.
- **Models and prices move.** Every result is dated; prices are list prices read
  on the dates in `evals/prices.json`.
- **OpenAI's agent API reported no usage** on turns with MCP calls, so its cost
  is missing, not zero.
- **GPT-6 Luna's cost through ours is understated** in the text runs: they ran
  before the Responses API's cached tokens were read.
- **Reasoning models' hard results** are from after the answer ceiling was raised;
  before it they were cut off and scored lower.
- **Kimi K2.6** lost hard attempts to the instrument's five-minute turn limit.
- **The easy set barely separates models**; medium and hard exist for that.

## How to repeat

```bash
python3 -m venv .venv && .venv/bin/pip install -r src/requirements.txt -r tests/requirements.txt

# Provider keys, the deployment's token and the MCP credentials, from env files
# (.env here, or ../aichain/.env): ANTHROPIC_API_KEY, OPENAI_API_KEY, … ,
# YAIT_CLIENT_KEY, MCP_FAULTS_URL, MCP_FAULTS_TOKEN_A/B, IDNTTY_KEY_A/B, EXA_API_KEY

# the original agent APIs
.venv/bin/python evals/quality.py --cases evals/cases/hard.jsonl \
  --arms anthropic:claude-haiku-4-5-20251001,openai:gpt-6-luna --trials 3 --run --name baseline

# the same models, and others, through your deployment
.venv/bin/python evals/quality.py --cases evals/cases/hard.jsonl \
  --base-url https://<service-url> \
  --arms ours:claude-haiku-4-5-20251001,ours:gpt-6-luna,ours:deepseek-flash --trials 3 --run --name ours
```

The first arm is the baseline the others are compared with. A run writes
`evals/results/<date>-<name>/`: every attempt (`ledger.jsonl`), what it ran under
(`passport.json`) and the report (`report.md`), on your machine — the repository
ignores `evals/results/`, so our own runs are not in it; the tables above are
their summary. `--rescore <dir>` re-scores a run without calling the models again. The test MCP server deploys anywhere with
`docker compose up -d` in `tests/mcp_faults/`, given a host name and tokens.
