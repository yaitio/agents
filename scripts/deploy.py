#!/usr/bin/env python3
"""
The deploy. Three resources, created directly — no CloudFormation anywhere.

This runs in GitHub Actions, from a fork, with credentials held as Actions
secrets. It also runs from a shell, which is how it is developed; but a release
is the workflow's, because the release path is the product.

CloudFormation is deliberately absent. It orchestrates, and orchestration is not
what an API needs — so it must not become a permission prerequisite. What is
given up with it is a change-set preview and a stack that records what exists;
`--plan` covers the first, and the names below are the whole of the second.

What an API actually needs, and nothing more:

1. an IAM role for the function — logs and nothing else;
2. the function itself, from the contents of ``src/``;
3. a REST API imported from the OpenAPI document, a permission for it to invoke
   the function, and a stage.

Every step is idempotent: run it again and it updates rather than duplicates.

    python3 scripts/deploy.py --region us-west-1
    python3 scripts/deploy.py --region us-west-1 --plan
    python3 scripts/deploy.py --region us-west-1 --delete

Credentials come from the ambient AWS configuration. Nothing is read from a file
here and nothing is written into one. The first thing this does is check that the
credentials carry every permission it will need, because a half-permitted deploy
fails in the middle, having already created some of the resources.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import endpoints  # noqa: E402 — the one parser, shared with the worker
sys.path.insert(0, str(ROOT / "openapi"))
sys.path.insert(0, str(ROOT / "scripts"))
import build as B  # noqa: E402

import names as N

NAME = N.PREFIX
ROLE_NAME = N.ROLE_NAME
ROLE_PATH = N.ROLE_PATH
FUNCTION = N.FUNCTION
RUNTIME = N.RUNTIME
STAGE = N.STAGE



def aws(*args: str, region: str | None = None, check: bool = True) -> dict | str:
    cmd = ["aws", *args, "--output", "json"]
    if region:
        cmd += ["--region", region]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        if check:
            raise SystemExit(f"$ {' '.join(args)}\n{p.stderr.strip()}")
        return {"__error__": p.stderr.strip()}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return out


def step(msg: str) -> None:
    # Flushed, because the permission check writes to the same descriptor from a
    # subprocess and an unflushed header lands after the output it heads.
    print(f"\n── {msg}", flush=True)


# ── 1. the role ────────────────────────────────────────────────────────────

BASIC_EXECUTION = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


#: IAM rejects a description outside [\t\n\r\x20-\x7E\xA1-\xFF], so an em dash —
#: which this repository's prose uses everywhere — fails validation. Anything sent
#: to an AWS API stays ASCII.
ASCII_DESCRIPTION = "yait agents function role: logs and its data policy, nothing else"


def _environment(extra: dict[str, str] | None = None) -> tuple[str, str]:
    """
    Where a function finds its tables, secrets and neighbours.

    Names only. Not one secret *value* is here: `GetFunctionConfiguration` shows a
    function's environment in plain text to anyone who may read it, and the console
    shows it too.
    """
    variables = {
        "YAIT_SESSIONS_TABLE": N.SESSIONS_TABLE,
        "YAIT_EVENTS_TABLE": N.EVENTS_TABLE,
        "YAIT_AGENTS_TABLE": N.AGENTS_TABLE,
        "YAIT_VAULT_KEY_SECRET": N.VAULT_KEY_SECRET,
        **(extra or {}),
    }
    # JSON, not the CLI's shorthand: a value with "=" or "," in it — an address,
    # a list of them — is mangled by the shorthand and passed whole by JSON.
    return ("--environment", json.dumps({"Variables": variables}))


def ensure_role(name: str, extra_policies: tuple[str, ...] = ()) -> str:
    """
    A function's role: logs, and the data policy the owner created.

    The deploy may create it, and the deploy policy makes that harmless —
    `iam:AttachRolePolicy` is conditioned on exactly two policy ARNs, so nothing
    else can be attached. Without that condition it could attach AdministratorAccess
    and pass the result to Lambda.
    """
    step(f"role {name}")
    got = aws("iam", "get-role", "--role-name", name, check=False)
    if "__error__" not in got:
        arn = got["Role"]["Arn"]
        print(f"   exists: {arn}")
    else:
        made = aws("iam", "create-role", "--role-name", name, "--path", ROLE_PATH,
                   "--assume-role-policy-document", json.dumps(TRUST),
                   "--description", ASCII_DESCRIPTION)
        arn = made["Role"]["Arn"]
        print(f"   created: {arn}")
    aws("iam", "attach-role-policy", "--role-name", name,
        "--policy-arn", BASIC_EXECUTION)
    print("   logs policy attached")

    # The data policy. The deploy may attach exactly this one and the AWS logs
    # policy, and it cannot create it, because a release that can write a policy and
    # pass the role to Lambda can run anything as an administrator. So a missing one
    # is the owner's setup step, named.
    attached = aws("iam", "attach-role-policy", "--role-name", name,
                   "--policy-arn", _data_policy_arn(), check=False)
    if "__error__" in attached:
        raise SystemExit(
            f"   the data policy {N.DATA_POLICY} does not exist, and this deploy "
            f"cannot create one — deliberately.\n\n"
            f"   Create it once, with credentials that may manage IAM: "
            f"docs/deploy.md, step 2b.\n\n"
            f"   ({attached['__error__'].splitlines()[-1]})")
    print(f"   data policy attached: {N.DATA_POLICY}")

    for policy in extra_policies:
        got = aws("iam", "attach-role-policy", "--role-name", name,
                  "--policy-arn", _policy_arn(policy), check=False)
        if "__error__" in got:
            raise SystemExit(
                f"   the policy {policy} does not exist, and this deploy cannot create "
                f"one — deliberately. Create it once: docs/deploy.md, step 2.\n"
                f"   ({got['__error__'].splitlines()[-1]})")
        print(f"   {policy} attached")
    return arn


_ACCOUNT: dict[str, str] = {}


def _policy_arn(name: str) -> str:
    if "id" not in _ACCOUNT:
        _ACCOUNT["id"] = aws("sts", "get-caller-identity")["Account"]
    return f"arn:aws:iam::{_ACCOUNT['id']}:policy/{name}"


def _data_policy_arn() -> str:
    return _policy_arn(N.DATA_POLICY)


# ── 1b. the tables ─────────────────────────────────────────────────────────

TABLES = {
    # pk is the partition, sk the sort. The event log's sk is a NUMBER, not a
    # padded string, so the store's ordering is the sequence's ordering and there
    # is no padding bug to find later.
    N.SESSIONS_TABLE: [("pk", "S", "HASH"), ("sk", "S", "RANGE")],
    N.EVENTS_TABLE:   [("pk", "S", "HASH"), ("sk", "N", "RANGE")],
    N.AGENTS_TABLE:   [("pk", "S", "HASH"), ("sk", "S", "RANGE")],
}


def ensure_tables(region: str) -> None:
    """
    On-demand billing, so an idle deployment pays for storage only.

    Existing tables are left alone rather than reconciled: changing a key schema is
    not something a release should do silently, and DynamoDB will not do it at all.
    """
    step("tables")
    for name, schema in TABLES.items():
        got = aws("dynamodb", "describe-table", "--table-name", name,
                  region=region, check=False)
        if "__error__" not in got:
            keys = {k["AttributeName"]: k["KeyType"]
                    for k in got["Table"]["KeySchema"]}
            print(f"   exists: {name}  {keys}")
            continue
        # One argument per definition. The CLI's shorthand puts the commas *inside*
        # each argument, so joining the pairs with commas and splitting on them
        # tears every pair in half — which is exactly what it did.
        attrs = [f"AttributeName={a},AttributeType={ty}" for a, ty, _ in schema]
        key_schema = [f"AttributeName={a},KeyType={role}" for a, _, role in schema]
        aws("dynamodb", "create-table", "--table-name", name,
            "--attribute-definitions", *attrs,
            "--key-schema", *key_schema,
            "--billing-mode", "PAY_PER_REQUEST", region=region)
        print(f"   created: {name}")
    for name in TABLES:
        aws("dynamodb", "wait", "table-exists", "--table-name", name, region=region)
    print("   active")


# ── 2. the function ────────────────────────────────────────────────────────

def package() -> pathlib.Path:
    """
    The function's zip: src/ plus its dependencies, vendored.

    Vendored rather than put in a Lambda layer, deliberately. A layer means an ARN
    that differs per region and per architecture and moves with every release of the
    layer — one more thing to look up, get wrong, and debug from a cold start. A zip
    is the same everywhere.
    """
    step("package")
    PLATFORMS = {"arm64": "manylinux2014_aarch64", "x86_64": "manylinux2014_x86_64"}
    tmp = pathlib.Path(tempfile.mkdtemp())
    stage = tmp / "pkg"
    stage.mkdir()

    requirements = ROOT / "src" / "requirements.txt"
    if requirements.exists():
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(stage),
             # For the function's machine, not this one: the signature check
             # needs a compiled library, and a wheel built for the runner's
             # platform would not load in Lambda.
             "--platform", PLATFORMS[N.ARCHITECTURE], "--implementation", "cp",
             "--python-version", N.RUNTIME.removeprefix("python"),
             "--only-binary=:all:",
             "-r", str(requirements)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            raise SystemExit(f"pip install failed:\n{p.stderr.strip()}")
        # Caches and scripts nothing at runtime reads. Not the *.dist-info: a
        # library may read its own metadata when it runs — fastmcp asks for its
        # version on every connection, and without it no MCP server can be reached.
        for junk in list(stage.rglob("__pycache__")) + list(stage.glob("bin")):
            shutil.rmtree(junk, ignore_errors=True)
        names = sorted(d.name for d in stage.iterdir() if d.is_dir())
        print(f"   dependencies: {', '.join(names)}")

    # `src/` is the function, so packaging it wholesale is correct by construction.
    # Anything that only exists for development — the local server, for one — lives
    # elsewhere, or it ships to Lambda and is never imported there.
    for f in sorted((ROOT / "src").glob("*.py")):
        shutil.copy2(f, stage / f.name)
        print(f"   {f.name}")

    zip_path = tmp / "function.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", stage)
    print(f"   {zip_path.name}  {zip_path.stat().st_size:,} bytes")
    return zip_path


@dataclasses.dataclass(frozen=True)
class Function:
    """One Lambda this deploy owns: what it is called, what runs, and as whom."""

    name: str
    role: str
    handler: str
    timeout: int
    description: str
    environment: dict = dataclasses.field(default_factory=dict)
    #: Policies beyond logs and data. None today: both functions run with the data policy.
    policies: tuple = ()
    memory_mb: int = N.MEMORY_MB


API = Function(
    name=N.FUNCTION, role=N.ROLE_NAME, handler="handler.handler",
    timeout=N.TIMEOUT_S, description="yait agents API: both dialect trees",
    # The API wakes the worker by name; nothing else about it is shared.
    environment={"YAIT_WORKER_FUNCTION": N.WORKER_FUNCTION},
)
WORKER = Function(
    name=N.WORKER_FUNCTION, role=N.WORKER_ROLE_NAME, handler="worker.handler",
    timeout=N.WORKER_TIMEOUT_S, description="yait agents step worker",
    environment={
        "YAIT_MODEL_BACKEND": N.MODEL_BACKEND,
        # The lease outlives the longest invocation, so a live worker never loses
        # it and a dead one frees the session shortly after.
        "YAIT_LEASE_SECONDS": str(N.WORKER_TIMEOUT_S + 30),
        "YAIT_RESERVE_MS": str(N.WORKER_RESERVE_S * 1000),
        # The named model servers, from the YAIT_ENDPOINTS variable — parsed and
        # checked in main() before anything changes; here as the worker reads it.
        "YAIT_ENDPOINTS": "{}",
        # The most one answer may take, thinking included (src/model.py). Empty is
        # the platform's default.
        "YAIT_MAX_TOKENS": os.environ.get("YAIT_MAX_TOKENS", ""),
    },
    memory_mb=N.WORKER_MEMORY_MB,
)
FUNCTIONS = (API, WORKER)


def ensure_function(fn: Function, role_arn: str, zip_path: pathlib.Path,
                    region: str) -> str:
    step(f"function {fn.name}")
    got = aws("lambda", "get-function", "--function-name", fn.name,
              region=region, check=False)
    if "__error__" not in got:
        aws("lambda", "update-function-code", "--function-name", fn.name,
            "--zip-file", f"fileb://{zip_path}", region=region)
        aws("lambda", "wait", "function-updated", "--function-name", fn.name,
            region=region, check=False)
        # The names are configurable, so they can change between releases; an
        # update that refreshes only the code would leave the function pointing at
        # the table it was created with.
        aws("lambda", "update-function-configuration", "--function-name", fn.name,
            "--timeout", str(fn.timeout), "--memory-size", str(fn.memory_mb),
            *_environment(fn.environment),
            region=region)
        aws("lambda", "wait", "function-updated", "--function-name", fn.name,
            region=region, check=False)
        arn = got["Configuration"]["FunctionArn"]
        print(f"   code and configuration updated: {arn}")
        return arn

    # A freshly created role is not visible to Lambda straight away; IAM is
    # eventually consistent and the first attempts fail on exactly that.
    last = ""
    for attempt in range(12):
        made = aws(
            "lambda", "create-function",
            "--function-name", fn.name,
            "--runtime", RUNTIME,
            "--architectures", N.ARCHITECTURE,
            "--role", role_arn,
            "--handler", fn.handler,
            "--zip-file", f"fileb://{zip_path}",
            "--timeout", str(fn.timeout),
            # Small on purpose: a step spends most of its wall-clock blocked on
            # the network, and Lambda bills memory x wall-clock. docs/economics.md.
            "--memory-size", str(fn.memory_mb),
            "--description", fn.description,
            *_environment(fn.environment),
            region=region, check=False,
        )
        if "__error__" not in made:
            print(f"   created: {made['FunctionArn']}")
            aws("lambda", "wait", "function-active-v2", "--function-name", fn.name,
                region=region, check=False)
            return made["FunctionArn"]
        last = made["__error__"]
        if "cannot be assumed" not in last and "InvalidParameterValue" not in last:
            raise SystemExit(last)
        print(f"   waiting for the role to propagate ({attempt + 1}/12)")
        time.sleep(5)
    raise SystemExit(last)


def ensure_invoke_grants(region: str, callers: dict[str, str]) -> None:
    """
    Who may start the worker: the API, and the worker itself for its handoff.

    Granted on the worker's *resource* policy, not by widening either role. Within
    one account a resource policy naming a role is enough on its own, and the deploy
    may add those (`lambda:AddPermission` on its own functions) — whereas changing
    the data policy is the owner's, by design. So the capability arrives without the
    deploy gaining any right it did not have.
    """
    step(f"who may invoke {WORKER.name}")
    for statement, role_arn in callers.items():
        aws("lambda", "remove-permission", "--function-name", WORKER.name,
            "--statement-id", statement, region=region, check=False)
        aws("lambda", "add-permission", "--function-name", WORKER.name,
            "--statement-id", statement, "--action", "lambda:InvokeFunction",
            "--principal", role_arn, region=region)
        print(f"   {statement}: {role_arn.rsplit('/', 1)[-1]}")


# ── 3. the API ─────────────────────────────────────────────────────────────

def api_name() -> str:
    """Configurable, and applied by renaming after import — see names.py."""
    return N.API_NAME


def find_api(region: str) -> str | None:
    want = api_name()
    apis = aws("apigateway", "get-rest-apis", "--limit", "500", region=region)
    for item in apis.get("items", []):
        if item.get("name") == want:
            return item["id"]
    return None


def ensure_api(function_arn: str, region: str, account: str) -> str:
    step("REST API from the OpenAPI document")
    spec = B.with_aws(B.document(), region=region, lambda_arn=function_arn,
                      api_name=api_name())
    body = json.dumps(spec).encode()
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as fh:
        fh.write(body)
        spec_file = fh.name
    ops = sum(len(v) for v in spec["paths"].values())
    print(f"   {len(spec['paths'])} paths, {ops} operations, {len(body):,} bytes")

    api_id = find_api(region)
    if api_id:
        aws("apigateway", "put-rest-api", "--rest-api-id", api_id,
            "--mode", "overwrite", "--body", f"fileb://{spec_file}", region=region)
        print(f"   re-imported into {api_id}")
    else:
        made = aws("apigateway", "import-rest-api",
                   "--body", f"fileb://{spec_file}", region=region)
        api_id = made["id"]
        print(f"   imported as {api_id}, named {made.get('name')!r}")

    step("permission for the gateway to invoke the function")
    # The statement is bound to one API's ARN, so a statement left from an earlier
    # API does not grant this one anything. Treating "already exists" as success is
    # how a fresh API ends up answering 500: the gateway may not invoke the
    # function, and the only sign is an opaque Internal server error.
    #
    # So: drop whatever is there, then add — and this add is checked.
    aws("lambda", "remove-permission", "--function-name", FUNCTION,
        "--statement-id", N.STATEMENT_ID, region=region, check=False)
    aws("lambda", "add-permission",
        "--function-name", FUNCTION,
        "--statement-id", N.STATEMENT_ID,
        "--action", "lambda:InvokeFunction",
        "--principal", "apigateway.amazonaws.com",
        "--source-arn", f"arn:aws:execute-api:{region}:{account}:{api_id}/*/*/*",
        region=region)
    print(f"   granted to {api_id}")
    # The same function is the gateway's request authorizer, and an authorizer is
    # invoked under a different ARN — api/authorizers/<id> — which the statement
    # above, for api/<stage>/<method>/<path>, does not cover.
    aws("lambda", "remove-permission", "--function-name", FUNCTION,
        "--statement-id", N.STATEMENT_ID + "-authorizer", region=region, check=False)
    aws("lambda", "add-permission",
        "--function-name", FUNCTION,
        "--statement-id", N.STATEMENT_ID + "-authorizer",
        "--action", "lambda:InvokeFunction",
        "--principal", "apigateway.amazonaws.com",
        "--source-arn", f"arn:aws:execute-api:{region}:{account}:{api_id}/authorizers/*",
        region=region)
    print("   and as its authorizer")

    step(f"deploy to stage {STAGE}")
    aws("apigateway", "create-deployment", "--rest-api-id", api_id,
        "--stage-name", STAGE, "--description", "skeleton", region=region)
    print("   deployed")
    return api_id


def teardown(region: str) -> int:
    step("delete")
    api_id = find_api(region)
    if api_id:
        aws("apigateway", "delete-rest-api", "--rest-api-id", api_id, region=region)
        print(f"   API {api_id} deleted")
    for fn in FUNCTIONS:
        aws("lambda", "delete-function", "--function-name", fn.name,
            region=region, check=False)
        print(f"   function {fn.name} deleted")
    # The tables are deliberately NOT deleted: they hold the event log, which is
    # the only record of what happened. A release that can erase history by
    # accident is a release nobody should run. Remove them by hand if you mean it.
    for name in TABLES:
        print(f"   table {name} left in place — it holds the log")
    for fn in FUNCTIONS:
        # Both policies, or the role cannot be deleted — and with check=False that
        # failure would be silent. It was, until the data policy existed.
        # Whatever is attached, not a list of what should be: an installation from
        # before a policy was dropped still has it, and the role would not go.
        listed = aws("iam", "list-attached-role-policies", "--role-name", fn.role,
                     check=False)
        attached = [p["PolicyArn"] for p in listed.get("AttachedPolicies", [])]
        for policy in dict.fromkeys([BASIC_EXECUTION, _data_policy_arn(),
                                     *(_policy_arn(p) for p in fn.policies), *attached]):
            aws("iam", "detach-role-policy", "--role-name", fn.role,
                "--policy-arn", policy, check=False)
        gone = aws("iam", "delete-role", "--role-name", fn.role, check=False)
        state = "deleted" if "__error__" not in gone else \
            f"NOT deleted — {gone['__error__'].splitlines()[-1]}"
        print(f"   role {fn.role} {state}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--plan", action="store_true",
                    help="say what would be created or updated, and stop")
    ap.add_argument("--skip-permission-check", action="store_true",
                    help="only for a rerun moments after a passing check")
    ap.add_argument("--find", action="store_true",
                    help="change nothing: find the deployed API and give its base URL")
    ap.add_argument("--url-out",
                    help="write the base URL here. A caller that scrapes stdout "
                         "instead gets an empty string when anything changes, and "
                         "an empty URL is how a green run comes to prove nothing.")
    a = ap.parse_args()

    # The named model servers are checked before anything else, and before any
    # change to the account: a wrong line stops the release with the line named,
    # rather than failing some caller's first request after it.
    try:
        servers = endpoints.parse(os.environ.get("YAIT_ENDPOINTS", ""))
    except endpoints.Invalid as exc:
        print(f"   {exc}", file=sys.stderr)
        return 2
    WORKER.environment["YAIT_ENDPOINTS"] = json.dumps(
        {k: v for k, v in servers.items()
         if not (endpoints.BUILT_IN.get(k) == v["url"] and not v["extra"])})
    print("   model servers: " + ", ".join(
        f"{k} → {v['url']}" + (f" {json.dumps(v['extra'])}" if v["extra"] else "")
        for k, v in sorted(servers.items())))

    if not a.skip_permission_check:
        step("permissions")
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_permissions.py"),
             "--region", a.region, "--quiet"]
        ).returncode
        if rc != 0:
            return rc

    who = aws("sts", "get-caller-identity")
    print(f"\naccount {who['Account']} as {who['Arn'].rsplit('/', 1)[-1]} "
          f"in {a.region}")

    if a.delete:
        return teardown(a.region)

    if a.find:
        # For a caller that needs the deployment, not a new one — the quality
        # run. Absent is a failure, never an empty URL.
        api_id = find_api(a.region)
        if not api_id:
            print(f"   no API named {api_name()!r} in {a.region}; deploy first")
            return 1
        base = f"https://{api_id}.execute-api.{a.region}.amazonaws.com/{STAGE}"
        if a.url_out:
            pathlib.Path(a.url_out).write_text(base)
        print(f"   found {api_name()!r}: {base}")
        return 0

    if a.plan:
        step("plan")
        role = aws("iam", "get-role", "--role-name", ROLE_NAME, check=False)
        fn = aws("lambda", "get-function", "--function-name", FUNCTION,
                 region=a.region, check=False)
        api_id = find_api(a.region)
        for name in TABLES:
            got = aws("dynamodb", "describe-table", "--table-name", name,
                      region=a.region, check=False)
            print(f"   {'update ' if '__error__' not in got else 'create '}  "
                  f"table    {name}")
        worker_fn = aws("lambda", "get-function", "--function-name", WORKER.name,
                        region=a.region, check=False)
        worker_role = aws("iam", "get-role", "--role-name", WORKER.role, check=False)
        for what, exists in (
            (f"role     {ROLE_NAME}", "__error__" not in role),
            (f"role     {WORKER.role}", "__error__" not in worker_role),
            (f"function {FUNCTION}", "__error__" not in fn),
            (f"function {WORKER.name}", "__error__" not in worker_fn),
            (f"api      {api_name()!r}" + (f" ({api_id})" if api_id else ""), bool(api_id)),
        ):
            print(f"   {'update ' if exists else 'create '}  {what}")
        print("\nNothing was changed. Drop --plan to apply.")
        return 0

    roles = {fn.name: ensure_role(fn.role, fn.policies) for fn in FUNCTIONS}
    ensure_tables(a.region)
    zip_path = package()
    arns = {fn.name: ensure_function(fn, roles[fn.name], zip_path, a.region)
            for fn in FUNCTIONS}
    ensure_invoke_grants(a.region, {
        f"{N.PREFIX}_api_wakes_worker": roles[API.name],
        f"{N.PREFIX}_worker_hands_off": roles[WORKER.name],
    })
    api_id = ensure_api(arns[API.name], a.region, who["Account"])

    base = f"https://{api_id}.execute-api.{a.region}.amazonaws.com/{STAGE}"
    if a.url_out:
        pathlib.Path(a.url_out).write_text(base)
    print(f"""
── deployed

  base url                  {base}
  Anthropic SDK base_url    {base}/anthropic
  OpenAI SDK base_url       {base}/openai/v1

The two differ by more than the prefix: the Anthropic SDK adds /v1 itself, the
OpenAI SDK expects it inside base_url.

  python3 tests/test_api.py --base-url {base} --skip-spec
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
