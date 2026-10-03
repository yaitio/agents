"""
The route table — the single source the OpenAPI document is rendered from.

Two dialects live behind two path prefixes because 29 method+path pairs collide
between them; see docs/api.md. The prefix is part of the path and is *not*
stripped, so its first segment selects the dialect in the handler and appears in
every log line.

Only the core agent surface is here. The full surfaces are 74 operations
(Anthropic) and 42 (OpenAI); this table is meant to grow, which is why the
document is rendered from it rather than hand-written.
"""

from __future__ import annotations

#: Dialect name -> path prefix. The name reaches the handler through the path.
DIALECTS = {
    "anthropic": "/anthropic/v1",
    "openai":    "/openai/v1",
}

#: base_url a client configures for each dialect. The asymmetry is deliberate
#: and is the documentation trap of docs/api.md: the Anthropic SDK's own paths
#: already start with /v1, the OpenAI SDK carries /v1 inside base_url.
BASE_URL_HINT = {
    "anthropic": "{host}/anthropic",
    "openai":    "{host}/openai/v1",
}

# (dialect, method, path after the prefix, operationId, summary)
ROUTES: list[tuple[str, str, str, str, str]] = [
    # ── Anthropic: agents ────────────────────────────────────────────────
    ("anthropic", "GET",    "/agents",                     "anthropicListAgents",      "List agents"),
    ("anthropic", "POST",   "/agents",                     "anthropicCreateAgent",     "Create an agent"),
    ("anthropic", "GET",    "/agents/{agent_id}",          "anthropicGetAgent",        "Get an agent"),
    ("anthropic", "POST",   "/agents/{agent_id}",          "anthropicUpdateAgent",     "Update an agent"),
    ("anthropic", "POST",   "/agents/{agent_id}/archive",  "anthropicArchiveAgent",    "Archive an agent"),
    ("anthropic", "GET",    "/agents/{agent_id}/versions", "anthropicListAgentVersions", "List agent versions"),
    # ── Anthropic: sessions ──────────────────────────────────────────────
    ("anthropic", "GET",    "/sessions",                          "anthropicListSessions",   "List sessions"),
    ("anthropic", "POST",   "/sessions",                          "anthropicCreateSession",  "Create a session"),
    ("anthropic", "GET",    "/sessions/{session_id}",             "anthropicGetSession",     "Get a session"),
    ("anthropic", "POST",   "/sessions/{session_id}",             "anthropicUpdateSession",  "Update a session"),
    ("anthropic", "DELETE", "/sessions/{session_id}",             "anthropicDeleteSession",  "Delete a session"),
    ("anthropic", "POST",   "/sessions/{session_id}/archive",     "anthropicArchiveSession", "Archive a session"),
    # ── Anthropic: events ────────────────────────────────────────────────
    ("anthropic", "GET",    "/sessions/{session_id}/events",        "anthropicListEvents",  "List events"),
    ("anthropic", "POST",   "/sessions/{session_id}/events",        "anthropicSendEvents",  "Send events"),
    ("anthropic", "GET",    "/sessions/{session_id}/events/stream", "anthropicStreamEvents", "Stream events (SSE)"),

    # ── Anthropic: vaults ────────────────────────────────────────────────
    ("anthropic", "GET",    "/vaults",                             "anthropicListVaults",      "List vaults"),
    ("anthropic", "POST",   "/vaults",                             "anthropicCreateVault",     "Create a vault"),
    ("anthropic", "GET",    "/vaults/{vault_id}",                  "anthropicGetVault",        "Get a vault"),
    ("anthropic", "POST",   "/vaults/{vault_id}",                  "anthropicUpdateVault",     "Update a vault"),
    ("anthropic", "DELETE", "/vaults/{vault_id}",                  "anthropicDeleteVault",     "Delete a vault"),
    ("anthropic", "POST",   "/vaults/{vault_id}/archive",          "anthropicArchiveVault",    "Archive a vault"),
    ("anthropic", "GET",    "/vaults/{vault_id}/credentials",      "anthropicListCredentials", "List credentials"),
    ("anthropic", "POST",   "/vaults/{vault_id}/credentials",      "anthropicCreateCredential", "Create a credential"),
    ("anthropic", "GET",    "/vaults/{vault_id}/credentials/{credential_id}",         "anthropicGetCredential",     "Get a credential"),
    ("anthropic", "POST",   "/vaults/{vault_id}/credentials/{credential_id}",         "anthropicUpdateCredential",  "Update a credential"),
    ("anthropic", "DELETE", "/vaults/{vault_id}/credentials/{credential_id}",         "anthropicDeleteCredential",  "Delete a credential"),
    ("anthropic", "POST",   "/vaults/{vault_id}/credentials/{credential_id}/archive", "anthropicArchiveCredential", "Archive a credential"),
    # ── OpenAI: agents ───────────────────────────────────────────────────
    ("openai", "GET",    "/agents",              "openaiListAgents",   "List agents"),
    ("openai", "POST",   "/agents",              "openaiCreateAgent",  "Create an agent"),
    ("openai", "GET",    "/agents/{agent_id}",   "openaiGetAgent",     "Get an agent"),
    ("openai", "POST",   "/agents/{agent_id}",   "openaiUpdateAgent",  "Update an agent"),
    ("openai", "DELETE", "/agents/{agent_id}",   "openaiDeleteAgent",  "Delete an agent"),
    # ── OpenAI: sessions (nested under /agents) ──────────────────────────
    ("openai", "GET",    "/agents/sessions",                "openaiListSessions",   "List sessions"),
    ("openai", "POST",   "/agents/sessions",                "openaiCreateSession",  "Create a session"),
    ("openai", "GET",    "/agents/sessions/{session_id}",   "openaiGetSession",     "Get a session"),
    ("openai", "POST",   "/agents/sessions/{session_id}",   "openaiUpdateSession",  "Update a session"),
    ("openai", "DELETE", "/agents/sessions/{session_id}",   "openaiDeleteSession",  "Delete a session"),
    # ── OpenAI: vaults ───────────────────────────────────────────────────
    ("openai", "GET",    "/vaults",                        "openaiListVaults",      "List vaults"),
    ("openai", "POST",   "/vaults",                        "openaiCreateVault",     "Create a vault"),
    ("openai", "GET",    "/vaults/{vault_id}",             "openaiGetVault",        "Get a vault"),
    ("openai", "DELETE", "/vaults/{vault_id}",             "openaiDeleteVault",     "Delete a vault"),
    ("openai", "GET",    "/vaults/{vault_id}/credentials", "openaiListCredentials", "List credentials"),
    ("openai", "POST",   "/vaults/{vault_id}/credentials", "openaiCreateCredential", "Create a credential"),
    ("openai", "GET",    "/vaults/{vault_id}/credentials/{credential_id}",    "openaiGetCredential",    "Get a credential"),
    ("openai", "POST",   "/vaults/{vault_id}/credentials/{credential_id}",    "openaiRotateCredential", "Rotate a credential"),
    ("openai", "DELETE", "/vaults/{vault_id}/credentials/{credential_id}",    "openaiDeleteCredential", "Delete a credential"),
    # ── OpenAI: events, items, turns ─────────────────────────────────────
    ("openai", "GET",    "/agents/sessions/{session_id}/events",           "openaiListEvents", "List events"),
    ("openai", "POST",   "/agents/sessions/{session_id}/events",           "openaiSendEvents", "Send events"),
    ("openai", "GET",    "/agents/sessions/{session_id}/items",            "openaiListItems",  "List items"),
    ("openai", "GET",    "/agents/sessions/{session_id}/turns",            "openaiListTurns",  "List turns"),
    ("openai", "GET",    "/agents/sessions/{session_id}/turns/{turn_id}",  "openaiGetTurn",    "Get a turn"),
]

#: Operations with behaviour behind them. Everything else still answers a stub.
#: One place, read by the handler to pick a function and by the tests to know what
#: to assert — otherwise the stub test would start failing as behaviour arrives and
#: someone would loosen it instead of narrowing it.
IMPLEMENTED = frozenset({
    "anthropicListAgents", "anthropicCreateAgent", "anthropicGetAgent",
    "anthropicUpdateAgent", "anthropicArchiveAgent", "anthropicListAgentVersions",
    "openaiListAgents", "openaiCreateAgent", "openaiGetAgent",
    "openaiUpdateAgent", "openaiDeleteAgent",
    "anthropicListSessions", "anthropicCreateSession", "anthropicGetSession",
    "anthropicUpdateSession", "anthropicArchiveSession", "anthropicDeleteSession",
    "anthropicListEvents", "anthropicSendEvents",
    "openaiListSessions", "openaiCreateSession", "openaiGetSession",
    "openaiUpdateSession", "openaiDeleteSession",
    "openaiListEvents", "openaiSendEvents", "openaiListItems", "openaiListTurns",
    "openaiGetTurn",
    "anthropicListVaults", "anthropicCreateVault", "anthropicGetVault",
    "anthropicUpdateVault", "anthropicDeleteVault", "anthropicArchiveVault",
    "anthropicListCredentials", "anthropicCreateCredential", "anthropicGetCredential",
    "anthropicUpdateCredential", "anthropicDeleteCredential", "anthropicArchiveCredential",
    "openaiListVaults", "openaiCreateVault", "openaiGetVault", "openaiDeleteVault",
    "openaiListCredentials", "openaiCreateCredential", "openaiGetCredential",
    "openaiRotateCredential", "openaiDeleteCredential",
})

#: Routes that a real deployment cannot serve through API Gateway, because the
#: gateway buffers responses and times an integration out at about 30 s. They
#: are declared so the surface is complete and answer a stub today; a real
#: stream needs a Lambda Function URL in RESPONSE_STREAM mode behind the same
#: CloudFront distribution. See docs/architecture.md.
NOT_SERVABLE_BY_APIGW = {"anthropicStreamEvents"}


def full_path(dialect: str, path: str) -> str:
    """The path as it appears in the OpenAPI document, prefix included."""
    return DIALECTS[dialect] + path


def path_params(path: str) -> list[str]:
    """Names of the `{...}` segments in a path, in order."""
    return [p[1:-1] for p in path.split("/") if p.startswith("{") and p.endswith("}")]
