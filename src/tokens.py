"""
The installation's tokens: who may use this deployment, and as which tenant.

A token is a JWT, signed with Ed25519, sent as `X-Yait-Key`. Its `sub` is the
tenant — whose agents and sessions these are. It has no expiry: it is good until
its signing key is removed.

**Only the public half lives in the deployment.** The owner makes a key pair on
their own machine (`scripts/keys.py keygen`), keeps the private key there, and
puts the public key in the agents table. So the deployment can check a token but
never make one, and nothing an attacker reads out of the account lets them in.

The public key is an item in the agents table, under a partition no tenant can
have, one item per key id (`kid`): a second key can be added beside the first
and the first removed later, which is how the keys are rotated.

API Gateway checks the token before the function runs (a request authorizer:
`authorize()` below, cached per token), so a request without a good one never
reaches our code. A local run has no gateway, and the handler checks it itself
with the same `verify()`.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import jwt

ALGORITHM = "EdDSA"

#: The agents table's partition for the installation's own items. A tenant's
#: partition keys begin with the tenant, which never begins with an underscore.
KEYS_PARTITION = "_installation"
KEY_ITEM_PREFIX = "jwt_key#"

#: How long a public key read from the table is trusted in a warm environment.
CACHE_SECONDS = 300
_cache: dict[str, tuple[float, str | None]] = {}


class Invalid(Exception):
    """Not a token of this installation."""


class NotConfigured(Exception):
    """The installation has no public key, so no token can be checked."""


# ── making one (the owner's machine, and tests) ────────────────────────────

def keypair() -> tuple[str, str]:
    """A new Ed25519 key pair, as PEM: (private, public)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode(),
        private.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo
                                          ).decode(),
    )


def issue(private_pem: str, *, kid: str, tenant: str, name: str = "") -> str:
    """A token for `tenant`, signed with the private key named `kid`."""
    claims = {"sub": tenant, "jti": uuid.uuid4().hex, "iat": int(time.time())}
    if name:
        claims["name"] = name
    return jwt.encode(claims, private_pem, algorithm=ALGORITHM, headers={"kid": kid})


# ── checking one ───────────────────────────────────────────────────────────

def _from_environment(kid: str) -> str | None:
    """`YAIT_JWT_PUBLIC_KEYS` — `{kid: pem}` as JSON, for local runs and tests."""
    raw = os.environ.get("YAIT_JWT_PUBLIC_KEYS", "").strip()
    return json.loads(raw).get(kid) if raw else None


def _from_table(kid: str) -> str | None:
    table = os.environ.get("YAIT_AGENTS_TABLE", "").strip()
    if not table:
        return None
    import boto3
    item = boto3.resource("dynamodb").Table(table).get_item(
        Key={"pk": KEYS_PARTITION, "sk": KEY_ITEM_PREFIX + kid}).get("Item")
    return (item or {}).get("public_key")


def public_key(kid: str) -> str | None:
    now = time.time()
    hit = _cache.get(kid)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    found = _from_environment(kid) or _from_table(kid)
    _cache[kid] = (now, found)
    return found


def configured() -> bool:
    return bool(os.environ.get("YAIT_JWT_PUBLIC_KEYS", "").strip()
                or os.environ.get("YAIT_AGENTS_TABLE", "").strip())


def verify(token: str) -> dict:
    """The token's claims, or `Invalid`. `NotConfigured` if nothing can check it."""
    if not configured():
        raise NotConfigured(
            "This installation has no public key to check tokens with: run "
            "scripts/keys.py keygen, or set YAIT_JWT_PUBLIC_KEYS for a local run.")
    try:
        kid = jwt.get_unverified_header(token).get("kid") or ""
    except jwt.PyJWTError as exc:
        raise Invalid("not a token") from exc
    key = public_key(kid)
    if not key:
        raise Invalid("signed with a key this installation does not have")
    try:
        # Only EdDSA: a token may not choose a weaker algorithm for itself.
        claims = jwt.decode(token, key, algorithms=[ALGORITHM],
                            options={"require": ["sub", "jti"]})
    except jwt.PyJWTError as exc:
        raise Invalid(str(exc)) from exc
    return claims


def reset_cache() -> None:
    _cache.clear()


# ── the gateway's authorizer ───────────────────────────────────────────────

def is_authorizer_event(event: dict) -> bool:
    return isinstance(event, dict) and event.get("type") == "REQUEST" \
        and "methodArn" in event


def authorize(event: dict) -> dict:
    """
    API Gateway's request authorizer. The answer is cached per token, and applies
    to every route: so it allows the whole API — allowing only the method that
    happened to arrive first would refuse the next route from the cache.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = (headers.get("x-yait-key") or "").strip()
    try:
        claims = verify(token)
    except (Invalid, NotConfigured):
        # The one answer the gateway turns into a 401.
        raise Exception("Unauthorized")  # noqa: TRY002 — the gateway's contract
    # arn:aws:execute-api:region:account:api/stage/METHOD/path → api/*
    arn = event["methodArn"]
    api = arn.split("/", 1)[0]
    return {
        "principalId": claims["sub"],
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": "Allow",
                           "Resource": f"{api}/*"}],
        },
        # Strings only: the gateway passes these on as the request's context.
        "context": {"tenant": claims["sub"],
                    "key_id": claims.get("name") or claims["jti"][:12]},
    }
