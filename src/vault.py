"""
Vaults: an end user's credentials for MCP servers, kept for the agent and never
shown to it.

A vault holds credentials; a session names the vaults it may use (`vault_ids`);
when the agent calls a tool on an MCP server, the worker finds the credential for
that server's address in those vaults and puts it in the request — never in the
model's context, never in a response. Both dialects have the same resource: write
a secret, and no API call ever returns it.

**The secret is encrypted before it is stored**, with AES-256-GCM under the
installation's vault key, and bound to its record: the tenant, the vault and the
credential are the associated data, so a ciphertext copied onto another record
does not decrypt. The key lives in one secret (`PREFIX/vault-key`) that the API and
the worker read, or in `YAIT_VAULT_KEY` for a local run. Without a key, vaults
and their public fields still work, and storing a secret is refused by name.

Stored in the agents table: a vault under `T#<tenant>#VAULT#<id>` with sort key
`vault`, its credentials beside it as `C#<id>` — never `meta`, which is what an
agent's listing looks for.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import events as E
import store as S

AUTH_TYPES = ("static_bearer", "mcp_oauth")
TOKEN_ENDPOINT_AUTH = ("none", "client_secret_basic", "client_secret_post")
METADATA_PAIRS, METADATA_KEY, METADATA_VALUE = 16, 64, 512


class NotFound(Exception):
    pass


class Invalid(ValueError):
    pass


class NotConfigured(Exception):
    """No vault key: nothing secret can be stored or read."""


class Archived(Exception):
    """The vault or credential is archived, and read-only."""


# ── the key ────────────────────────────────────────────────────────────────

_key_cache: dict[str, Any] = {"at": 0.0, "keys": None}
KEY_CACHE_SECONDS = 300


def _parse_keys(raw: str) -> dict:
    """
    `{"current": "k1", "keys": {"k1": "<base64 of 32 bytes>"}}`, or a bare base64
    key meaning `k1`. Several keys let a new one be added before the old is dropped.
    """
    raw = raw.strip()
    parsed = json.loads(raw) if raw.startswith("{") else {"current": "k1",
                                                          "keys": {"k1": raw}}
    keys = {kid: base64.b64decode(v) for kid, v in parsed["keys"].items()}
    if any(len(k) != 32 for k in keys.values()) or parsed["current"] not in keys:
        raise NotConfigured("the vault key must be 32 bytes, and `current` one of them")
    return {"current": parsed["current"], "keys": keys}


def _keys() -> dict:
    now = time.time()
    if _key_cache["keys"] and now - _key_cache["at"] < KEY_CACHE_SECONDS:
        return _key_cache["keys"]
    raw = os.environ.get("YAIT_VAULT_KEY", "").strip()
    if not raw and os.environ.get("YAIT_VAULT_KEY_SECRET", "").strip():
        import boto3
        client = boto3.client("secretsmanager")
        try:
            raw = client.get_secret_value(
                SecretId=os.environ["YAIT_VAULT_KEY_SECRET"])["SecretString"]
        except client.exceptions.ResourceNotFoundException:
            raw = ""
    if not raw:
        raise NotConfigured(
            "this installation has no vault key, so it cannot store a secret: "
            "run scripts/keys.py vault-key, or set YAIT_VAULT_KEY for a local run")
    keys = _parse_keys(raw)
    _key_cache.update(at=now, keys=keys)
    return keys


def new_key() -> str:
    """A fresh vault key, as the secret holds it."""
    return json.dumps({"current": "k1",
                       "keys": {"k1": base64.b64encode(os.urandom(32)).decode()}})


def _bound(tenant: str, vault_id: str, credential_id: str) -> bytes:
    return f"{tenant}|{vault_id}|{credential_id}".encode()


def seal(secret: dict, *, tenant: str, vault_id: str, credential_id: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    keys = _keys()
    nonce = os.urandom(12)
    sealed = AESGCM(keys["keys"][keys["current"]]).encrypt(
        nonce, json.dumps(secret).encode(), _bound(tenant, vault_id, credential_id))
    return f"{keys['current']}:{base64.b64encode(nonce + sealed).decode()}"


def unseal(blob: str, *, tenant: str, vault_id: str, credential_id: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kid, _, body = blob.partition(":")
    key = _keys()["keys"].get(kid)
    if key is None:
        raise NotConfigured(f"the vault key {kid!r} this secret was sealed with is gone")
    raw = base64.b64decode(body)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:],
                                          _bound(tenant, vault_id, credential_id)))


def reset_cache() -> None:
    _key_cache.update(at=0.0, keys=None)


# ── what a request may say ─────────────────────────────────────────────────

def _metadata(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > METADATA_PAIRS or any(
            not isinstance(k, str) or not isinstance(v, str)
            or len(k) > METADATA_KEY or len(v) > METADATA_VALUE
            for k, v in value.items()):
        raise Invalid(f"metadata: at most {METADATA_PAIRS} string pairs, keys up to "
                      f"{METADATA_KEY} characters and values up to {METADATA_VALUE}")
    return dict(value)


def _https(url: Any, field: str) -> str:
    # A local run may name a server on this machine — the test MCP server the
    # suites start. Never in the cloud: the setting is not deployed.
    local = os.environ.get("YAIT_ALLOW_LOCAL_MCP") == "1" and isinstance(url, str) \
        and url.startswith(("http://127.0.0.1", "http://localhost"))
    if not isinstance(url, str) or not (url.startswith("https://") or local):
        raise Invalid(f"{field} must be an https:// URL")
    return url.rstrip("/")


def split_auth(auth: Any) -> tuple[dict, dict]:
    """A request's `auth` into what may be shown and what is sealed."""
    if not isinstance(auth, dict):
        raise Invalid("auth must be an object")
    kind = auth.get("type")
    if kind == "environment_variable":
        raise Invalid("auth.type environment_variable needs a sandbox to substitute "
                      "it into, and this deployment has none")
    if kind not in AUTH_TYPES:
        raise Invalid(f"auth.type must be one of {', '.join(AUTH_TYPES)}")
    url = _https(auth.get("mcp_server_url"), "auth.mcp_server_url")
    if kind == "static_bearer":
        _only(auth, {"type", "mcp_server_url", "token"}, "auth")
        if not auth.get("token"):
            raise Invalid("auth.token is required")
        return {"type": kind, "mcp_server_url": url}, {"token": auth["token"]}

    _only(auth, {"type", "mcp_server_url", "access_token", "expires_at", "refresh"},
          "auth")
    if not auth.get("access_token"):
        raise Invalid("auth.access_token is required")
    public = {"type": kind, "mcp_server_url": url,
              "expires_at": auth.get("expires_at"), "refresh": None}
    secret = {"access_token": auth["access_token"]}
    refresh = auth.get("refresh")
    if refresh:
        _only(refresh, {"client_id", "refresh_token", "token_endpoint",
                        "token_endpoint_auth", "resource", "scope"}, "auth.refresh")
        for need in ("client_id", "refresh_token", "token_endpoint"):
            if not refresh.get(need):
                raise Invalid(f"auth.refresh.{need} is required")
        endpoint_auth = refresh.get("token_endpoint_auth") or {"type": "none"}
        if endpoint_auth.get("type") not in TOKEN_ENDPOINT_AUTH:
            raise Invalid("auth.refresh.token_endpoint_auth.type must be one of "
                          + ", ".join(TOKEN_ENDPOINT_AUTH))
        if endpoint_auth["type"] != "none" and not endpoint_auth.get("client_secret"):
            raise Invalid("auth.refresh.token_endpoint_auth.client_secret is required")
        public["refresh"] = {
            "client_id": refresh["client_id"],
            "token_endpoint": _https(refresh["token_endpoint"],
                                     "auth.refresh.token_endpoint"),
            "token_endpoint_auth": {"type": endpoint_auth["type"]},
            "resource": refresh.get("resource"), "scope": refresh.get("scope")}
        secret["refresh_token"] = refresh["refresh_token"]
        if endpoint_auth.get("client_secret"):
            secret["client_secret"] = endpoint_auth["client_secret"]
    return public, secret


def _only(body: dict, allowed: set, where: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise Invalid(f"{where}: unknown field(s): {', '.join(unknown)}")


# ── the store ──────────────────────────────────────────────────────────────

def key(tenant: str, vault_id: str) -> str:
    return f"T#{tenant}#VAULT#{vault_id}"


class MemoryVaults:
    """The same semantics without AWS, for tests and local runs."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def _put(self, pk: str, sk: str, item: dict) -> None:
        self._items[(pk, sk)] = dict(item)

    def _get(self, pk: str, sk: str) -> dict | None:
        found = self._items.get((pk, sk))
        return dict(found) if found else None

    def _delete(self, pk: str, sk: str) -> None:
        self._items.pop((pk, sk), None)

    def _query(self, pk: str, prefix: str) -> list[dict]:
        return [dict(v) for (p, s), v in self._items.items()
                if p == pk and s.startswith(prefix)]

    def _vaults_of(self, tenant: str) -> list[dict]:
        return [dict(v) for (p, s), v in self._items.items()
                if s == "vault" and v.get("tenant") == tenant]


class DynamoVaults(MemoryVaults):
    def __init__(self, table: str, client=None) -> None:
        import boto3
        self._table = (client or boto3.resource("dynamodb")).Table(table)

    def _put(self, pk, sk, item):
        self._table.put_item(Item={"pk": pk, "sk": sk, **item})

    def _get(self, pk, sk):
        found = S.DynamoStore._plain(self._table.get_item(
            Key={"pk": pk, "sk": sk}).get("Item"))
        if found:
            found.pop("pk", None)
            found.pop("sk", None)
        return found

    def _delete(self, pk, sk):
        self._table.delete_item(Key={"pk": pk, "sk": sk})

    def _query(self, pk, prefix):
        from boto3.dynamodb.conditions import Key as K
        items, kwargs = [], {"KeyConditionExpression":
                             K("pk").eq(pk) & K("sk").begins_with(prefix)}
        while True:
            page = self._table.query(**kwargs)
            items += [S.DynamoStore._plain(i) for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _vaults_of(self, tenant):
        # A scan, as for agents: listing a tenant's vaults is rare and human-driven.
        from boto3.dynamodb.conditions import Attr
        items, kwargs = [], {"FilterExpression":
                             Attr("sk").eq("vault") & Attr("tenant").eq(tenant)}
        while True:
            page = self._table.scan(**kwargs)
            items += [S.DynamoStore._plain(i) for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


class Vaults:
    """Vaults and credentials over either store; the same answers from both."""

    def __init__(self, backend: MemoryVaults) -> None:
        self.backend = backend

    # vaults
    def create(self, tenant: str, *, name: str | None, metadata: Any) -> dict:
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 255):
            raise Invalid("the name must be 1 to 255 characters")
        vault_id = E.new_id("vlt")
        now = S.now_ms()
        record = {"tenant": tenant, "vault_id": vault_id,
                  "name": name.strip() if name else None,
                  "metadata": _metadata(metadata),
                  "created_at": now, "updated_at": now}
        self.backend._put(key(tenant, vault_id), "vault", record)
        return record

    def get(self, tenant: str, vault_id: str) -> dict:
        found = self.backend._get(key(tenant, vault_id), "vault")
        if not found or found.get("deleted"):
            raise NotFound(vault_id)
        return found

    def list(self, tenant: str, *, include_archived: bool = False) -> list[dict]:
        out = [v for v in self.backend._vaults_of(tenant) if not v.get("deleted")
               and (include_archived or not v.get("archived_at"))]
        return sorted(out, key=lambda v: v["vault_id"], reverse=True)

    def update(self, tenant: str, vault_id: str, *, name: Any = None,
               metadata: Any = None, patch_metadata: bool = True) -> dict:
        record = self.get(tenant, vault_id)
        if record.get("archived_at"):
            raise Archived(f"{vault_id} is archived and is read-only")
        if name is not None:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
                raise Invalid("the name must be 1 to 255 characters")
            record["name"] = name.strip()
        if metadata is not None:
            if patch_metadata:
                merged = {**record.get("metadata", {}), **metadata}
                record["metadata"] = _metadata(
                    {k: v for k, v in merged.items() if v is not None})
            else:
                record["metadata"] = _metadata(metadata)
        record["updated_at"] = S.now_ms()
        self.backend._put(key(tenant, vault_id), "vault", record)
        return record

    def archive(self, tenant: str, vault_id: str) -> dict:
        record = self.get(tenant, vault_id)
        if not record.get("archived_at"):
            now = S.now_ms()
            record.update(archived_at=now, updated_at=now)
            self.backend._put(key(tenant, vault_id), "vault", record)
            for c in self._credentials(tenant, vault_id):
                if not c.get("archived_at"):
                    c.update(archived_at=now, updated_at=now)
                    self.backend._put(key(tenant, vault_id), f"C#{c['credential_id']}", c)
        return record

    def delete(self, tenant: str, vault_id: str) -> None:
        self.get(tenant, vault_id)
        for c in self._credentials(tenant, vault_id):
            self.backend._delete(key(tenant, vault_id), f"C#{c['credential_id']}")
        self.backend._delete(key(tenant, vault_id), "vault")

    # credentials
    def _credentials(self, tenant: str, vault_id: str) -> list[dict]:
        return self.backend._query(key(tenant, vault_id), "C#")

    def add(self, tenant: str, vault_id: str, *, auth: Any, name: Any = None,
            metadata: Any = None) -> dict:
        vault = self.get(tenant, vault_id)
        if vault.get("archived_at"):
            raise Archived(f"{vault_id} is archived and is read-only")
        public, secret = split_auth(auth)
        # One credential per server in a vault: a session looks one up by the
        # server's address, and two would leave the answer to chance.
        clash = [c for c in self._credentials(tenant, vault_id)
                 if not c.get("archived_at")
                 and c["auth"]["mcp_server_url"] == public["mcp_server_url"]]
        if clash:
            raise Invalid(f"{vault_id} already holds a credential for "
                          f"{public['mcp_server_url']}: {clash[0]['credential_id']}")
        credential_id = E.new_id("vcrd")
        now = S.now_ms()
        record = {"tenant": tenant, "vault_id": vault_id,
                  "credential_id": credential_id,
                  "name": name.strip() if isinstance(name, str) and name.strip() else None,
                  "metadata": _metadata(metadata), "auth": public,
                  "secret": seal(secret, tenant=tenant, vault_id=vault_id,
                                 credential_id=credential_id),
                  "created_at": now, "updated_at": now}
        self.backend._put(key(tenant, vault_id), f"C#{credential_id}", record)
        return _public(record)

    def credential(self, tenant: str, vault_id: str, credential_id: str) -> dict:
        self.get(tenant, vault_id)
        found = self.backend._get(key(tenant, vault_id), f"C#{credential_id}")
        if not found:
            raise NotFound(credential_id)
        return _public(found)

    def credentials(self, tenant: str, vault_id: str, *,
                    include_archived: bool = False) -> list[dict]:
        self.get(tenant, vault_id)
        out = [_public(c) for c in self._credentials(tenant, vault_id)
               if include_archived or not c.get("archived_at")]
        return sorted(out, key=lambda c: c["credential_id"], reverse=True)

    def rotate(self, tenant: str, vault_id: str, credential_id: str, *,
               auth: Any = None, name: Any = None, metadata: Any = None,
               patch_metadata: bool = True) -> dict:
        found = self.backend._get(key(tenant, vault_id), f"C#{credential_id}")
        if not found:
            raise NotFound(credential_id)
        if found.get("archived_at"):
            raise Archived(f"{credential_id} is archived and is read-only")
        if auth is not None:
            public, secret = split_auth(_merged(auth, found, tenant=tenant,
                                                vault_id=vault_id,
                                                credential_id=credential_id))
            if public["type"] != found["auth"]["type"] or \
                    public["mcp_server_url"] != found["auth"]["mcp_server_url"]:
                raise Invalid("a credential's type and server are fixed: create "
                              "another for a different one")
            found["auth"] = public
            found["secret"] = seal(secret, tenant=tenant, vault_id=vault_id,
                                   credential_id=credential_id)
        if isinstance(name, str) and name.strip():
            found["name"] = name.strip()
        if metadata is not None:
            merged = ({**found.get("metadata", {}), **metadata}
                      if patch_metadata else metadata)
            found["metadata"] = _metadata({k: v for k, v in merged.items()
                                           if v is not None})
        found["updated_at"] = S.now_ms()
        self.backend._put(key(tenant, vault_id), f"C#{credential_id}", found)
        return _public(found)

    def archive_credential(self, tenant: str, vault_id: str,
                           credential_id: str) -> dict:
        found = self.backend._get(key(tenant, vault_id), f"C#{credential_id}")
        if not found:
            raise NotFound(credential_id)
        if not found.get("archived_at"):
            now = S.now_ms()
            # Archiving keeps the record and drops the secret: nothing archived
            # can be used, so nothing archived needs to be decryptable.
            found.update(archived_at=now, updated_at=now, secret=None)
            self.backend._put(key(tenant, vault_id), f"C#{credential_id}", found)
        return _public(found)

    def delete_credential(self, tenant: str, vault_id: str, credential_id: str) -> None:
        self.get(tenant, vault_id)
        if not self.backend._get(key(tenant, vault_id), f"C#{credential_id}"):
            raise NotFound(credential_id)
        self.backend._delete(key(tenant, vault_id), f"C#{credential_id}")

    # the worker's side
    def check_ids(self, tenant: str, vault_ids: Any) -> list[str]:
        """A session's `vault_ids`, each an existing, usable vault of this tenant."""
        if vault_ids in (None, []):
            return []
        if not isinstance(vault_ids, list) or not all(isinstance(v, str) for v in vault_ids):
            raise Invalid("vault_ids must be a list of vault ids")
        for vault_id in vault_ids:
            try:
                vault = self.get(tenant, vault_id)
            except NotFound:
                raise Invalid(f"no vault {vault_id}") from None
            if vault.get("archived_at"):
                raise Invalid(f"{vault_id} is archived")
        return list(dict.fromkeys(vault_ids))

    def secret_for(self, tenant: str, vault_ids: list[str], server_url: str) -> dict | None:
        """
        The credential for an MCP server, from the first of a session's vaults that
        has one: its public auth and its secret, decrypted. Only the worker calls
        this, and only to put the secret in a request to that server.
        """
        url = server_url.rstrip("/")
        for vault_id in vault_ids:
            for c in self._credentials(tenant, vault_id):
                if c.get("archived_at") or c["auth"]["mcp_server_url"] != url:
                    continue
                return {"credential_id": c["credential_id"], "vault_id": vault_id,
                        "auth": c["auth"],
                        "secret": unseal(c["secret"], tenant=tenant, vault_id=vault_id,
                                         credential_id=c["credential_id"])}
        return None


def _merged(auth: Any, found: dict, **where: str) -> dict:
    """
    An update's `auth` over the credential as it stands. Both dialects fix the
    server (OpenAI's rotate has no field for it), and Anthropic's update is
    partial — a new `expires_at` alone keeps the token — so what the request leaves
    out is taken from what is stored.
    """
    if not isinstance(auth, dict):
        raise Invalid("auth must be an object")
    current, old = found["auth"], unseal(found["secret"], **where)
    merged = {"type": auth.get("type", current["type"]),
              "mcp_server_url": auth.get("mcp_server_url", current["mcp_server_url"])}
    if merged["type"] == "static_bearer":
        merged["token"] = auth.get("token") or old.get("token")
        return merged
    merged["access_token"] = auth.get("access_token") or old.get("access_token")
    merged["expires_at"] = auth["expires_at"] if "expires_at" in auth \
        else current.get("expires_at")
    refresh = auth["refresh"] if "refresh" in auth else current.get("refresh")
    if refresh:
        was = current.get("refresh") or {}
        endpoint_auth = {**(was.get("token_endpoint_auth") or {}),
                         **(refresh.get("token_endpoint_auth") or {})}
        if endpoint_auth.get("type", "none") != "none" \
                and not endpoint_auth.get("client_secret"):
            endpoint_auth["client_secret"] = old.get("client_secret")
        merged["refresh"] = {**{k: was.get(k) for k in ("client_id", "token_endpoint",
                                                        "resource", "scope")},
                             **{k: v for k, v in refresh.items()
                                if k != "token_endpoint_auth"},
                             "token_endpoint_auth": endpoint_auth}
        merged["refresh"].setdefault("refresh_token", old.get("refresh_token"))
        if not merged["refresh"].get("refresh_token"):
            merged["refresh"]["refresh_token"] = old.get("refresh_token")
    return merged


def _public(record: dict) -> dict:
    """A credential as anything but the worker may see it: never its secret."""
    return {k: v for k, v in record.items() if k != "secret"}


def from_environment() -> Vaults:
    table = os.environ.get("YAIT_AGENTS_TABLE", "").strip()
    return Vaults(DynamoVaults(table) if table else MemoryVaults())
