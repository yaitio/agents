#!/usr/bin/env python3
"""
Agents: the store, and the two shapes one record takes.

Two halves, and the second is the one that matters. The store's behaviour is
ordinary bookkeeping. The rendering is the claim the whole two-façade design rests
on — that a record can be shared and only its serialization differs — and it is
either demonstrated here or it is an assertion in a document.

    python3 tests/test_agents.py
    python3 tests/test_agents.py --dynamo --region us-west-1 --table yait_agents_agents
"""

from __future__ import annotations

import json

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agentstore as A  # noqa: E402
import render as Rn  # noqa: E402
import tenancy  # noqa: E402

TENANT = "t_test"


class Results:
    def __init__(self, label: str) -> None:
        self.label, self.passed, self.failures = label, 0, []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        print(f"\n{self.label}: {self.passed}/{self.passed + len(self.failures)}"
              " checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


def store_checks(store, label: str) -> int:
    res = Results(label)

    made = store.create(TENANT, name="first", model="claude-opus-5",
                        instructions="be brief", metadata={"k": "v"})
    res.check("create returns version 1", made["version"] == 1, f"{made}")
    res.check("create keeps what it was given",
              made["model"] == "claude-opus-5" and made["instructions"] == "be brief",
              f"{made}")
    agent_id = made["agent_id"]

    res.check("the record reads back",
              store.get(TENANT, agent_id)["agent_id"] == agent_id)

    try:
        store.create(TENANT, name="no model")
        res.check("model is required", False, "it was accepted")
    except ValueError as exc:
        res.check("model is required", "model is required" in str(exc), str(exc))

    try:
        store.create(TENANT, model="m", nonsense=1)
        res.check("an unknown field is refused by name", False, "it was accepted")
    except ValueError as exc:
        res.check("an unknown field is refused by name", "nonsense" in str(exc),
                  str(exc))

    # A version is never mutated: a session pins one, and a session that followed a
    # moving definition would change behaviour mid-conversation without saying so.
    updated = store.update(TENANT, agent_id, instructions="be thorough")
    res.check("an update writes a new version", updated["version"] == 2,
              f"{updated['version']}")
    res.check("the update took effect",
              updated["instructions"] == "be thorough", f"{updated}")
    res.check("unmentioned fields survive an update",
              updated["model"] == "claude-opus-5" and updated["name"] == "first",
              f"{updated}")

    first = store.get(TENANT, agent_id, version=1)
    res.check("version 1 is unchanged",
              first["instructions"] == "be brief" and first["version"] == 1,
              f"{first}")

    versions = store.versions(TENANT, agent_id)
    res.check("both versions are listed",
              [v["version"] for v in versions] == [1, 2],
              f"{[v['version'] for v in versions]}")

    res.check("get without a version gives the latest",
              store.get(TENANT, agent_id)["version"] == 2)

    listed = store.list(TENANT)
    res.check("the agent appears in the list",
              any(a["agent_id"] == agent_id for a in listed), f"{len(listed)}")

    other = store.create(TENANT, model="m2")
    listed = store.list(TENANT)
    res.check("the list is newest first",
              listed[0]["agent_id"] == other["agent_id"],
              f"{[a['agent_id'] for a in listed[:2]]}")

    # A different tenant sees nothing, and that is structural: the tenant is a
    # prefix of the partition key, so there is no query that crosses it.
    res.check("another tenant sees none of it", store.list("t_other") == [],
              f"{store.list('t_other')}")
    try:
        store.get("t_other", agent_id)
        res.check("another tenant cannot read it", False, "it was readable")
    except A.NotFound:
        res.check("another tenant cannot read it", True)

    archived = store.archive(TENANT, agent_id)
    res.check("archive marks it", archived.get("archived") is True, f"{archived}")
    res.check("archive is idempotent",
              store.archive(TENANT, agent_id).get("archived") is True)
    try:
        store.update(TENANT, agent_id, model="m3")
        res.check("an archived agent is read-only", False, "the update went through")
    except A.Conflict:
        res.check("an archived agent is read-only", True)

    try:
        store.get(TENANT, "agent_missing")
        res.check("a missing agent is NotFound", False, "it returned something")
    except A.NotFound:
        res.check("a missing agent is NotFound", True)

    return res.report()


def id_checks() -> int:
    """
    Ids must sort in the order they were made — a burst inside one millisecond is
    the case that broke twice before this check existed.
    """
    import events as E
    res = Results("ids")
    made = [E.new_id("agent") for _ in range(500)]
    res.check("ids sort in creation order", made == sorted(made),
              "a burst inside one millisecond sorted randomly")
    res.check("ids are unique", len(set(made)) == len(made))
    res.check("the prefix survives", all(i.startswith("agent_") for i in made))
    res.check("ids are fixed width", len({len(i) for i in made}) == 1,
              f"{sorted({len(i) for i in made})} — a varying width breaks "
              "lexicographic ordering")
    return res.report()


def render_checks() -> int:
    """
    One record, two shapes — with the differences named.

    A renderer that only renamed fields would be wrong about three of these.
    """
    res = Results("render")
    record = {
        "agent_id": "agent_1", "name": "A", "model": "claude-opus-5",
        "instructions": "be brief", "tools": [], "metadata": {},
        "version": 3, "latest_version": 3,
        "created_at": 1_700_000_000_000, "updated_at": 1_700_000_500_000,
    }
    a = Rn.agent(record, Rn.ANTHROPIC)
    o = Rn.agent(record, Rn.OPENAI)

    res.check("both carry the same id", a["id"] == o["id"] == "agent_1")

    res.check("Anthropic names the prompt `system`",
              a.get("system") == "be brief" and "instructions" not in a, f"{a}")
    res.check("OpenAI names it `instructions`",
              o.get("instructions") == "be brief" and "system" not in o, f"{o}")

    res.check("Anthropic exposes the version", a.get("version") == 3, f"{a}")
    res.check("OpenAI does not — it has no versions endpoint",
              "version" not in o,
              "showing one invents a surface its clients cannot use")

    res.check("Anthropic timestamps are ISO-8601 with a Z",
              isinstance(a["created_at"], str) and a["created_at"].endswith("Z"),
              f"{a['created_at']}")
    res.check("OpenAI timestamps are whole Unix seconds",
              isinstance(o["created_at"], int) and o["created_at"] == 1_700_000_000,
              f"{o['created_at']}")

    res.check("OpenAI carries its `object` discriminator",
              o.get("object") == "agent",
              "their SDKs pick a type from it; an unknown value raises")
    res.check("Anthropic carries `type` instead",
              a.get("type") == "agent" and "object" not in a, f"{a}")

    empty_a = Rn.agent_list([], Rn.ANTHROPIC)
    empty_o = Rn.agent_list([], Rn.OPENAI)
    res.check("an Anthropic list has no `object`", "object" not in empty_a,
              f"{empty_a}")
    res.check("an OpenAI list is marked as one", empty_o.get("object") == "list",
              f"{empty_o}")

    err_a = Rn.error(404, "not_found_error", "gone", Rn.ANTHROPIC)
    err_o = Rn.error(404, "not_found_error", "gone", Rn.OPENAI)
    res.check("the Anthropic error envelope is type + error",
              err_a["type"] == "error" and set(err_a["error"]) == {"type", "message"},
              f"{err_a}")
    res.check("the OpenAI error envelope carries param and code",
              set(err_o["error"]) == {"message", "type", "param", "code"},
              f"{err_o}")

    return res.report()


def tenancy_checks() -> int:
    res = Results("tenancy")
    import os
    import tokens

    private, public = tokens.keypair()
    tenancy.reset_cache()
    os.environ.pop("YAIT_JWT_PUBLIC_KEYS", None)
    table = os.environ.pop("YAIT_AGENTS_TABLE", None)
    token = tokens.issue(private, kid="t1", tenant="acme", name="alice")
    try:
        tenancy.resolve({"X-Yait-Key": token})
        res.check("with no public key, nothing is let in", False, "it was accepted")
    except tenancy.NotConfigured:
        res.check("with no public key, nothing is let in", True)

    os.environ["YAIT_JWT_PUBLIC_KEYS"] = json.dumps({"t1": public})
    tenancy.reset_cache()
    who = tenancy.resolve({"X-Yait-Key": token})
    res.check("the token's subject is the tenant", who.tenant == "acme", f"{who}")
    res.check("and its name is the key's name in logs", who.key_id == "alice", f"{who}")
    res.check("with no provider key, none is passed on", who.provider_key is None)
    who = tenancy.resolve({"X-Yait-Key": token, "x-api-key": "sk-ant-mine"})
    res.check("a provider key in the Anthropic convention is passed on",
              who.provider_key == "sk-ant-mine", f"{who}")
    who = tenancy.resolve({"X-Yait-Key": token, "Authorization": "Bearer sk-proj-mine"})
    res.check("and in the OpenAI convention", who.provider_key == "sk-proj-mine", f"{who}")
    res.check("no key appears in the principal's repr",
              token not in repr(who) and "sk-proj-mine" not in repr(who), repr(who))

    other_private, _ = tokens.keypair()
    import jwt
    for name, headers in (
            ("no token", {}),
            ("a string that is not a token", {"X-Yait-Key": "anything"}),
            ("a token signed by another key",
             {"X-Yait-Key": tokens.issue(other_private, kid="t1", tenant="acme")}),
            ("a token naming a key the installation does not have",
             {"X-Yait-Key": tokens.issue(private, kid="t9", tenant="acme")}),
            ("an unsigned token",
             {"X-Yait-Key": jwt.encode({"sub": "acme", "jti": "x"}, None,
                                       algorithm="none", headers={"kid": "t1"})}),
            ("our token sent where a provider key goes", {"x-api-key": token})):
        try:
            tenancy.resolve(headers)
            res.check(f"{name} is refused", False, "it was accepted")
        except tenancy.Unauthorized:
            res.check(f"{name} is refused", True)

    # Behind the gateway the authorizer has checked it, and says whose it is.
    who = tenancy.resolve({"x-api-key": "sk-ant-mine"},
                          {"tenant": "acme", "key_id": "alice", "principalId": "acme"})
    res.check("behind the gateway, the authorizer's tenant is taken",
              who.tenant == "acme" and who.provider_key == "sk-ant-mine", f"{who}")

    arn = "arn:aws:execute-api:us-west-1:123:abc123/prod/GET/anthropic/v1/agents"
    answer = tokens.authorize({"type": "REQUEST", "methodArn": arn,
                               "headers": {"X-Yait-Key": token}})
    statement = answer["policyDocument"]["Statement"][0]
    res.check("the authorizer allows the whole API, since its answer is cached",
              statement["Effect"] == "Allow"
              and statement["Resource"] == "arn:aws:execute-api:us-west-1:123:abc123/*",
              f"{statement}")
    res.check("and hands on the tenant", answer["context"]["tenant"] == "acme",
              f"{answer['context']}")
    try:
        tokens.authorize({"type": "REQUEST", "methodArn": arn,
                          "headers": {"X-Yait-Key": "anything"}})
        res.check("the authorizer refuses a bad token", False, "it allowed it")
    except Exception as exc:  # noqa: BLE001
        res.check("the authorizer refuses a bad token, as the gateway's 401",
                  str(exc) == "Unauthorized", str(exc))

    os.environ.pop("YAIT_JWT_PUBLIC_KEYS", None)
    if table:
        os.environ["YAIT_AGENTS_TABLE"] = table
    tenancy.reset_cache()
    return res.report()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dynamo", action="store_true")
    ap.add_argument("--table")
    ap.add_argument("--region")
    a = ap.parse_args()

    rc = store_checks(A.MemoryAgentStore(), "memory")
    rc |= id_checks()
    rc |= render_checks()
    rc |= tenancy_checks()

    if a.dynamo:
        if not a.table:
            print("--dynamo needs --table", file=sys.stderr)
            return 2
        if a.region:
            import os
            os.environ.setdefault("AWS_DEFAULT_REGION", a.region)
        rc |= store_checks(A.DynamoAgentStore(a.table), "dynamodb")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
