"""
Agents: stored, versioned, and a prerequisite for a session.

The Anthropic dialect takes no inline agent configuration — a session references an
agent by id, and pins a version. The OpenAI dialect allows either a saved agent or
an inline one and has no versions endpoint at all. So we store the richer shape,
which is a superset: a version is recorded whether or not a façade exposes it.

**A version is never mutated.** An update writes a new one and moves the pointer,
because a session pins a version and a session that followed a moving definition
would silently change behaviour mid-conversation. That is also why `get` takes an
optional version: resolving "the agent this session pinned" must not depend on what
the agent looks like now.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import events as E
import store as S

#: The fields a façade may set. Anything else in a request is refused by name
#: rather than ignored — a silently dropped field is a configuration nobody
#: reviewed.
FIELDS = ("name", "model", "instructions", "tools", "mcp_servers", "skills",
          "metadata")


class Conflict(S.Conflict):
    """The agent exists, or the version moved under us."""


class NotFound(Exception):
    pass


def key(tenant: str, agent_id: str) -> str:
    return f"T#{tenant}#A#{agent_id}"


class AgentStore(Protocol):
    def create(self, tenant: str, **fields: Any) -> dict: ...
    def update(self, tenant: str, agent_id: str, **fields: Any) -> dict: ...
    def get(self, tenant: str, agent_id: str,
            version: int | None = None) -> dict: ...
    def list(self, tenant: str, limit: int = 50) -> list[dict]: ...
    def versions(self, tenant: str, agent_id: str) -> list[dict]: ...
    def archive(self, tenant: str, agent_id: str) -> dict: ...


def _order(record: dict) -> str:
    """
    The id, which carries the creation time — see `events.new_id`.

    A timestamp alone is not a total order: two agents created in the same
    millisecond tie, and a tie under pagination means a client walking pages sees an
    item twice or misses one. Sorting by the id avoids both, and agrees with
    creation because the time is the id's prefix.
    """
    return record.get("agent_id", "")


def _validate(fields: dict) -> dict:
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    if not fields.get("model"):
        raise ValueError("model is required")
    return {k: v for k, v in fields.items() if v is not None}


class MemoryAgentStore:
    """The same semantics without AWS, for tests and local runs."""

    def __init__(self) -> None:
        self._meta: dict[str, dict] = {}
        self._versions: dict[str, dict[int, dict]] = {}

    def create(self, tenant: str, **fields: Any) -> dict:
        body = _validate(fields)
        agent_id = E.new_id("agent")
        k = key(tenant, agent_id)
        version = {"version": 1, "created_at": S.now_ms(), **body}
        self._versions[k] = {1: version}
        self._meta[k] = {"tenant": tenant, "agent_id": agent_id, "version": 1,
                         "created_at": version["created_at"],
                         "updated_at": version["created_at"], "archived": False}
        return self.get(tenant, agent_id)

    def update(self, tenant: str, agent_id: str, **fields: Any) -> dict:
        k = key(tenant, agent_id)
        meta = self._meta.get(k)
        if meta is None:
            raise NotFound(agent_id)
        if meta["archived"]:
            raise Conflict(f"{agent_id} is archived and is read-only")
        current = dict(self._versions[k][meta["version"]])
        current.pop("version", None)
        current.pop("created_at", None)
        body = _validate({**current, **fields})
        version = meta["version"] + 1
        self._versions[k][version] = {"version": version,
                                      "created_at": S.now_ms(), **body}
        meta["version"] = version
        meta["updated_at"] = S.now_ms()
        return self.get(tenant, agent_id)

    def get(self, tenant: str, agent_id: str, version: int | None = None) -> dict:
        k = key(tenant, agent_id)
        meta = self._meta.get(k)
        if meta is None:
            raise NotFound(agent_id)
        want = version or meta["version"]
        body = self._versions[k].get(want)
        if body is None:
            raise NotFound(f"{agent_id} version {want}")
        return {**meta, **body, "version": want,
                "latest_version": meta["version"]}

    def list(self, tenant: str, limit: int = 50) -> list[dict]:
        out = [self.get(tenant, m["agent_id"])
               for m in self._meta.values() if m["tenant"] == tenant]
        out.sort(key=_order, reverse=True)
        return out[:limit]

    def versions(self, tenant: str, agent_id: str) -> list[dict]:
        k = key(tenant, agent_id)
        if k not in self._versions:
            raise NotFound(agent_id)
        return [dict(self._versions[k][v]) for v in sorted(self._versions[k])]

    def archive(self, tenant: str, agent_id: str) -> dict:
        k = key(tenant, agent_id)
        meta = self._meta.get(k)
        if meta is None:
            raise NotFound(agent_id)
        # Archive is terminal in both dialects: read-only, no new sessions, and no
        # way back. So it is idempotent rather than an error on a second call.
        meta["archived"] = True
        meta["archived_at"] = meta.get("archived_at") or S.now_ms()
        return self.get(tenant, agent_id)


class DynamoAgentStore:
    """
    One table, two kinds of item under the same partition: `meta` and `V#<n>`.

    A new version is written before the pointer moves, and the pointer moves with a
    condition on the version it is replacing. So two concurrent updates cannot both
    claim version N, and the loser sees a conflict rather than overwriting.
    """

    def __init__(self, table: str, client=None) -> None:
        import boto3
        resource = client or boto3.resource("dynamodb")
        self._table = resource.Table(table)
        self._conditional = (
            resource.meta.client.exceptions.ConditionalCheckFailedException)

    def _plain(self, item: dict | None) -> dict | None:
        return S.DynamoStore._plain(item)

    def create(self, tenant: str, **fields: Any) -> dict:
        body = _validate(fields)
        agent_id = E.new_id("agent")
        k = key(tenant, agent_id)
        at = S.now_ms()
        self._table.put_item(Item={"pk": k, "sk": "V#1", "version": 1,
                                   "created_at": at, **body})
        self._table.put_item(
            Item={"pk": k, "sk": "meta", "tenant": tenant, "agent_id": agent_id,
                  "version": 1, "created_at": at, "updated_at": at,
                  "archived": False},
            ConditionExpression="attribute_not_exists(pk)")
        return self.get(tenant, agent_id)

    def update(self, tenant: str, agent_id: str, **fields: Any) -> dict:
        k = key(tenant, agent_id)
        meta = self._plain(self._table.get_item(
            Key={"pk": k, "sk": "meta"}).get("Item"))
        if meta is None:
            raise NotFound(agent_id)
        if meta.get("archived"):
            raise Conflict(f"{agent_id} is archived and is read-only")

        current = self._plain(self._table.get_item(
            Key={"pk": k, "sk": f"V#{meta['version']}"}).get("Item")) or {}
        for drop in ("pk", "sk", "version", "created_at"):
            current.pop(drop, None)
        body = _validate({**current, **fields})
        version = int(meta["version"]) + 1
        at = S.now_ms()
        self._table.put_item(Item={"pk": k, "sk": f"V#{version}",
                                   "version": version, "created_at": at, **body})
        try:
            self._table.update_item(
                Key={"pk": k, "sk": "meta"},
                UpdateExpression="SET #v = :new, updated_at = :at",
                ConditionExpression="#v = :old",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":new": version,
                                           ":old": meta["version"], ":at": at})
        except self._conditional as exc:
            raise Conflict(
                f"{agent_id} moved to another version while this update was in "
                "flight") from exc
        return self.get(tenant, agent_id)

    def get(self, tenant: str, agent_id: str, version: int | None = None) -> dict:
        k = key(tenant, agent_id)
        meta = self._plain(self._table.get_item(
            Key={"pk": k, "sk": "meta"}).get("Item"))
        if meta is None:
            raise NotFound(agent_id)
        want = int(version or meta["version"])
        body = self._plain(self._table.get_item(
            Key={"pk": k, "sk": f"V#{want}"}).get("Item"))
        if body is None:
            raise NotFound(f"{agent_id} version {want}")
        for drop in ("pk", "sk"):
            meta.pop(drop, None)
            body.pop(drop, None)
        return {**meta, **body, "version": want,
                "latest_version": int(meta["version"])}

    def list(self, tenant: str, limit: int = 50) -> list[dict]:
        # A scan, and it is the right call at this size: listing every agent of one
        # tenant is a rare, human-driven request. It becomes a GSI when there is a
        # measurement saying so, not before.
        from boto3.dynamodb.conditions import Attr
        out: list[dict] = []
        kwargs = {"FilterExpression": Attr("sk").eq("meta")
                  & Attr("tenant").eq(tenant)}
        while True:
            page = self._table.scan(**kwargs)
            out.extend(self._plain(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page or len(out) >= limit:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        out.sort(key=_order, reverse=True)
        return [self.get(tenant, a["agent_id"]) for a in out[:limit]]

    def versions(self, tenant: str, agent_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key as K
        k = key(tenant, agent_id)
        page = self._table.query(
            KeyConditionExpression=K("pk").eq(k) & K("sk").begins_with("V#"))
        items = [self._plain(i) for i in page.get("Items", [])]
        if not items:
            raise NotFound(agent_id)
        for item in items:
            item.pop("pk", None)
            item.pop("sk", None)
        return sorted(items, key=lambda v: int(v["version"]))

    def archive(self, tenant: str, agent_id: str) -> dict:
        k = key(tenant, agent_id)
        try:
            self._table.update_item(
                Key={"pk": k, "sk": "meta"},
                UpdateExpression=("SET archived = :t, "
                                  "archived_at = if_not_exists(archived_at, :at)"),
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":t": True, ":at": S.now_ms()})
        except self._conditional as exc:
            raise NotFound(agent_id) from exc
        return self.get(tenant, agent_id)


def from_environment() -> AgentStore:
    import os
    table = os.environ.get("YAIT_AGENTS_TABLE", "").strip()
    return DynamoAgentStore(table) if table else MemoryAgentStore()
