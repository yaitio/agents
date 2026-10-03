#!/usr/bin/env python3
"""
Every name the deploy uses, resolved in one place.

Each one has a default and an environment variable that overrides it. In GitHub
they are Actions *variables*, not secrets — none of them is sensitive, and the
workflow passes them straight through.

`YAIT_PREFIX` moves all four names at once, which is what most people want. The
individual overrides exist for the case where a name is already taken, or where a
naming convention is not negotiable.

    python3 scripts/names.py             # what will be used
    python3 scripts/names.py --policy    # the IAM policy for exactly these names

**The policy is generated from these values, not written beside them.** A renamed
function with a policy that still names the old one is an AccessDenied halfway
through a deploy, and the check cannot warn about it because the check asks IAM
about actions, not names.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_TEMPLATE = ROOT / "infra" / "deploy-policy.json"


def _env(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


# The one knob that moves everything.
PREFIX = _env("YAIT_PREFIX", "yait_agents")

#: The IAM role the function runs as, and the path it sits on. The path is what
#: the deploy policy scopes role creation to, so the two must agree.
ROLE_PATH = _env("YAIT_ROLE_PATH", f"/{PREFIX}/")
ROLE_NAME = _env("YAIT_ROLE_NAME", f"{PREFIX}_api_role")

FUNCTION = _env("YAIT_FUNCTION_NAME", f"{PREFIX}_api_functions")

#: The step worker: a second function, with a role of its own. The API function
#: validates, routes and answers; this one takes steps. When provider keys arrive
#: they go to this role and never to the API's — two functions so that code the agent
#: causes to run never shares a role with the keys.
WORKER_FUNCTION = _env("YAIT_WORKER_FUNCTION", f"{PREFIX}_worker")
WORKER_ROLE_NAME = _env("YAIT_WORKER_ROLE_NAME", f"{PREFIX}_worker_role")
#: The longest single invocation. Lambda bills only the time used, so the ceiling
#: costs nothing until a step needs it — and a real model on a hard task can think
#: for minutes.
WORKER_TIMEOUT_S = int(_env("YAIT_WORKER_TIMEOUT_S", "900"))

#: Stop starting steps with less than this left. It has to exceed the longest single
#: model call, or a step starts that the timeout will kill, leaving a turn half done.
WORKER_RESERVE_S = int(_env("YAIT_WORKER_RESERVE_S", "300"))

#: `aichain` in a deployment; `echo` is what a local run gets when nothing is set,
#: so the suites stay deterministic and free.
MODEL_BACKEND = _env("YAIT_MODEL_BACKEND", "aichain")


#: The REST API's name. API Gateway takes it from the imported document's
#: `info.title`, so the deploy writes this value into the AWS variant of the
#: document rather than renaming the API afterwards — a rename does not survive
#: the next release, because an overwrite replaces the name along with everything
#: else.
API_NAME = _env("YAIT_API_NAME", f"{PREFIX}_api")

#: The stage becomes the first path segment of the invoke URL, so it is part of
#: every client's base_url until a custom domain removes it.
STAGE = _env("STAGE_NAME", "prod")

RUNTIME = _env("YAIT_RUNTIME", "python3.12")
ARCHITECTURE = _env("YAIT_ARCHITECTURE", "arm64")

#: Small on purpose: a step spends most of its wall-clock blocked on the network,
#: and Lambda bills memory x wall-clock. See docs/economics.md.
MEMORY_MB = int(_env("YAIT_MEMORY_MB", "256"))
#: The step worker's, apart: it imports the model and MCP libraries, and Lambda
#: gives CPU in proportion to memory. At 256 MB a cold worker took 15–20 s to
#: start and used 200 MB of it (measured 2026-10-01); see docs/economics.md.
WORKER_MEMORY_MB = int(_env("YAIT_WORKER_MEMORY_MB", "1024"))
TIMEOUT_S = int(_env("YAIT_TIMEOUT_S", "10"))

#: The two tables of slice one. The session item is small and read on every step;
#: the event log is append-only and is the only source of truth for what happened.
SESSIONS_TABLE = _env("YAIT_SESSIONS_TABLE", f"{PREFIX}_sessions")
EVENTS_TABLE = _env("YAIT_EVENTS_TABLE", f"{PREFIX}_events")
AGENTS_TABLE = _env("YAIT_AGENTS_TABLE", f"{PREFIX}_agents")

#: The provider keys, as one JSON secret whose keys are the library's own env_key
#: names — so the mapping is identity and there is no table to drift. Created once
#: by the account owner: the deploy may read it and may not write it.

#: The client keys: `{key: {"tenant": ..., "id": ...}}`. One tenant per deployment
#: in version one, but the map shape is the multi-tenant one from the start, so
#: later is an entry rather than a migration.
VAULT_KEY_SECRET = _env("YAIT_VAULT_KEY_SECRET", f"{PREFIX}/vault-key")

#: The customer-managed policy giving the function its data. Created once by the
#: owner from infra/function-policy.json, because the deploy may attach exactly this
#: and the AWS logs policy and nothing else.
DATA_POLICY = _env("YAIT_DATA_POLICY", f"{PREFIX}_data")

STATEMENT_ID = f"{PREFIX}_apigw"

VARIABLES = [
    ("YAIT_PREFIX",        PREFIX,       "moves all four names at once"),
    ("YAIT_ROLE_PATH",     ROLE_PATH,    "the deploy policy scopes role creation to this"),
    ("YAIT_ROLE_NAME",     ROLE_NAME,    "the role the function runs as"),
    ("YAIT_FUNCTION_NAME", FUNCTION,     "the Lambda function"),
    ("YAIT_API_NAME",      API_NAME,     "the REST API, via the document's title"),
    ("YAIT_WORKER_FUNCTION", WORKER_FUNCTION, "the step worker"),
    ("YAIT_WORKER_ROLE_NAME", WORKER_ROLE_NAME, "the worker's own role"),
    ("YAIT_WORKER_TIMEOUT_S", WORKER_TIMEOUT_S, "the longest single invocation"),
    ("YAIT_MODEL_BACKEND", MODEL_BACKEND, "aichain, or echo for a free dry run"),
    ("YAIT_WORKER_RESERVE_S", WORKER_RESERVE_S, "no new step with less time left"),
    ("STAGE_NAME",         STAGE,        "first path segment of every base_url"),
    ("YAIT_RUNTIME",       RUNTIME,      "Lambda runtime"),
    ("YAIT_ARCHITECTURE",  ARCHITECTURE, "arm64 or x86_64"),
    ("YAIT_MEMORY_MB",     MEMORY_MB,    "memory, which Lambda bills per wall-clock second"),
    ("YAIT_WORKER_MEMORY_MB", WORKER_MEMORY_MB, "the step worker's memory, which buys its cold-start CPU"),
    ("YAIT_TIMEOUT_S",     TIMEOUT_S,    "per-request timeout"),
    ("YAIT_SESSIONS_TABLE", SESSIONS_TABLE, "cursor, lease, status, counters"),
    ("YAIT_EVENTS_TABLE",  EVENTS_TABLE, "the append-only log"),
    ("YAIT_AGENTS_TABLE",  AGENTS_TABLE, "agent definitions and their versions"),
    ("YAIT_VAULT_KEY_SECRET", VAULT_KEY_SECRET, "the key vault secrets are sealed with"),
    ("YAIT_DATA_POLICY",   DATA_POLICY,  "the function's data policy, owner-created"),
]


def policy(region: str = "*") -> dict:
    """The deploy policy with these names and this region substituted in."""
    text = POLICY_TEMPLATE.read_text()
    doc = json.loads(text)
    doc.pop("Comment", None)
    for statement in doc["Statement"]:
        statement.pop("Comment", None)

    def substitute(value):
        if isinstance(value, str):
            return (value
                    .replace("WORKER_FUNCTION", WORKER_FUNCTION)
                    .replace("REGION", region)
                    .replace("ROLE_PATH", ROLE_PATH.strip("/"))
                    .replace("FUNCTION_NAME", FUNCTION)
                    .replace("SESSIONS_TABLE", SESSIONS_TABLE)
                    .replace("EVENTS_TABLE", EVENTS_TABLE)
                    .replace("AGENTS_TABLE", AGENTS_TABLE)
                    .replace("VAULT_KEY_SECRET", VAULT_KEY_SECRET)
                    .replace("DATA_POLICY", DATA_POLICY)
                    .replace("PREFIX", PREFIX))
        if isinstance(value, list):
            return [substitute(v) for v in value]
        if isinstance(value, dict):
            return {k: substitute(v) for k, v in value.items()}
        return value

    return substitute(doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", action="store_true",
                    help="print the IAM policy for these names")
    ap.add_argument("--region", default="*")
    a = ap.parse_args()

    if a.policy:
        print(json.dumps(policy(a.region), indent=2))
        return 0

    width = max(len(name) for name, _, _ in VARIABLES)
    print(f"{'variable':<{width}}  {'value':<22}  set by\n")
    for name, value, why in VARIABLES:
        source = "environment" if os.environ.get(name, "").strip() else "default"
        print(f"{name:<{width}}  {str(value):<22}  {source:<11}  {why}")
    print(f"""
The log group is created by Lambda itself, at /aws/lambda/{FUNCTION}, and has no
expiry — see docs/deploy.md.

  python3 scripts/names.py --policy --region <region>   the matching IAM policy""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
