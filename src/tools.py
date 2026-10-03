"""
An agent's tools, as the step worker offers them to the model and runs them.

Version one serves MCP servers — every tool the agent has is one of theirs. Both
dialects describe the same thing in their own shape:

* Anthropic: `mcp_servers: [{"type": "url", "name", "url"}]`, and in `tools` an
  `{"type": "mcp_toolset", "mcp_server_name", "default_config", "configs"}` per
  server the agent may use — a server with no toolset is not offered.
* OpenAI: in `tools`, `{"type": "mcp", "server_label", "transport": {"type":
  "http", "server_url"}, "allowed_tools"}`.

Both become `Server(name, url, allowed)`. The connection is aichain's MCP client;
the credential is the one the session's vaults hold for that server's address
(vault.py), sent as a bearer token and never shown to the model.

Any other kind of tool — a client-side custom or function tool, the built-in
toolset — is refused by name when the step starts, rather than silently left out.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

#: How long a server may take to connect and list its tools, and one call to run.
#: Connecting is given 30 s: in a fresh Lambda the MCP client's first connection
#: starts its own machinery, and 15 s was seen to run out on a server that answers.
#: A call longer than the worker's reserve would be killed with the invocation,
#: leaving an outcome nobody knows; a named timeout is a result the model can use.
CONNECT_TIMEOUT_S = float(os.environ.get("YAIT_MCP_CONNECT_TIMEOUT_S", "30"))
CALL_TIMEOUT_S = float(os.environ.get("YAIT_TOOL_TIMEOUT_S", "120"))

#: The most of one result that is kept and shown to the model. A result is an
#: event, and an event is one DynamoDB item of at most 400 KB; a megabyte the
#: model reads is also a quarter of a million tokens someone pays for.
MAX_RESULT_CHARS = int(os.environ.get("YAIT_TOOL_RESULT_CHARS", "65536"))


class Unsupported(ValueError):
    """The agent has a tool this deployment does not serve."""


class ServerFailed(Exception):
    """An MCP server could not be reached, or refused the credential."""

    def __init__(self, message: str, kind: str, server: str) -> None:
        super().__init__(message)
        self.kind = kind            # events.FAILURES: mcp_authentication, mcp_connection
        self.server = server


@dataclass
class Server:
    name: str
    url: str
    allowed: frozenset | None       # None: every tool the server offers


def servers(agent: dict) -> list[Server]:
    """The MCP servers an agent may use, from either dialect's configuration."""
    declared = {s.get("name"): s.get("url")
                for s in agent.get("mcp_servers") or [] if isinstance(s, dict)}
    found: list[Server] = []
    for tool in agent.get("tools") or []:
        kind = (tool or {}).get("type")
        if kind == "mcp_toolset":
            name = tool.get("mcp_server_name")
            if name not in declared:
                raise Unsupported(f"mcp_toolset names {name!r}, which is not in mcp_servers")
            default = (tool.get("default_config") or {}).get("enabled", True)
            configs = {c.get("name"): c.get("enabled", True)
                       for c in tool.get("configs") or []}
            if default is False:
                allowed = frozenset(n for n, on in configs.items() if on)
            elif any(on is False for on in configs.values()):
                allowed = _Except(frozenset(n for n, on in configs.items() if on is False))
            else:
                allowed = None
            found.append(Server(name, declared[name], allowed))
        elif kind == "mcp":
            transport = tool.get("transport") or {}
            if transport.get("type") not in (None, "http"):
                raise Unsupported(f"MCP transport {transport.get('type')!r}: only a "
                                  "remote server over http is served")
            url = transport.get("server_url") or tool.get("server_url")
            allowed = tool.get("allowed_tools")
            found.append(Server(tool.get("server_label") or url, url,
                                frozenset(allowed) if allowed else None))
        else:
            raise Unsupported(
                f"a {kind!r} tool is not served by this deployment yet: only MCP "
                "servers are")
    return found


def check(agent: dict) -> None:
    """
    Refuse, when the agent is made, what a step could not serve: a tool of another
    kind, a toolset naming no server, or a policy that asks before a call — there
    is no approval to ask for yet, and running the call anyway would ignore it.
    """
    servers(agent)
    for tool in agent.get("tools") or []:
        policies = [(tool.get("default_config") or {}).get("permission_policy")] + [
            c.get("permission_policy") for c in tool.get("configs") or []]
        for policy in policies:
            kind = policy if isinstance(policy, str) else (policy or {}).get("type")
            if kind not in (None, "always_allow"):
                raise Unsupported(f"permission_policy {kind!r}: calls cannot wait for "
                                  "an approval yet, so only always_allow is served")


class _Except(frozenset):
    """Every tool but these."""


def _allowed(server: Server, name: str) -> bool:
    if server.allowed is None:
        return True
    if isinstance(server.allowed, _Except):
        return name not in server.allowed
    return name in server.allowed


#: What each MCP server offered, kept for the length of one worker run: a turn
#: asks on every step, and connecting to every server each time was most of the
#: time a tool turn spent outside the model. Keyed by the address and a hash of
#: the credential, so a list is only ever reused with the same key.
#:
#: **Never longer than the run.** The tools carry the caller's credential in their
#: headers, and a warm Lambda serves the next invocation — another tenant's — from
#: the same process. The worker calls `forget()` when a run ends; the TTL only
#: bounds a run that never ends.
LIST_TTL_S = float(os.environ.get("YAIT_MCP_LIST_TTL_S", "300"))
_listed: dict[tuple, tuple[float, list]] = {}


def forget() -> None:
    """Drop every listed tool, and the credentials they carry."""
    _listed.clear()


def _list(url: str, headers: dict) -> list:
    import hashlib
    import time
    from yait_aichain.tools import MCPTools
    key = (url, hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest())
    hit = _listed.get(key)
    if hit and time.monotonic() - hit[0] < LIST_TTL_S:
        return hit[1]
    found = MCPTools(url, headers=headers or None, timeout=CONNECT_TIMEOUT_S)
    _listed[key] = (time.monotonic(), found)
    return found


def discover(agent: dict, credential_for) -> dict:
    """
    `{tool name: (server, MCPTool)}` for every tool the agent may call.
    `credential_for(url)` is the vault's answer for a server, or None.
    """
    from yait_aichain.tools import MCPTools
    offered: dict[str, tuple[Server, Any]] = {}
    for server in servers(agent):
        credential = credential_for(server.url)
        headers = {}
        if credential:
            token = credential["secret"].get("token") or credential["secret"].get("access_token")
            headers["Authorization"] = f"Bearer {token}"
        try:
            found = _list(server.url, headers)
        except Exception as exc:  # noqa: BLE001 — the library wraps every failure
            # The library says "the server returned an error" and keeps the status;
            # asking once more is how a refusal is told from a server that is down.
            status = _status(server.url, headers)
            if status in (401, 403):
                raise ServerFailed(
                    f"MCP server {server.name!r} ({server.url}) answered {status}: "
                    + ("it refused the credential the session's vault holds for it"
                       if credential else "it wants a credential — put one for this "
                       "address in a vault the session names"),
                    "mcp_authentication", server.name) from exc
            import traceback
            traceback.print_exc()           # the whole of it, to the function's log
            raise ServerFailed(
                f"MCP server {server.name!r} ({server.url}) could not be used"
                + (f" (it answers {status} to a plain initialize)" if status else "")
                + f": {_cause(exc)}", "mcp_connection", server.name) from exc
        for tool in found:
            if not _allowed(server, tool.name):
                continue
            if tool.name in offered:
                raise Unsupported(f"two MCP servers offer a tool named {tool.name!r}: "
                                  f"{offered[tool.name][0].name} and {server.name}")
            offered[tool.name] = (server, tool)
    return offered


def _cause(exc: BaseException) -> str:
    """The innermost reason, through the library's wrappers and exception groups."""
    seen, reasons = set(), []
    stack = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        reasons.append(f"{type(e).__name__}: {e}"[:200])
        stack += list(getattr(e, "exceptions", []) or [])
        for nxt in (e.__cause__, e.__context__):
            if nxt is not None:
                stack.append(nxt)
    return " <- ".join(reasons[-3:])


def _status(url: str, headers: dict) -> int | None:
    """The HTTP status an MCP `initialize` gets from this server, if it answers."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "yait-agents", "version": "0.1"}}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json",
                 "accept": "application/json, text/event-stream",
                 "user-agent": "yait-agents/0.1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as reply:
            return reply.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 — not answering is the answer
        return None


def _bounded(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (text[:MAX_RESULT_CHARS]
            + f"\n\n[truncated: the result was {len(text):,} characters; the first "
              f"{MAX_RESULT_CHARS:,} are shown]")


def call(offered: dict, name: str, arguments: dict) -> tuple[str, bool]:
    """Run one call: (the result as text, whether it failed)."""
    if name not in offered:
        return f"There is no tool named {name!r}.", True
    _, tool = offered[name]
    try:
        result = tool.run(input=arguments or {}, options={"timeout": CALL_TIMEOUT_S})
    except Exception as exc:  # noqa: BLE001 — a failed call is a result the model reads
        text = str(exc) or type(exc).__name__
        if "timed out" in text.lower() or "timeout" in type(exc).__name__.lower():
            text = f"it did not answer within {CALL_TIMEOUT_S:g} s"
        return _bounded(f"The tool failed: {text}"), True
    if isinstance(result, str):
        return _bounded(result), False
    return _bounded(json.dumps(result, ensure_ascii=False, default=str)), False
