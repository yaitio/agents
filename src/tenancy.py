"""
Who is calling, and whose data that makes it.

Every partition key begins with the tenant, so a query without one has no key to
write — the isolation is structural. This module is where a request becomes a
tenant, and it is deliberately the only place.

**Two keys, two jobs, two headers.**

* `X-Yait-Key` — the installation's token (tokens.py): a JWT the owner issued.
  It says whether this caller may use the deployment at all, and whose sessions
  are whose — its `sub` is the tenant. API Gateway checks it before the function
  runs and hands us the tenant; a local run checks it here.
* The standard header — `x-api-key` from the Anthropic SDK, `Authorization: Bearer`
  from the OpenAI SDK — carries the caller's **provider** key. It is not checked
  here and not stored anywhere: it is handed to the worker, which calls the model
  with it. That is what lets a client move by changing `base_url` and adding one
  default header, keeping its own key and its own bill.

Both SDKs take extra headers once, on the client: `default_headers={"X-Yait-Key":
…}`.
"""

from __future__ import annotations

import tokens

#: The installation token's header. Lower-case: headers are matched without case.
YAIT_HEADER = "x-yait-key"

NotConfigured = tokens.NotConfigured


class Unauthorized(Exception):
    """No token, or not one of this installation's."""


class Principal:
    """
    The answer: which tenant, which token said so, and the provider key the caller
    brought, if any — to be passed on, never kept.
    """

    __slots__ = ("tenant", "key_id", "provider_key")

    def __init__(self, tenant: str, key_id: str, provider_key: str | None = None) -> None:
        self.tenant = tenant
        self.key_id = key_id
        self.provider_key = provider_key

    def __repr__(self) -> str:  # never a key itself
        return (f"Principal(tenant={self.tenant!r}, key_id={self.key_id!r}, "
                f"provider_key={'given' if self.provider_key else 'none'})")


def presented(headers: dict | None) -> str | None:
    """The caller's provider key, from either SDK's convention."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    if lower.get("x-api-key"):
        return lower["x-api-key"].strip()
    auth = (lower.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth or None


def resolve(headers: dict | None, authorizer: dict | None = None) -> Principal:
    """
    A request to a `Principal`. Behind API Gateway the authorizer has already
    checked the token and says whose it is; without one — a local run — the token
    is checked here, with the same code.
    """
    if authorizer and authorizer.get("tenant"):
        return Principal(tenant=authorizer["tenant"],
                         key_id=authorizer.get("key_id") or "",
                         provider_key=presented(headers))
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    token = (lower.get(YAIT_HEADER) or "").strip()
    if not token:
        raise Unauthorized("no installation token")
    try:
        claims = tokens.verify(token)
    except tokens.Invalid as exc:
        raise Unauthorized(str(exc)) from exc
    return Principal(tenant=claims["sub"],
                     # A name for the token, for logs and audit — never the token.
                     key_id=claims.get("name") or claims["jti"][:12],
                     provider_key=presented(headers))


def reset_cache() -> None:
    """For tests: forget the public keys read so far."""
    tokens.reset_cache()
