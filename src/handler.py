"""
The API function.

Routing, path parameters and error shapes come from AWS Lambda Powertools'
``APIGatewayRestResolver``; the routes themselves come from ``routes.py``, which is
also what the OpenAPI document is rendered from. One table, two consumers — a
decorator per route would put the same 30 (eventually 116) paths in two places and
let them drift.

Every route answers a stub. What this exists to prove is the decision the whole
surface rests on: **the path prefix selects the dialect, and it is not stripped.**
The dialect appears in the response and in every log line, which is how, with two
façades over one event log, we can tell later whose client did what.
"""

from __future__ import annotations

import contextvars
import json

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.event_handler.middlewares import NextMiddleware

import agentstore
import paging
import project
import render
import routes as R
import sessions as SS
import store
import tenancy
import tokens
import tools as T
import vault as V
import worker

VERSION = "0.1.0"

#: Built once per warm environment. Both hold no secret and no tenant state — the
#: tenant arrives with every request and is passed in, never remembered.
AGENTS = agentstore.from_environment()
VAULTS = V.from_environment()
STORE = store.from_environment()
#: The caller's provider key, for the length of one request: the worker woken by
#: it calls the model with it. A context variable rather than an argument, because
#: the wake happens deep inside the session service, which has no business
#: holding a credential.
PROVIDER_KEY: contextvars.ContextVar = contextvars.ContextVar("provider_key", default=None)

SESSIONS = SS.Sessions(
    STORE, AGENTS,
    wake=lambda tenant, session: worker.wake(tenant, session,
                                             store=STORE, agents=AGENTS,
                                             provider_key=PROVIDER_KEY.get(),
                                             vaults=VAULTS),
    vaults=VAULTS)

logger = Logger(service="yait-agents-api")
app = APIGatewayRestResolver()

# The two SDKs disagree about how a credential is sent: the Anthropic SDK uses
# x-api-key, the OpenAI SDK uses Authorization: Bearer. One client key, both
# conventions accepted.
_KEY_HEADERS = ("x-api-key", "authorization")


def _dialect() -> str:
    """The first path segment, which is the whole of how a façade is chosen."""
    parts = (app.current_event.path or "").strip("/").split("/")
    return parts[0] if parts and parts[0] in R.DIALECTS else render.ANTHROPIC


def _json(status: int, body: dict) -> Response:
    return Response(status_code=status, content_type="application/json",
                    body=json.dumps(body, ensure_ascii=False))


def _error(status: int, type_: str, message: str,
           dialect: str | None = None) -> Response:
    # Each façade renders its own envelope: a client parses one of them, and
    # OpenAI's has `error.param` and `error.code` where Anthropic's has neither.
    return _json(status, render.error(status, type_, message,
                                     dialect or _dialect()))


# ── routes, registered from the table ──────────────────────────────────────

def _rule(dialect: str, path: str) -> str:
    """`{session_id}` in the OpenAPI document is `<session_id>` to the resolver."""
    return R.full_path(dialect, path).replace("{", "<").replace("}", ">")


def _make(dialect: str, op_id: str):
    def route(**params):
        body = {
            "ok": True,
            "dialect": dialect,
            "operation_id": op_id,
            "method": app.current_event.http_method,
            "path": app.current_event.path,
            "path_parameters": params,
            "stub": True,
            "version": VERSION,
        }
        if op_id in R.NOT_SERVABLE_BY_APIGW:
            # Answered so the surface is complete, and marked so nobody mistakes
            # the stub for something that could become real on this transport.
            body["note"] = ("A real stream needs a Function URL in RESPONSE_STREAM "
                            "mode; API Gateway buffers and times out at about 30 s.")
        return body

    route.__name__ = op_id
    return route


# ── agents: the first routes with behaviour behind them ────────────────────
#
# A session cannot be created without an agent — the Anthropic dialect takes no
# inline configuration — so this is where the stubs start coming out.

def _principal() -> tenancy.Principal:
    """
    Set by the middleware through Powertools' request context.

    The event object itself is immutable, and that is a good thing: the request is
    what arrived, not a place to stash our conclusions. The context is cleared after
    every request, so nothing a warm environment holds can leak into the next one.
    """
    return app.context["principal"]


def _body() -> dict:
    raw = app.current_event.json_body if app.current_event.body else None
    return raw if isinstance(raw, dict) else {}


def _agent_fields(body: dict, dialect: str) -> dict:
    """A request body to internal field names, per dialect."""
    fields = {k: body.get(k) for k in agentstore.FIELDS if k in body}
    if dialect == render.ANTHROPIC and "system" in body:
        # The dialect calls it `system`; we store `instructions`.
        fields["instructions"] = body["system"]
    return fields


def _list_agents():
    who = _principal()
    records = AGENTS.list(who.tenant)
    return _json(200, render.agent_list(records, _dialect()))


def _create_agent():
    who, dialect = _principal(), _dialect()
    try:
        fields = _agent_fields(_body(), dialect)
        T.check(fields)                 # a tool no step could serve fails here
        record = AGENTS.create(who.tenant, **fields)
    except ValueError as exc:
        # A field we do not support fails by name rather than being ignored.
        return _error(400, "invalid_request_error", str(exc), dialect)
    return _json(201, render.agent(record, dialect))


def _get_agent(agent_id: str):
    who, dialect = _principal(), _dialect()
    try:
        record = AGENTS.get(who.tenant, agent_id)
    except agentstore.NotFound:
        return _error(404, "not_found_error", f"No agent {agent_id}", dialect)
    return _json(200, render.agent(record, dialect))


def _update_agent(agent_id: str):
    who, dialect = _principal(), _dialect()
    try:
        fields = _agent_fields(_body(), dialect)
        T.check({**AGENTS.get(who.tenant, agent_id), **fields})
        record = AGENTS.update(who.tenant, agent_id, **fields)
    except agentstore.NotFound:
        return _error(404, "not_found_error", f"No agent {agent_id}", dialect)
    except agentstore.Conflict as exc:
        return _error(409, "conflict_error", str(exc), dialect)
    except ValueError as exc:
        return _error(400, "invalid_request_error", str(exc), dialect)
    return _json(200, render.agent(record, dialect))


def _delete_agent(agent_id: str):
    """OpenAI has delete where Anthropic has archive; both end the agent."""
    who, dialect = _principal(), _dialect()
    try:
        AGENTS.archive(who.tenant, agent_id)
    except agentstore.NotFound:
        return _error(404, "not_found_error", f"No agent {agent_id}", dialect)
    return _json(200, {"id": agent_id, "object": "agent.deleted", "deleted": True})


def _archive_agent(agent_id: str):
    who, dialect = _principal(), _dialect()
    try:
        record = AGENTS.archive(who.tenant, agent_id)
    except agentstore.NotFound:
        return _error(404, "not_found_error", f"No agent {agent_id}", dialect)
    return _json(200, render.agent(record, dialect))


def _agent_versions(agent_id: str):
    who = _principal()
    try:
        versions = AGENTS.versions(who.tenant, agent_id)
        # The agent as it was at each version: the dialect returns full agents here.
        records = [AGENTS.get(who.tenant, agent_id, v["version"]) for v in versions]
    except agentstore.NotFound:
        return _error(404, "not_found_error", f"No agent {agent_id}")
    return _json(200, render.agent_versions(records))


# ── sessions, events, turns, items ─────────────────────────────────────────

def _q(name: str, default=None):
    return app.current_event.get_query_string_value(name, default)


def _limit(default: int, ceiling: int = 1000) -> int:
    try:
        return max(1, min(int(_q("limit", default)), ceiling))
    except (TypeError, ValueError):
        return default


def _session_error(exc: Exception):
    """One mapping from the service's errors to status codes, for every route."""
    dialect = _dialect()
    if isinstance(exc, (SS.Invalid, paging.BadCursor)):
        return _error(400, "invalid_request_error", str(exc), dialect)
    if isinstance(exc, SS.NotFound):
        return _error(404, "not_found_error", f"No {exc}", dialect)
    if isinstance(exc, SS.Conflict):
        # The Anthropic original answers a request the session's state forbids —
        # archiving while running, writing to an archived session — with 400
        # invalid_request_error, not 409. OpenAI's document declares 409.
        if dialect == render.ANTHROPIC:
            return _error(400, "invalid_request_error", str(exc), dialect)
        return _error(409, "conflict_error", str(exc), dialect)
    raise exc


def _rendered_session(tenant: str, record: dict) -> dict:
    return render.session(record, _dialect(), SESSIONS.agent_of(tenant, record))


def _seq_key(ev: dict) -> str:
    # Zero-padded so a string sort is a numeric one.
    return f"{ev['seq']:012d}"


def _list_sessions():
    who = _principal()
    records = SESSIONS.list(who.tenant)
    try:
        if _dialect() == render.ANTHROPIC:
            page = paging.anthropic(records, key=lambda r: r["session"],
                                    limit=_limit(20), order=_q("order", "desc"),
                                    page=_q("page"))
            return _json(200, render.anthropic_list(
                [_rendered_session(who.tenant, r) for r in page["data"]], page))
        page = paging.openai(records, key=lambda r: r["session"],
                             id_of=lambda r: r["session"], limit=_limit(20),
                             order=_q("order", "desc"), after=_q("after"))
        return _json(200, render.openai_list(
            [_rendered_session(who.tenant, r) for r in page["data"]], page))
    except paging.BadCursor as exc:
        return _session_error(exc)


def _create_session():
    who = _principal()
    try:
        record, _ = SESSIONS.create(who.tenant, _dialect(), _body())
    except (SS.Invalid, SS.NotFound, SS.Conflict) as exc:
        return _session_error(exc)
    # A new session is 201 in the OpenAI dialect, 200 in the Anthropic one.
    return _json(201 if _dialect() == render.OPENAI else 200,
                 _rendered_session(who.tenant, record))


def _get_session(session_id: str):
    who = _principal()
    try:
        record = SESSIONS.get(who.tenant, session_id)
    except SS.NotFound as exc:
        return _session_error(exc)
    return _json(200, _rendered_session(who.tenant, record))


def _archive_session(session_id: str):
    who = _principal()
    try:
        record = SESSIONS.archive(who.tenant, session_id)
    except (SS.NotFound, SS.Conflict) as exc:
        return _session_error(exc)
    return _json(200, _rendered_session(who.tenant, record))


def _update_session(session_id: str):
    who = _principal()
    try:
        record = SESSIONS.update(who.tenant, session_id, _dialect(), _body())
    except (SS.Invalid, SS.NotFound, SS.Conflict) as exc:
        return _session_error(exc)
    return _json(200, _rendered_session(who.tenant, record))


def _delete_session(session_id: str):
    who = _principal()
    try:
        SESSIONS.delete(who.tenant, session_id)
    except SS.NotFound as exc:
        # OpenAI's delete is idempotent: deleting a session already deleted answers
        # as the first time did — while we still know it was, which is until the
        # purge takes its tombstone. Anthropic's answers 404, as ours always did.
        if not (_dialect() == render.OPENAI and SESSIONS.was_deleted(who.tenant,
                                                                     session_id)):
            return _session_error(exc)
    except SS.Conflict as exc:
        return _session_error(exc)
    # The same deletion behind both; only the confirmation differs.
    if _dialect() == render.ANTHROPIC:
        return _json(200, {"id": session_id, "type": "session_deleted"})
    return _json(200, {"id": session_id, "object": "agent.session.deleted",
                       "deleted": True})


def _list_events(session_id: str):
    who, dialect = _principal(), _dialect()
    try:
        log = SESSIONS.events(who.tenant, session_id)
        processed = project.processed_times(log)
        context = {}
        if dialect == render.OPENAI:
            # Its events carry the session, and the turn as it stood at each one.
            record = SESSIONS.get(who.tenant, session_id)
            context = {
                "session": _rendered_session(who.tenant, record),
                "timeline": project.turn_timeline(
                    log, session_id=session_id, agent_id=record.get("agent_id"))}
        # Render first, then page: a page counts events the client can see, and
        # an internal event may render as zero dialect events or as several.
        visible = []
        anchors = _message_anchors(log) if dialect == render.ANTHROPIC else {}
        shown = render._with_mcp_items(log) if dialect == render.OPENAI else log
        for ev in shown:
            for n, out in enumerate(render.event(ev, dialect, session_id=session_id,
                                                 processed=processed, **context)):
                visible.append({"_key": anchors.get(ev["event_id"])
                                or f"{ev['seq']:012d}.{n}", **out})
        visible.sort(key=lambda e: e["_key"])
        if dialect == render.ANTHROPIC:
            page = paging.anthropic(visible, key=lambda e: e["_key"],
                                    limit=_limit(1000), order=_q("order", "asc"),
                                    page=_q("page"))
            return _json(200, render.anthropic_list(
                [{k: v for k, v in e.items() if k != "_key"} for e in page["data"]],
                page))
        page = paging.openai(visible, key=lambda e: e["_key"],
                             id_of=lambda e: e["event_id"], limit=_limit(100),
                             order=_q("order", "asc"), after=_q("after"))
        return _json(200, render.openai_list(
            [{k: v for k, v in e.items() if k != "_key"} for e in page["data"]], page))
    except (SS.NotFound, paging.BadCursor) as exc:
        return _session_error(exc)


def _message_anchors(log: list[dict]) -> dict[str, str]:
    """
    Where each taken-up message stands in the Anthropic event list: just before the
    model request that took it up — as the original lists it, when it is processed
    rather than when it arrived. A message queued behind another turn then reads
    after that turn's answer, which is the order the conversation happened in. A
    message not yet taken up keeps its place at the end.

    The key sorts before the request's own ("!" is below every digit), and keeps the
    messages of one turn in the order they came.
    """
    first_request: dict = {}
    for ev in log:
        if ev.get("type") == "span.model_request_start" and ev.get("turn_id") \
                and ev["turn_id"] not in first_request:
            first_request[ev["turn_id"]] = ev["seq"]
    return {ev["event_id"]: f"{first_request[ev['turn_id']]:012d}.!{ev['seq']:012d}"
            for ev in log
            if ev.get("type") == "user.message" and ev.get("turn_id") in first_request}


def _send_events(session_id: str):
    who, dialect = _principal(), _dialect()
    try:
        committed = SESSIONS.send(who.tenant, session_id, dialect,
                                  _body().get("events"))
    except (SS.Invalid, SS.NotFound, SS.Conflict) as exc:
        return _session_error(exc)
    if dialect == render.OPENAI:
        # Accepted, and nothing more to say: the dialect's answer has no body. What
        # the events became is read from the event list, the items or the turns.
        return Response(status_code=202, content_type="application/json", body="")
    return _json(200, {"data": render.events(committed, dialect, session_id=session_id)})


def _list_items(session_id: str):
    who = _principal()
    try:
        log = SESSIONS.events(who.tenant, session_id)
        records = project.items(log)
        page = paging.openai(records, key=lambda i: f"{i['seq']:012d}:{i['id']}",
                             id_of=lambda i: i["id"], limit=_limit(100),
                             order=_q("order", "desc"), after=_q("after"))
    except (SS.NotFound, paging.BadCursor) as exc:
        return _session_error(exc)
    return _json(200, render.openai_list([render.item(i) for i in page["data"]], page))


def _turns_of(session_id: str):
    who = _principal()
    record = SESSIONS.get(who.tenant, session_id)
    return project.turns(SESSIONS.events(who.tenant, session_id),
                         session_id=session_id, agent_id=record.get("agent_id"))


def _list_turns(session_id: str):
    try:
        records = _turns_of(session_id)
        page = paging.openai(records, key=lambda t: f"{t['first_seq']:012d}",
                             id_of=lambda t: t["turn_id"], limit=_limit(100),
                             order=_q("order", "desc"), after=_q("after"))
    except (SS.NotFound, paging.BadCursor) as exc:
        return _session_error(exc)
    return _json(200, render.openai_list([render.turn(t) for t in page["data"]], page))


def _get_turn(session_id: str, turn_id: str):
    try:
        records = _turns_of(session_id)
    except SS.NotFound as exc:
        return _session_error(exc)
    found = next((t for t in records if t["turn_id"] == turn_id), None)
    if found is None:
        return _error(404, "not_found_error", f"No turn {turn_id}")
    return _json(200, render.turn(found))


# ── vaults ─────────────────────────────────────────────────────────────────

def _vault_error(exc: Exception, what: str):
    dialect = _dialect()
    if isinstance(exc, V.NotFound):
        return _error(404, "not_found_error", f"No {what} {exc}", dialect)
    if isinstance(exc, V.NotConfigured):
        return _error(503, "api_error", str(exc), dialect)
    if isinstance(exc, V.Archived):
        return _error(400 if dialect == render.ANTHROPIC else 409,
                      "invalid_request_error", str(exc), dialect)
    return _error(400, "invalid_request_error", str(exc), dialect)


_VAULT_FAILURES = (V.NotFound, V.Invalid, V.NotConfigured, V.Archived)


def _include_archived() -> bool:
    params = app.current_event.query_string_parameters or {}
    return (params.get("include_archived") or "").lower() == "true" \
        or params.get("status") in ("archived", "all")


def _list_vaults():
    who, dialect = _principal(), _dialect()
    found = VAULTS.list(who.tenant, include_archived=_include_archived())
    return _json(200, render.resource_list([render.vault(v, dialect) for v in found],
                                           dialect))


def _create_vault():
    who, dialect, body = _principal(), _dialect(), _body()
    name = body.get("display_name" if dialect == render.ANTHROPIC else "name")
    unknown = sorted(set(body) - {"display_name" if dialect == render.ANTHROPIC
                                  else "name", "metadata"})
    if unknown:
        return _error(400, "invalid_request_error",
                      f"unknown field(s): {', '.join(unknown)}", dialect)
    if dialect == render.ANTHROPIC and not name:
        return _error(400, "invalid_request_error", "display_name is required", dialect)
    try:
        record = VAULTS.create(who.tenant, name=name, metadata=body.get("metadata"))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")
    return _json(201 if dialect == render.OPENAI else 200, render.vault(record, dialect))


def _get_vault(vault_id: str):
    who, dialect = _principal(), _dialect()
    try:
        return _json(200, render.vault(VAULTS.get(who.tenant, vault_id), dialect))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")


def _update_vault(vault_id: str):
    who, dialect, body = _principal(), _dialect(), _body()
    unknown = sorted(set(body) - {"display_name", "metadata"})
    if unknown:
        return _error(400, "invalid_request_error",
                      f"unknown field(s): {', '.join(unknown)}", dialect)
    try:
        record = VAULTS.update(who.tenant, vault_id, name=body.get("display_name"),
                               metadata=body.get("metadata"))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")
    return _json(200, render.vault(record, dialect))


def _archive_vault(vault_id: str):
    who, dialect = _principal(), _dialect()
    try:
        return _json(200, render.vault(VAULTS.archive(who.tenant, vault_id), dialect))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")


def _delete_vault(vault_id: str):
    who, dialect = _principal(), _dialect()
    try:
        VAULTS.delete(who.tenant, vault_id)
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")
    if dialect == render.ANTHROPIC:
        return _json(200, {"id": vault_id, "type": "vault_deleted"})
    return _json(200, {"id": vault_id, "object": "vault.deleted", "deleted": True})


def _list_credentials(vault_id: str):
    who, dialect = _principal(), _dialect()
    try:
        found = VAULTS.credentials(who.tenant, vault_id,
                                   include_archived=_include_archived())
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")
    return _json(200, render.resource_list(
        [render.credential(c, dialect) for c in found], dialect))


def _create_credential(vault_id: str):
    who, dialect, body = _principal(), _dialect(), _body()
    name_field = "display_name" if dialect == render.ANTHROPIC else "name"
    unknown = sorted(set(body) - {name_field, "auth", "metadata"})
    if unknown:
        return _error(400, "invalid_request_error",
                      f"unknown field(s): {', '.join(unknown)}", dialect)
    try:
        record = VAULTS.add(who.tenant, vault_id, auth=body.get("auth"),
                            name=body.get(name_field), metadata=body.get("metadata"))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "vault")
    return _json(201 if dialect == render.OPENAI else 200,
                 render.credential(record, dialect))


def _get_credential(vault_id: str, credential_id: str):
    who, dialect = _principal(), _dialect()
    try:
        return _json(200, render.credential(
            VAULTS.credential(who.tenant, vault_id, credential_id), dialect))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "credential")


def _update_credential(vault_id: str, credential_id: str):
    """Anthropic's update, and OpenAI's rotate: the same change, two spellings."""
    who, dialect, body = _principal(), _dialect(), _body()
    allowed = ({"display_name", "auth", "metadata"} if dialect == render.ANTHROPIC
               else {"auth", "metadata"})
    unknown = sorted(set(body) - allowed)
    if unknown:
        return _error(400, "invalid_request_error",
                      f"unknown field(s): {', '.join(unknown)}", dialect)
    try:
        record = VAULTS.rotate(who.tenant, vault_id, credential_id,
                               auth=body.get("auth"), name=body.get("display_name"),
                               metadata=body.get("metadata"),
                               # Anthropic patches metadata per key; OpenAI replaces it.
                               patch_metadata=dialect == render.ANTHROPIC)
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "credential")
    return _json(200, render.credential(record, dialect))


def _archive_credential(vault_id: str, credential_id: str):
    who, dialect = _principal(), _dialect()
    try:
        return _json(200, render.credential(
            VAULTS.archive_credential(who.tenant, vault_id, credential_id), dialect))
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "credential")


def _delete_credential(vault_id: str, credential_id: str):
    who, dialect = _principal(), _dialect()
    try:
        VAULTS.delete_credential(who.tenant, vault_id, credential_id)
    except _VAULT_FAILURES as exc:
        return _vault_error(exc, "credential")
    if dialect == render.ANTHROPIC:
        return _json(200, {"id": credential_id, "type": "vault_credential_deleted"})
    return _json(200, {"id": credential_id, "object": "vault.credential.deleted",
                       "deleted": True})


#: Keyed by operation id, and the keys must be exactly R.IMPLEMENTED — the set
#: lives in routes.py because the tests read it too, and a route that is real in
#: one place and stubbed in the other is the kind of drift nothing notices.
REAL = {
    "anthropicListAgents": _list_agents,
    "anthropicCreateAgent": _create_agent,
    "anthropicGetAgent": _get_agent,
    "anthropicUpdateAgent": _update_agent,
    "anthropicArchiveAgent": _archive_agent,
    "anthropicListAgentVersions": _agent_versions,
    "openaiListAgents": _list_agents,
    "openaiCreateAgent": _create_agent,
    "openaiGetAgent": _get_agent,
    "openaiUpdateAgent": _update_agent,
    "openaiDeleteAgent": _delete_agent,
    "anthropicListSessions": _list_sessions,
    "anthropicCreateSession": _create_session,
    "anthropicGetSession": _get_session,
    "anthropicArchiveSession": _archive_session,
    "anthropicListEvents": _list_events,
    "anthropicSendEvents": _send_events,
    "openaiListSessions": _list_sessions,
    "openaiCreateSession": _create_session,
    "openaiGetSession": _get_session,
    "openaiDeleteSession": _delete_session,
    "openaiUpdateSession": _update_session,
    "anthropicUpdateSession": _update_session,
    "anthropicDeleteSession": _delete_session,
    "openaiListEvents": _list_events,
    "openaiSendEvents": _send_events,
    "openaiListItems": _list_items,
    "openaiListTurns": _list_turns,
    "openaiGetTurn": _get_turn,
    "anthropicListVaults": _list_vaults, "openaiListVaults": _list_vaults,
    "anthropicCreateVault": _create_vault, "openaiCreateVault": _create_vault,
    "anthropicGetVault": _get_vault, "openaiGetVault": _get_vault,
    "anthropicUpdateVault": _update_vault,
    "anthropicArchiveVault": _archive_vault,
    "anthropicDeleteVault": _delete_vault, "openaiDeleteVault": _delete_vault,
    "anthropicListCredentials": _list_credentials,
    "openaiListCredentials": _list_credentials,
    "anthropicCreateCredential": _create_credential,
    "openaiCreateCredential": _create_credential,
    "anthropicGetCredential": _get_credential, "openaiGetCredential": _get_credential,
    "anthropicUpdateCredential": _update_credential,
    "openaiRotateCredential": _update_credential,
    "anthropicArchiveCredential": _archive_credential,
    "anthropicDeleteCredential": _delete_credential,
    "openaiDeleteCredential": _delete_credential,
}

assert set(REAL) == set(R.IMPLEMENTED), (
    "REAL and routes.IMPLEMENTED disagree: "
    f"{sorted(set(REAL) ^ set(R.IMPLEMENTED))}")

for _d, _method, _path, _op_id, _ in R.ROUTES:
    _fn = REAL.get(_op_id) or _make(_d, _op_id)
    app.route(_rule(_d, _path), method=[_method])(_fn)


# ── credentials ────────────────────────────────────────────────────────────

def require_credential(current: APIGatewayRestResolver,
                       next_middleware: NextMiddleware) -> Response:
    """
    Resolve the caller to a tenant, or refuse.

    Presence is no longer enough: now that the API stores things, a credential has
    to *be* one of ours. A deployment with no key configured refuses everything,
    because an API that accepts any string because nobody configured it is worse
    than one that is down.
    """
    try:
        who = tenancy.resolve(
            current.current_event.headers,
            (current.current_event.raw_event.get("requestContext") or {}).get("authorizer"))
    except tenancy.NotConfigured as exc:
        # 503, not 401: this is the deployment's fault, not the client's, and a 401
        # would send someone looking for a better key.
        return _error(503, "api_error", str(exc))
    except tenancy.Unauthorized:
        return _error(
            401, "authentication_error",
            "Send this installation's token as X-Yait-Key — with the SDKs, "
            "default_headers={'X-Yait-Key': ...}. The owner issues tokens with "
            "scripts/keys.py. Your provider key goes where it always does, and is "
            "passed on to the model.",
        )
    current.append_context(principal=who)
    token = PROVIDER_KEY.set(who.provider_key)
    try:
        response = next_middleware(current)
    finally:
        PROVIDER_KEY.reset(token)

    # Logged here rather than in `handler`, because this is the one place that sees
    # both the caller and the answer. The key's *name* goes in the line, never the
    # key: this is shipped to CloudWatch.
    logger.info("request", extra={
        "method": current.current_event.http_method,
        "path": current.current_event.path,
        "status": response.status_code,
        "dialect": _dialect(),
        "tenant": who.tenant,
        "key_id": who.key_id,
    })
    return response


app.use(middlewares=[require_credential])


# ── the shapes the resolver does not give us ───────────────────────────────

@app.not_found
def not_found(_exc) -> Response:
    """
    A 404 in our envelope, and a 405 where one is owed.

    The resolver answers an unmatched method with 404, which is a different thing
    from an unknown path and the difference is worth keeping: a client that gets
    404 for `DELETE /v1/agents` will look for a typo in the path rather than in the
    verb. The route table already knows every path, so the distinction is a lookup.
    """
    method = app.current_event.http_method
    path = (app.current_event.path or "").rstrip("/") or "/"
    for dialect, route_method, route_path, _, _ in R.ROUTES:
        if _matches(R.full_path(dialect, route_path), path):
            if route_method.upper() != method.upper():
                return _error(405, "invalid_request_error",
                              f"{method} is not allowed on {path}")
    return _error(404, "not_found_error", f"No route for {method} {path}")


def _matches(template: str, path: str) -> bool:
    want = template.strip("/").split("/")
    got = path.strip("/").split("/")
    if len(want) != len(got):
        return False
    return all(w.startswith("{") or w == g for w, g in zip(want, got))


@logger.inject_lambda_context(log_event=False)
def handler(event, context=None):
    # API Gateway calls this same function as its request authorizer: one function
    # that already reads the agents table, rather than another with a role and a
    # policy of its own.
    if tokens.is_authorizer_event(event):
        return tokens.authorize(event)
    # The prefix is deliberately NOT stripped: `path` carries it, and its first
    # segment is what selects the dialect. Requests that never reach a route — an
    # unknown path, a refused credential — are logged by the resolver's own error
    # paths rather than here.
    return app.resolve(event, context)
