#!/usr/bin/env python3
"""
Step zero of every deploy: do these credentials have what it needs?

A half-permitted deploy fails in the middle, having already created some of the
resources, and reports one API call's AccessDenied rather than the missing right.
So this runs first and names what is absent.

Where a harmless read exists it makes the real call; everything else goes through
iam:SimulatePrincipalPolicy, which is not proof — without a resource ARN it does
not see resource-level grants, and it reports apigateway:POST as denied for
credentials that demonstrably create REST APIs. Unconfirmed is therefore reported
as unconfirmed, not as failure.

    python3 scripts/check_permissions.py --region us-west-1
    python3 scripts/check_permissions.py --region us-west-1 --probe-writes
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "infra" / "deploy-policy.json"

OK, MISSING, DOUBTFUL, UNKNOWN = "ok", "missing", "doubtful", "unknown"
MARK = {OK: "  ok    ", MISSING: "MISSING ", DOUBTFUL: "doubtful", UNKNOWN: "unknown "}

# Every action the deploy actually calls, and what it is called for. The list is
# the contract: infra/deploy-policy.json exists to satisfy exactly this.
NEEDED: list[tuple[str, str]] = [
    ("sts:GetCallerIdentity",        "say whose account is being deployed into"),
    ("iam:SimulatePrincipalPolicy",  "this check"),
    ("iam:GetRole",                  "is the function's role already there"),
    ("iam:CreateRole",               "create it if not"),
    ("iam:AttachRolePolicy",         "attach the logs policy — and only that one"),
    ("iam:PassRole",                 "hand the role to Lambda"),
    ("lambda:GetFunction",           "is the function already there"),
    ("lambda:CreateFunction",        "create it"),
    ("lambda:UpdateFunctionCode",    "update it on every release"),
    ("lambda:AddPermission",         "let the gateway invoke it"),
    ("apigateway:GET",               "find an existing API by name"),
    ("apigateway:POST",              "import the OpenAPI document, deploy the stage"),
    ("apigateway:PUT",               "re-import it on a later release"),
    ("lambda:DeleteFunction",        "teardown"),
    ("iam:DeleteRole",               "teardown"),
    ("apigateway:DELETE",            "teardown"),
    ("dynamodb:DescribeTable",       "are the tables already there"),
    ("dynamodb:CreateTable",         "create them if not"),
    ("dynamodb:PutItem",             "append an event"),
    ("dynamodb:Query",               "read the log"),
    ("dynamodb:UpdateItem",          "the cursor and the lease"),
    ("dynamodb:TransactWriteItems",  "events and the cursor in one commit"),
]



def aws(*args: str, region: str | None = None) -> tuple[bool, dict | str]:
    cmd = ["aws", *args, "--output", "json"]
    if region:
        cmd += ["--region", region]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return False, p.stderr.strip()
    out = (p.stdout or "").strip()
    try:
        return True, json.loads(out) if out else {}
    except json.JSONDecodeError:
        return True, out


def first_hand(region: str) -> dict[str, str]:
    """Capabilities proven by making a real, harmless call."""
    found: dict[str, str] = {}

    ok, who = aws("sts", "get-caller-identity")
    found["sts:GetCallerIdentity"] = OK if ok else MISSING

    # A read on the gateway. An empty list is a pass: it proves the right, not
    # the presence of anything.
    ok, _ = aws("apigateway", "get-rest-apis", "--limit", "1", region=region)
    found["apigateway:GET"] = OK if ok else MISSING

    # GetRole on a role that may not exist: NoSuchEntity proves the permission
    # just as well as a hit does, and AccessDenied disproves it.
    ok, err = aws("iam", "get-role", "--role-name", "yait-agents-api-role")
    found["iam:GetRole"] = OK if ok or "NoSuchEntity" in str(err) else MISSING

    ok, err = aws("lambda", "get-function", "--function-name", "yait-agents-api",
                  region=region)
    found["lambda:GetFunction"] = (
        OK if ok or "ResourceNotFound" in str(err) else MISSING
    )
    return found


def simulated(arn: str, actions: list[str]) -> dict[str, str]:
    ok, res = aws("iam", "simulate-principal-policy",
                  "--policy-source-arn", arn,
                  "--action-names", *actions)
    if not ok:
        return {a: UNKNOWN for a in actions}
    out: dict[str, str] = {}
    for r in res.get("EvaluationResults", []):
        decision = r.get("EvalDecision", "")
        out[r["EvalActionName"]] = OK if decision == "allowed" else DOUBTFUL
    return {a: out.get(a, UNKNOWN) for a in actions}


def probe_writes(region: str) -> dict[str, str]:
    """
    Settle the doubtful gateway verbs the only way they can be settled: write.

    Creates an empty REST API and deletes it immediately. Off by default, because
    a check should not create anything unless asked.
    """
    ok, made = aws("apigateway", "create-rest-api",
                   "--name", "yait-agents-permission-probe", region=region)
    if not ok:
        return {"apigateway:POST": MISSING}
    api_id = made.get("id", "")
    deleted, _ = aws("apigateway", "delete-rest-api", "--rest-api-id", api_id,
                     region=region)
    return {
        "apigateway:POST": OK,
        "apigateway:DELETE": OK if deleted else MISSING,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--probe-writes", action="store_true",
                    help="settle doubtful gateway verbs by creating and deleting "
                         "a throwaway API")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    ok, who = aws("sts", "get-caller-identity")
    if not ok:
        print("No usable AWS credentials at all. Set AWS_ACCESS_KEY_ID and "
              "AWS_SECRET_ACCESS_KEY, or configure a profile.\n"
              f"  {who}", file=sys.stderr)
        return 2
    arn, account = who["Arn"], who["Account"]
    print(f"identity  {arn}")
    print(f"account   {account}   region {a.region}\n")

    status = {action: UNKNOWN for action, _ in NEEDED}
    status.update(simulated(arn, [action for action, _ in NEEDED]))
    status.update(first_hand(a.region))          # ground truth wins
    if a.probe_writes:
        status.update(probe_writes(a.region))

    width = max(len(action) for action, _ in NEEDED)
    for action, why in NEEDED:
        state = status[action]
        if a.quiet and state == OK:
            continue
        print(f"  {MARK[state]}  {action:<{width}}  {why}")

    missing = [act for act, _ in NEEDED if status[act] == MISSING]
    doubtful = [act for act, _ in NEEDED if status[act] == DOUBTFUL]
    unknown = [act for act, _ in NEEDED if status[act] == UNKNOWN]

    print()
    if missing:
        print(f"{len(missing)} permission(s) are definitely absent:")
        for m in missing:
            print(f"  - {m}")
        print(f"\nAttach {POLICY.relative_to(ROOT)} to the credentials and retry.")
        return 1

    if doubtful:
        print(f"{len(doubtful)} permission(s) could not be confirmed by simulation:")
        for d in doubtful:
            print(f"  - {d}")
        print("\nSimulation cannot see resource-level grants, so this is not a "
              "failure.\nSettle it with --probe-writes, or let the deploy try.")
    if unknown:
        print(f"{len(unknown)} permission(s) could not be checked "
              "(iam:SimulatePrincipalPolicy is itself denied).")

    if not doubtful and not unknown:
        print("Every permission the deploy needs is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
