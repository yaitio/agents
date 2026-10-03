#!/usr/bin/env python3
"""
Layer one, OpenAI dialect: every response validates against OpenAI's own schema.

OpenAI's Python SDK has no Agents API yet, so the reference is their published
OpenAPI document — the `/agents` part of it, cut out and kept in
`tests/reference/openai-agents.json` with where it came from. Each response is
checked against the schema the document declares for that operation *and status
code*, so a 200 where the original answers 201 fails as surely as a missing field.
Most of those schemas are `additionalProperties: false`, so an extra field fails too.

The requests are held to the document as well: a scenario that sent what no real
client could send would prove nothing about what real clients get.

**Known gaps are listed below, not hidden** — see `conformance_harness.py`.

    python3 tests/conformance_openai.py                        # a local server, echo
    python3 tests/conformance_openai.py --base-url https://… --key …
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import conformance_harness as harness

ROOT = harness.ROOT
SPEC = json.loads((ROOT / "tests" / "reference" / "openai-agents.json").read_text())
REGISTRY = Registry().with_resource("urn:openai", Resource(SPEC, DRAFT202012))

#: Checks we know fail, and why. Remove an entry when its gap is closed — the suite
#: insists, by failing when an expected failure passes.
KNOWN_GAPS: dict[str, str] = {
    "events.list": "the original's GET /events is a live text/event-stream; API "
                   "Gateway cannot stream, so we answer a JSON page of the same "
                   "events — each of which is checked below as events.each",
}


# ── validation ─────────────────────────────────────────────────────────────

def against(schema: dict, body) -> list[str]:
    """Every way `body` fails `schema`, most specific first."""
    validator = Draft202012Validator(schema, registry=REGISTRY)
    return [explain(e) for e in validator.iter_errors(body)]


def explain(error) -> str:
    """
    One failure, said about the branch that was meant.

    A oneOf reports "not valid under any of the given schemas" about the whole
    object. The document marks most of its unions with a discriminator — the `type`
    field names the branch — so the useful failure is that branch's own, not the
    one a heuristic finds closest.
    """
    while error.context:
        branch = _discriminated(error)
        inside = [e for e in error.context
                  if branch is None or e.relative_schema_path[0] == branch]
        if branch is not None and not inside:
            break                               # the named branch had no complaint
        if not inside:
            where = "/".join(str(p) for p in error.absolute_path) or "(root)"
            kind = error.instance.get("type") if isinstance(error.instance, dict) else None
            return f"{where}: no branch of the union is type {kind!r}"
        error = best_match(inside)
    where = "/".join(str(p) for p in error.absolute_path) or "(root)"
    return f"{where}: {error.message[:160]}"


def _discriminated(error) -> int | None:
    schema, instance = error.schema, error.instance
    mapping = (schema.get("discriminator") or {}).get("mapping") or {}
    field = (schema.get("discriminator") or {}).get("propertyName")
    if not mapping or not isinstance(instance, dict) or field not in instance:
        return None
    target = mapping.get(instance[field], "").split("/")[-1]
    for n, option in enumerate(schema.get(error.validator) or []):
        if option.get("$ref", "").split("/")[-1] == target:
            return n
    return None


def named(name: str) -> dict:
    return {"$ref": f"urn:openai#/components/schemas/{name}"}


def _pointer(schema: dict) -> dict:
    """A schema from the document, with its local refs pointed at the registry."""
    return json.loads(json.dumps(schema).replace('"#/components/', '"urn:openai#/components/'))


def operation(method: str, path: str) -> dict:
    template = re.sub(r"^/openai/v1", "", path.split("?")[0])
    for pattern, ops in SPEC["paths"].items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", pattern), template) \
                and method.lower() in ops:
            return ops[method.lower()]
    raise KeyError(f"{method} {path} is not in the reference")


def request_errors(method: str, path: str, body: dict) -> list[str]:
    schema = operation(method, path)["requestBody"]["content"]["application/json"]["schema"]
    return [f"request {e}" for e in against(_pointer(schema), body)]


def response_errors(method: str, path: str, status: int, body) -> list[str]:
    """The declared response for this operation and status, and the body against it."""
    responses = operation(method, path)["responses"]
    success = sorted(code for code in responses if code.startswith("2"))
    if str(status) not in responses:
        return [f"status {status}; the original declares {', '.join(sorted(responses))}"]
    if str(status).startswith("2") and str(status) != success[0]:
        return [f"status {status}; the original answers {success[0]}"]
    content = responses[str(status)].get("content") or {}
    if "application/json" not in content:
        if not content:
            return []                           # declared bodiless; a body is ignored
        return [f"the original answers {', '.join(content)}, not a JSON document"]
    return against(_pointer(content["application/json"]["schema"]), body)


# ── the client ─────────────────────────────────────────────────────────────

class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body: dict | None = None,
             *, dialect: str = "openai") -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, method=method)
        headers = (harness.openai_headers(yait_key=self.key,
                                          provider_key=harness.provider_key_for("gpt-6-luna"))
                   if dialect == "openai" else {"X-Yait-Key": self.key})
        for name, value in headers.items():
            req.add_header(name, value)
        if body is not None:
            req.add_header("content-type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=30) as reply:
                status, raw = reply.status, reply.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {"__not_json__": raw[:200].decode(errors="replace")}


# ── the scenario ───────────────────────────────────────────────────────────

def run(base: str, key: str) -> int:
    out = harness.Outcomes(KNOWN_GAPS)
    c = Client(base, key)
    V1 = "/openai/v1"

    def exchange(name: str, method: str, path: str, body: dict | None = None,
                 extra: list[str] | None = None) -> dict:
        errors = request_errors(method, path, body) if body is not None else []
        status, reply = c.call(method, V1 + path, body)
        errors += response_errors(method, path, status, reply)
        out.record(name, errors + (extra or []))
        return reply

    agent = exchange("agents.create", "POST", "/agents",
                     {"model": "gpt-6-luna", "name": "conformance",
                      "instructions": "Be brief."})
    agent_id = agent.get("id", "missing")
    exchange("agents.retrieve", "GET", f"/agents/{agent_id}")
    exchange("agents.update", "POST", f"/agents/{agent_id}",
             {"instructions": "Be briefer."})
    exchange("agents.list", "GET", "/agents")

    session = exchange("sessions.create", "POST", "/agents/sessions",
                       {"agent_id": agent_id, "environment": {"type": "none"},
                        "input": "hello"})
    session_id = session.get("id", "missing")
    exchange("sessions.retrieve", "GET", f"/agents/sessions/{session_id}")

    exchange("events.send", "POST", f"/agents/sessions/{session_id}/events",
             {"events": [{"type": "agent.session.input.message",
                          "input": [{"role": "user", "type": "message",
                                     "content": [{"type": "input_text",
                                                  "text": "and again"}]}]}]})

    for _ in range(120):
        if c.call("GET", f"{V1}/agents/sessions/{session_id}")[1].get("status") == "idle":
            break
        time.sleep(0.5)

    status, page = c.call("GET", f"{V1}/agents/sessions/{session_id}/events")
    out.record("events.list", response_errors(
        "GET", f"/agents/sessions/{session_id}/events", status, page))
    errors = []
    for n, ev in enumerate(page.get("data") or []):
        errors += [f"data[{n}] {ev.get('type')}: {e}"
                   for e in against(named("SessionEvent"), ev)]
    if not page.get("data"):
        errors.append("no events to check")
    out.record("events.each", errors)

    exchange("items.list", "GET", f"/agents/sessions/{session_id}/items")
    turns = exchange("turns.list", "GET", f"/agents/sessions/{session_id}/turns")
    turn_id = ((turns.get("data") or [{}])[0]).get("id", "missing")
    exchange("turns.retrieve", "GET", f"/agents/sessions/{session_id}/turns/{turn_id}")

    # A session opened through the other façade, by the same tenant, is listed here
    # too — the case a scenario that uses one dialect never produces.
    _, other = c.call("POST", "/anthropic/v1/sessions",
                      {"agent": agent_id, "environment_id": "env_none"},
                      dialect="anthropic")
    listed = exchange("sessions.list", "GET", "/agents/sessions")
    if other.get("id") not in [s.get("id") for s in listed.get("data") or []]:
        out.rows[-1] = (out.rows[-1][0], "FAIL",
                        "the session opened through the Anthropic façade is not listed")

    c.call("POST", f"{V1}/agents/sessions/{session_id}", {"metadata": {"old": "1"}})
    updated = exchange("sessions.update", "POST", f"/agents/sessions/{session_id}",
                       {"metadata": {"new": "2"}, "agent": {"model": "claude-sonnet-5"}})
    semantics = []
    if updated.get("metadata") != {"new": "2"}:
        semantics.append(f"metadata replaces; it became {updated.get('metadata')}")
    if (updated.get("agent") or {}).get("model") != "claude-sonnet-5":
        semantics.append(f"the model is {(updated.get('agent') or {}).get('model')!r}")
    if semantics:
        out.rows[-1] = (out.rows[-1][0], "FAIL", "; ".join(semantics))

    # OpenAI has no budget to set, but a session can have one — set through the
    # other façade — and its turn then fails with the dialect's own code.
    _, broke = c.call("POST", "/anthropic/v1/sessions",
                      {"agent": agent_id, "environment_id": "env_none",
                       "budget": {"type": "limit",
                                  "max_list_cost": {"amount": "0", "currency": "USD"}}},
                      dialect="anthropic")
    broke_id = broke.get("id", "missing")
    c.call("POST", f"{V1}/agents/sessions/{broke_id}/events",
           {"events": [{"type": "agent.session.input.message",
                        "input": [{"role": "user", "type": "message",
                                   "content": [{"type": "input_text", "text": "hi"}]}]}]})
    for _ in range(120):
        if c.call("GET", f"{V1}/agents/sessions/{broke_id}")[1].get("status") == "idle":
            break
        time.sleep(0.5)
    status, turns_page = c.call("GET", f"{V1}/agents/sessions/{broke_id}/turns")
    errors = response_errors("GET", f"/agents/sessions/{broke_id}/turns", status, turns_page)
    codes = [((t.get("error") or {}).get("code")) for t in turns_page.get("data") or []]
    if codes != ["session_budget_exceeded"]:
        errors.append(f"turn error codes were {codes}")
    _, evs = c.call("GET", f"{V1}/agents/sessions/{broke_id}/events")
    for ev in evs.get("data") or []:
        errors += [f"{ev.get('type')}: {e}" for e in against(named("SessionEvent"), ev)]
    if "agent.session.turn.failed" not in [e.get("type") for e in evs.get("data") or []]:
        errors.append("no turn.failed event")
    out.record("sessions.budget_exceeded", errors)

    exchange("errors.not_found", "GET", "/agents/sessions/sess_does_not_exist")

    # Vaults: the resource, its credentials, a session that names one — and no
    # secret in any answer.
    secret = f"conformance-secret-{time.time_ns()}"
    shown = []
    vault = exchange("vaults.create", "POST", "/vaults",
                     {"name": "conformance", "metadata": {"who": "conformance"}})
    vault_id = vault.get("id", "missing")
    exchange("vaults.retrieve", "GET", f"/vaults/{vault_id}")
    exchange("vaults.list", "GET", "/vaults")
    shown.append(exchange(
        "vaults.credentials.create", "POST", f"/vaults/{vault_id}/credentials",
        {"name": "exa", "auth": {"type": "static_bearer",
                                 "mcp_server_url": "https://mcp.exa.ai/mcp",
                                 "token": secret}}))
    cred_id = shown[-1].get("id", "missing")
    shown.append(exchange(
        "vaults.credentials.create_oauth", "POST", f"/vaults/{vault_id}/credentials",
        {"name": "oauth", "auth": {
            "type": "mcp_oauth", "mcp_server_url": "https://mcp.example.com",
            "access_token": secret, "expires_at": "2030-01-01T00:00:00Z",
            "refresh": {"client_id": "cid", "refresh_token": secret,
                        "token_endpoint": "https://auth.example.com/token",
                        "token_endpoint_auth": {"type": "client_secret_post",
                                                "client_secret": secret}}}}))
    shown.append(exchange("vaults.credentials.retrieve", "GET",
                          f"/vaults/{vault_id}/credentials/{cred_id}"))
    shown.append(exchange(
        "vaults.credentials.rotate", "POST", f"/vaults/{vault_id}/credentials/{cred_id}",
        {"auth": {"type": "static_bearer", "token": secret + "-2"}}))
    shown.append(exchange("vaults.credentials.list", "GET",
                          f"/vaults/{vault_id}/credentials"))
    out.record("vaults.secrets_never_returned",
               ["a secret came back"] if secret in json.dumps(shown) else [])
    fresh = c.call("POST", f"{V1}/agents", {"model": "gpt-6-luna", "name": "vaulted"})[1]
    with_vault = exchange("sessions.create_with_vault", "POST", "/agents/sessions",
                          {"agent_id": fresh.get("id"), "environment": {"type": "none"},
                           "input": "hello", "vault_ids": [vault_id]})
    if with_vault.get("vault_ids") != [vault_id]:
        out.rows[-1] = (out.rows[-1][0], "FAIL",
                        f"vault_ids came back as {with_vault.get('vault_ids')!r}")
    # Tools, on the test MCP server — local only, as in the other dialect: the
    # echo model calls a tool on cue. The turn's events, its mcp_call item and a
    # refused credential, each against the published document.
    if harness.is_local(base):
        with harness.serve_mcp_faults() as mcp_url:
            def tool_turn(token, text):
                vids = []
                if token:
                    _, v = c.call("POST", f"{V1}/vaults", {"name": "tools"})
                    c.call("POST", f"{V1}/vaults/{v['id']}/credentials",
                           {"name": "faults", "auth": {"type": "static_bearer",
                                                       "mcp_server_url": mcp_url,
                                                       "token": token}})
                    vids = [v["id"]]
                a = exchange(f"tools.agent_with_mcp{'' if token else '.no_vault'}",
                             "POST", "/agents", {
                    "model": "gpt-6-luna", "name": "tools",
                    "tools": [{"type": "mcp", "server_label": "faults",
                               "transport": {"type": "http", "server_url": mcp_url},
                               "allowed_tools": ["lookup_order", "whoami"]}]})
                _, sess = c.call("POST", f"{V1}/agents/sessions", {
                    "agent_id": a["id"], "environment": {"type": "none"},
                    "input": text, "vault_ids": vids})
                sid = sess.get("id", "missing")
                for _ in range(120):
                    if c.call("GET", f"{V1}/agents/sessions/{sid}")[1].get("status") in ("idle", "failed"):
                        break
                    time.sleep(0.25)
                return sid

            sid = tool_turn(harness.MCP_TOKENS["alice"], 'use lookup_order {"order_id": "5521"}')
            status, evs = c.call("GET", f"{V1}/agents/sessions/{sid}/events")
            errors = [f"{e.get('type')}: {x}" for e in evs.get("data") or []
                      for x in against(named("SessionEvent"), e)]
            items = exchange("tools.items", "GET", f"/agents/sessions/{sid}/items")
            calls = [i for i in items.get("data") or [] if i.get("type") == "mcp_call"]
            if not calls or calls[0].get("status") != "completed" \
                    or calls[0].get("server_label") != "faults":
                errors.append(f"no completed mcp_call from faults: {calls}")
            if not any(e.get("type") == "agent.session.turn.item.done"
                       and (e.get("item") or {}).get("type") == "mcp_call"
                       for e in evs.get("data") or []):
                errors.append("no item.done for the mcp_call")
            out.record("tools.call_events", errors)

            sid = tool_turn(None, "use whoami {}")
            status, turns = c.call("GET", f"{V1}/agents/sessions/{sid}/turns")
            errors = response_errors("GET", f"/agents/sessions/{sid}/turns", status, turns)
            codes = [(t.get("error") or {}).get("code") for t in turns.get("data") or []]
            if codes != ["authentication_error"]:
                errors.append(f"turn error codes were {codes}")
            out.record("tools.no_credential", errors)

    exchange("vaults.credentials.delete", "DELETE",
             f"/vaults/{vault_id}/credentials/{cred_id}")
    exchange("vaults.delete", "DELETE", f"/vaults/{vault_id}")

    # The paths the echo backend never takes — a failed turn and a cancelled one —
    # built as logs, rendered the way the handler renders them, and held to the
    # same schemas as a good turn.
    sys.path.insert(0, str(ROOT / "src"))
    import events as E
    import project
    import render
    start = E.event(E.MODEL_REQUEST_START, turn_id="t1")
    log = [
        dict(E.event(E.SESSION_CREATED), seq=0),
        dict(E.event(E.USER_MESSAGE, turn_id="t1", item_id="item_1", actor="user",
                     text="hello"), seq=1),
        dict(E.event(E.STATUS_RUNNING, turn_id="t1"), seq=2),
        dict(start, seq=3),
        dict(E.event(E.STEP_FAILED, turn_id="t1", reason="top up the account",
                     error_type="billing"), seq=4),
        dict(E.event(E.MODEL_REQUEST_END, turn_id="t1", error=True,
                     model_request_start_id=start["event_id"]), seq=5),
        dict(E.event(E.STATUS_IDLE, turn_id="t1", stop_reason="retries_exhausted"), seq=6),
        dict(E.event(E.USER_MESSAGE, turn_id="t2", item_id="item_2", actor="user",
                     text="never mind"), seq=7),
        dict(E.event(E.USER_INTERRUPT, turn_id="t2"), seq=8),
    ]
    turns = {t["turn_id"]: render.turn(t)
             for t in project.turns(log, session_id="s", agent_id="a")}
    rendered = render.events(
        log, render.OPENAI, session_id="s",
        timeline=project.turn_timeline(log, session_id="s", agent_id="a"),
        session=session if "id" in session else None)
    errors = []
    for ev in rendered:
        errors += [f"{ev.get('type')}: {e}" for e in against(named("SessionEvent"), ev)]
    for turn in turns.values():
        errors += [f"turn {turn['id']}: {e}" for e in against(named("TurnResource"), turn)]
    kinds = {ev.get("type") for ev in rendered}
    for wanted in ("agent.session.created", "agent.session.turn.created",
                   "agent.session.in_progress", "agent.session.turn.in_progress",
                   "agent.session.turn.failed", "agent.session.idle",
                   "agent.session.turn.cancelled"):
        if wanted not in kinds:
            errors.append(f"{wanted} was not rendered, so not checked")
    if (turns["t1"].get("error") or {}).get("code") != "credit_balance_exhausted":
        errors.append(f"a billing failure reads {turns['t1'].get('error')}")
    out.record("events.failed_and_cancelled", errors)

    exchange("sessions.delete", "DELETE", f"/agents/sessions/{session_id}")
    # Gone from the API at once — from the OpenAI façade and from the other one.
    gone = [c.call("GET", f"{V1}/agents/sessions/{session_id}")[0],
            c.call("GET", f"/anthropic/v1/sessions/{session_id}", dialect="anthropic")[0]]
    listed = [x.get("id") for x in c.call("GET", f"{V1}/agents/sessions")[1].get("data") or []]
    out.record("sessions.delete.gone",
               ([] if gone == [404, 404] else [f"reads after delete answered {gone}"])
               + (["still listed"] if session_id in listed else []))
    status, reply = c.call("DELETE", f"{V1}/agents/sessions/{session_id}")
    out.record("sessions.delete.again",
               response_errors("DELETE", f"/agents/sessions/{session_id}", status, reply)
               + ([] if status == 200 else [f"a second delete answered {status}; the "
                                             "original answers 200 — it is idempotent"]))
    exchange("agents.delete", "DELETE", f"/agents/{agent_id}")

    return out.report()


if __name__ == "__main__":
    raise SystemExit(harness.main(run, "/openai/v1/agents"))
