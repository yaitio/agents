#!/usr/bin/env python3
"""
Layer one, Anthropic dialect: every response validates as the official SDK's type.

The official `anthropic` SDK drives the API the way a client would, and the raw body
of each response is validated — strictly — against the SDK's own model for that call.
The SDK alone is not enough: it parses leniently, building an object from whatever
arrives, so a missing required field would pass unnoticed until some client read it.
Strict validation of the raw body is what makes this a test.

**Known gaps are listed below, not hidden** — see `conformance_harness.py`.

    python3 tests/conformance_anthropic.py                     # a local server, echo
    python3 tests/conformance_anthropic.py --base-url https://… --key …
"""

from __future__ import annotations

import json
import sys
import time
import typing
import urllib.request

import anthropic
from pydantic import TypeAdapter, ValidationError

import conformance_harness as harness

ROOT = harness.ROOT

#: Checks we know fail, and why. Remove an entry when its gap is closed — the suite
#: insists, by failing when an expected failure passes.
KNOWN_GAPS: dict[str, str] = {}


def validate(type_, body) -> list[str]:
    """Strict validation of a raw body against an SDK type. Returns the errors."""
    try:
        TypeAdapter(type_).validate_python(body)
        return []
    except ValidationError as exc:
        return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def returned(method):
    """The SDK's declared return type for a resource method."""
    return typing.get_type_hints(method)["return"]


def page(method, body) -> list[str]:
    """A list endpoint: the page envelope, and every item as the page's item type."""
    page_type = returned(method)
    errors = validate(page_type, body)
    # A parametrised pydantic generic is its own subclass; the item type lives in its
    # generic metadata, not where typing.get_args looks.
    meta = getattr(page_type, "__pydantic_generic_metadata__", {}) or {}
    item_type = (meta.get("args") or typing.get_args(page_type))[0]
    for n, item in enumerate(body.get("data") or []):
        errors += [f"data[{n}].{e}" for e in validate(item_type, item)]
    return errors


def raw(call):
    """Run a `with_raw_response` call, returning the parsed JSON body."""
    return call().http_response.json()


# ── the scenario ───────────────────────────────────────────────────────────

def run(base: str, key: str) -> int:
    out = harness.Outcomes(KNOWN_GAPS)
    c = harness.anthropic_client(
        base, yait_key=key, max_retries=0,
        provider_key=harness.provider_key_for("claude-haiku-4-5-20251001"))
    agents, sessions = c.beta.agents, c.beta.sessions

    body = raw(lambda: agents.with_raw_response.create(
        model="claude-haiku-4-5-20251001", name="conformance", system="Be brief."))
    out.record("agents.create", validate(returned(agents.create), body))
    agent_id = body.get("id", "missing")

    body = raw(lambda: agents.with_raw_response.retrieve(agent_id))
    out.record("agents.retrieve", validate(returned(agents.retrieve), body))

    body = raw(lambda: agents.with_raw_response.update(agent_id, system="Be briefer."))
    out.record("agents.update", validate(returned(agents.update), body))

    body = raw(lambda: agents.with_raw_response.list())
    out.record("agents.list", page(agents.list, body))

    body = raw(lambda: agents.versions.with_raw_response.list(agent_id))
    out.record("agents.versions.list", page(agents.versions.list, body))

    body = raw(lambda: sessions.with_raw_response.create(
        agent=agent_id, environment_id="env_none", title="conformance"))
    out.record("sessions.create", validate(returned(sessions.create), body))
    session_id = body.get("id", "missing")

    body = raw(lambda: sessions.with_raw_response.retrieve(session_id))
    out.record("sessions.retrieve", validate(returned(sessions.retrieve), body))

    # A session opened through the other façade, by the same tenant. It is listed
    # here too, so it has to read as an Anthropic session — the case a scenario
    # that uses one dialect never produces.
    other = urllib.request.Request(
        f"{base.rstrip('/')}/openai/v1/agents/sessions", method="POST",
        headers={**harness.openai_headers(yait_key=key),
                 "content-type": "application/json"},
        data=json.dumps({"agent_id": agent_id, "environment": {"type": "none"},
                         "input": "hello from the other dialect"}).encode())
    with urllib.request.urlopen(other, timeout=30) as reply:
        other_id = json.loads(reply.read()).get("id")

    body = raw(lambda: sessions.with_raw_response.list())
    errors = page(sessions.list, body)
    if other_id not in [x.get("id") for x in body.get("data") or []]:
        errors.append("the session opened through the OpenAI façade is not listed")
    out.record("sessions.list", errors)

    body = raw(lambda: sessions.events.with_raw_response.send(
        session_id, events=[{"type": "user.message",
                             "content": [{"type": "text", "text": "hello"}]}]))
    out.record("sessions.events.send", validate(returned(sessions.events.send), body))

    for _ in range(120):
        if sessions.retrieve(session_id).status == "idle":
            break
        time.sleep(0.5)

    body = raw(lambda: sessions.events.with_raw_response.list(session_id))
    out.record("sessions.events.list", page(sessions.events.list, body))

    sessions.update(session_id, metadata={"keep": "1", "drop": "2"})
    body = raw(lambda: sessions.with_raw_response.update(
        session_id, title="renamed", metadata={"drop": None, "add": "3"},
        agent={"tools": []}))
    errors = validate(returned(sessions.update), body)
    if body.get("title") != "renamed":
        errors.append(f"title is {body.get('title')!r}")
    if body.get("metadata") != {"keep": "1", "add": "3"}:
        errors.append(f"metadata is a patch; it became {body.get('metadata')}")
    out.record("sessions.update", errors)

    # A budget of nothing: the turn is refused before any request is issued — the
    # same here and against a deployment, and free in both.
    broke = sessions.with_raw_response.create(
        agent=agent_id, environment_id="env_none",
        budget={"type": "limit", "max_list_cost": {"amount": "0", "currency": "USD"}})
    body = broke.http_response.json()
    errors = validate(returned(sessions.create), body)
    if (body.get("budget") or {}).get("max_list_cost", {}).get("amount") != "0":
        errors.append(f"the budget reads back as {body.get('budget')}")
    broke_id = body.get("id", "missing")
    sessions.events.send(broke_id, events=[
        {"type": "user.message", "content": [{"type": "text", "text": "hello"}]}])
    for _ in range(120):
        if sessions.retrieve(broke_id).status == "idle":
            break
        time.sleep(0.5)
    listed = raw(lambda: sessions.events.with_raw_response.list(broke_id))
    errors += [f"events: {e}" for e in page(sessions.events.list, listed)]
    kinds = [(e.get("type"), (e.get("stop_reason") or {}).get("type"))
             for e in listed.get("data") or []]
    if ("session.status_idle", "budget_reached") not in kinds:
        errors.append(f"no idle with budget_reached: {kinds}")
    if any(k == "span.model_request_start" for k, _ in kinds):
        errors.append("a model request was issued past the budget")
    out.record("sessions.budget_reached", errors)

    body = raw(lambda: sessions.with_raw_response.archive(session_id))
    out.record("sessions.archive", validate(returned(sessions.archive), body))

    body = raw(lambda: sessions.with_raw_response.delete(session_id))
    out.record("sessions.delete", validate(returned(sessions.delete), body))
    problems = []
    for name, read in (("retrieve", lambda: sessions.retrieve(session_id)),
                       ("delete again", lambda: sessions.delete(session_id))):
        try:
            read()
            problems.append(f"{name} after delete succeeded")
        except anthropic.NotFoundError:
            pass
        except anthropic.APIStatusError as exc:
            problems.append(f"{name} after delete raised {type(exc).__name__}")
    if session_id in [x.id for x in sessions.list().data]:
        problems.append("still listed")
    out.record("sessions.delete.gone", problems)


    body = raw(lambda: agents.with_raw_response.archive(agent_id))
    out.record("agents.archive", validate(returned(agents.archive), body))

    # Errors: the SDK maps a status to an exception class and reads the envelope.
    try:
        sessions.retrieve("sess_does_not_exist")
        out.record("errors.not_found", ["no error raised"])
    except anthropic.NotFoundError as exc:
        envelope = exc.body if isinstance(exc.body, dict) else {}
        problems = []
        if envelope.get("type") != "error":
            problems.append(f"envelope type is {envelope.get('type')!r}")
        if (envelope.get("error") or {}).get("type") != "not_found_error":
            problems.append(f"error.type is {(envelope.get('error') or {}).get('type')!r}")
        out.record("errors.not_found", problems)
    except anthropic.APIStatusError as exc:
        out.record("errors.not_found", [f"raised {type(exc).__name__} ({exc.status_code})"])

    # Vaults: the resource, its credentials, and a session that names one. No
    # secret may come back in any of these answers.
    vaults = c.beta.vaults
    secret = f"conformance-secret-{time.time_ns()}"
    body = raw(lambda: vaults.with_raw_response.create(
        display_name="conformance", metadata={"who": "conformance"}))
    out.record("vaults.create", validate(returned(vaults.create), body))
    vault_id = body.get("id", "missing")
    body = raw(lambda: vaults.with_raw_response.retrieve(vault_id))
    out.record("vaults.retrieve", validate(returned(vaults.retrieve), body))
    body = raw(lambda: vaults.with_raw_response.update(vault_id, display_name="renamed"))
    out.record("vaults.update", validate(returned(vaults.update), body))
    body = raw(lambda: vaults.with_raw_response.list())
    out.record("vaults.list", page(vaults.list, body))

    creds = vaults.credentials
    shown = []
    body = raw(lambda: creds.with_raw_response.create(
        vault_id, display_name="exa",
        auth={"type": "static_bearer", "mcp_server_url": "https://mcp.exa.ai/mcp",
              "token": secret}))
    shown.append(body)
    out.record("vaults.credentials.create", validate(returned(creds.create), body))
    cred_id = body.get("id", "missing")
    body = raw(lambda: creds.with_raw_response.create(
        vault_id, auth={"type": "mcp_oauth", "mcp_server_url": "https://mcp.example.com",
                        "access_token": secret, "expires_at": "2030-01-01T00:00:00Z",
                        "refresh": {"client_id": "cid", "refresh_token": secret,
                                    "token_endpoint": "https://auth.example.com/token",
                                    "token_endpoint_auth": {"type": "client_secret_basic",
                                                            "client_secret": secret}}}))
    shown.append(body)
    out.record("vaults.credentials.create_oauth", validate(returned(creds.create), body))
    body = raw(lambda: creds.with_raw_response.retrieve(cred_id, vault_id=vault_id))
    shown.append(body)
    out.record("vaults.credentials.retrieve", validate(returned(creds.retrieve), body))
    body = raw(lambda: creds.with_raw_response.update(
        cred_id, vault_id=vault_id,
        auth={"type": "static_bearer", "mcp_server_url": "https://mcp.exa.ai/mcp",
              "token": secret + "-2"}))
    shown.append(body)
    out.record("vaults.credentials.update", validate(returned(creds.update), body))
    body = raw(lambda: creds.with_raw_response.list(vault_id))
    shown.append(body)
    out.record("vaults.credentials.list", page(creds.list, body))
    out.record("vaults.secrets_never_returned",
               ["a secret came back"] if secret in json.dumps(shown) else [])

    fresh = agents.create(model="claude-haiku-4-5-20251001", name="with a vault").id
    body = raw(lambda: sessions.with_raw_response.create(
        agent=fresh, environment_id="env_none", vault_ids=[vault_id]))
    errors = validate(returned(sessions.create), body)
    if body.get("vault_ids") != [vault_id]:
        errors.append(f"vault_ids came back as {body.get('vault_ids')!r}")
    out.record("sessions.create_with_vault", errors)

    body = raw(lambda: creds.with_raw_response.archive(cred_id, vault_id=vault_id))
    out.record("vaults.credentials.archive", validate(returned(creds.archive), body))
    body = raw(lambda: creds.with_raw_response.delete(cred_id, vault_id=vault_id))
    out.record("vaults.credentials.delete", validate(returned(creds.delete), body))
    body = raw(lambda: vaults.with_raw_response.archive(vault_id))
    out.record("vaults.archive", validate(returned(vaults.archive), body))
    body = raw(lambda: vaults.with_raw_response.delete(vault_id))
    out.record("vaults.delete", validate(returned(vaults.delete), body))

    # Tools, on the test MCP server. Local only: the echo model is what calls a tool
    # on cue, and a deployment runs real models. Every event of a tool turn — the
    # call, its result, a failed call, a refused credential — as the SDK's type.
    if harness.is_local(base):
        union = returned(sessions.events.list).__pydantic_generic_metadata__["args"][0]
        with harness.serve_mcp_faults() as mcp_url:
            def tool_turn(token, text):
                vids = []
                if token:
                    v = vaults.create(display_name="tools")
                    creds.create(v.id, auth={"type": "static_bearer",
                                             "mcp_server_url": mcp_url, "token": token})
                    vids = [v.id]
                made = raw(lambda: agents.with_raw_response.create(
                    model="claude-haiku-4-5-20251001", name="tools",
                    mcp_servers=[{"type": "url", "name": "faults", "url": mcp_url}],
                    tools=[{"type": "mcp_toolset", "mcp_server_name": "faults",
                            "configs": [{"name": "slow", "enabled": False}]}]))
                out.record("tools.agent_with_mcp", validate(returned(agents.create), made))
                a = agents.retrieve(made["id"])
                s = sessions.create(agent=a.id, environment_id="env_none", vault_ids=vids)
                sessions.events.send(s.id, events=[{"type": "user.message",
                                                    "content": [{"type": "text", "text": text}]}])
                for _ in range(120):
                    evs = raw(lambda: sessions.events.with_raw_response.list(s.id))["data"]
                    if any(e.get("type") == "session.status_idle" for e in evs):
                        return evs
                    time.sleep(0.25)
                return evs

            def check(name, evs, want_kinds, extra=None):
                errors = [f"{e.get('type')}: {x}" for e in evs for x in validate(union, e)]
                kinds = [e.get("type") for e in evs]
                errors += [f"no {k}" for k in want_kinds if k not in kinds]
                errors += extra(evs) if extra else []
                out.record(name, errors)

            alice = harness.MCP_TOKENS["alice"]
            check("tools.call", tool_turn(alice, 'use lookup_order {"order_id": "5521"}'),
                  ["agent.mcp_tool_use", "agent.mcp_tool_result", "agent.message"],
                  lambda evs: [] if any(e.get("type") == "agent.mcp_tool_use"
                                        and e.get("mcp_server_name") == "faults"
                                        and e.get("input") == {"order_id": "5521"}
                                        for e in evs) else ["the call does not name its server and input"])
            check("tools.own_credential", tool_turn(alice, "use whoami {}"),
                  ["agent.mcp_tool_result"],
                  lambda evs: [] if any(e.get("type") == "agent.mcp_tool_result"
                                        and e["content"][0]["text"] == "alice"
                                        for e in evs) else ["the call did not carry the vault's token"])
            check("tools.failed_call", tool_turn(alice, 'use fail {"message": "boom"}'),
                  ["agent.mcp_tool_result", "agent.message"],
                  lambda evs: [] if any(e.get("type") == "agent.mcp_tool_result"
                                        and e.get("is_error") for e in evs)
                  else ["a failed call is not marked is_error"])
            check("tools.no_credential", tool_turn(None, "use whoami {}"),
                  ["session.error"],
                  lambda evs: [] if any(e.get("type") == "session.error"
                                        and e["error"]["type"] == "mcp_authentication_failed_error"
                                        and e["error"].get("mcp_server_name") == "faults"
                                        for e in evs) else ["no mcp_authentication_failed_error"])
            check("tools.no_secret_in_events",
                  tool_turn(alice, "use add {\"a\": 1, \"b\": 2}"), [],
                  lambda evs: ["the token is in an event"] if alice in json.dumps(evs) else [])

    # The failure path, which the echo backend never takes. Built as a log and
    # rendered directly, then validated against the same union the SDK reads events
    # with — so a failed turn is held to the dialect as strictly as a good one.
    sys.path.insert(0, str(ROOT / "src"))
    import events as E
    import render
    start = E.event(E.MODEL_REQUEST_START, turn_id="t")
    failed_log = [
        dict(E.event(E.STATUS_RUNNING, turn_id="t"), seq=1),
        dict(start, seq=2),
        dict(E.event(E.STEP_FAILED, turn_id="t", reason="top up the account",
                     error_type="billing"), seq=3),
        dict(E.event(E.MODEL_REQUEST_END, turn_id="t", error=True,
                     model_request_start_id=start["event_id"]), seq=4),
        dict(E.event(E.STATUS_IDLE, turn_id="t", stop_reason="retries_exhausted"), seq=5),
    ]
    rendered = render.events(failed_log, render.ANTHROPIC, session_id="s")
    union = returned(sessions.events.list).__pydantic_generic_metadata__["args"][0]
    errors = []
    for n, ev in enumerate(rendered):
        errors += [f"{ev.get('type')}: {e}" for e in validate(union, ev)]
    kinds = [e.get("type") for e in rendered]
    if kinds != ["session.status_running", "span.model_request_start", "session.error",
                 "span.model_request_end", "session.status_idle"]:
        errors.append(f"order was {kinds}")
    out.record("events.failed_turn", errors)

    return out.report()


if __name__ == "__main__":
    raise SystemExit(harness.main(run, "/anthropic/v1/agents"))
