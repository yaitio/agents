"""
The two tables the session lives in, and the guarantees that make them safe.

`sessions` holds one small item per session — the cursor, the lease, the status and
the counters. `events` is the append-only log, and the only source of truth for what
happened.

Three guarantees, and only the last two are load-bearing:

1. **The sequence is assigned by the write that wins.** Every append is conditional
   on `attribute_not_exists(sk)`, so two invocations that both computed sequence N
   cannot both succeed. Ordering is therefore a property of the store, not of a
   queue — which is why there is no queue.
2. **The lease is a time-limited claim**, not a lock. A lock needs releasing and
   the holder can die; a lease expires. It saves wasted work; it does not guarantee
   correctness, because (1) already does.
3. **One transaction per commit** — the events and the session item cannot
   disagree, which is what makes "every state transition is written before it is
   acted on" true rather than aspirational.

Two implementations, one interface, one test suite. `MemoryStore` honours the
conditional append and the lease exactly, because a fake that always says yes would
test nothing — and the conditional append is the whole point.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Iterable, Protocol

import events as E


#: One clock for the whole function, defined beside the events it timestamps.
now_ms = E.now_ms


def key(tenant: str, session: str) -> str:
    """The partition key. The tenant is a prefix, so a query without one has no key
    to write — the isolation is structural rather than careful."""
    return f"T#{tenant}#S#{session}"


class Conflict(Exception):
    """Another writer took this sequence number, or holds the lease."""


class Store(Protocol):
    def create_session(self, tenant: str, session: str, **fields: Any) -> dict: ...
    def get_session(self, tenant: str, session: str) -> dict | None: ...
    def list_sessions(self, tenant: str) -> list[dict]: ...
    def append(self, tenant: str, session: str, seq: int, event: dict) -> None: ...
    def append_next(self, tenant: str, session: str, event: dict) -> int: ...
    def events(self, tenant: str, session: str, after: int = -1) -> list[dict]: ...
    def commit(self, tenant: str, session: str, events: Iterable[dict], *,
               cursor: int, **fields: Any) -> int: ...
    def take_lease(self, tenant: str, session: str, holder: str,
                   seconds: int = 60) -> bool: ...
    def release_lease(self, tenant: str, session: str, holder: str) -> None: ...
    def purge(self, tenant: str, session: str, *,
              keep_going: Callable[[], bool] = lambda: True) -> bool: ...


# ── in memory, for tests and the local server ──────────────────────────────

class MemoryStore:
    """
    The same semantics without AWS. Not a stub: the conditional append really
    refuses a taken sequence and the lease really expires, because those are the
    behaviours under test.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._events: dict[str, dict[int, dict]] = {}
        self._lock = threading.Lock()

    def create_session(self, tenant: str, session: str, **fields: Any) -> dict:
        k = key(tenant, session)
        with self._lock:
            if k in self._sessions:
                raise Conflict(f"session {session} already exists")
            item = {"pk": k, "tenant": tenant, "session": session,
                    "cursor": 0, "status": E.TURN_OVER, "agent_state": {},
                    "created_at": now_ms(), **fields}
            self._sessions[k] = item
            self._events[k] = {0: dict(E.event(E.SESSION_CREATED))}
            return dict(item)

    def get_session(self, tenant: str, session: str) -> dict | None:
        item = self._sessions.get(key(tenant, session))
        return dict(item) if item else None

    def list_sessions(self, tenant: str) -> list[dict]:
        return [dict(s) for s in self._sessions.values() if s.get("tenant") == tenant]

    def append(self, tenant: str, session: str, seq: int, event: dict) -> None:
        k = key(tenant, session)
        with self._lock:
            log = self._events.setdefault(k, {})
            if seq in log:
                raise Conflict(f"sequence {seq} is taken")
            log[seq] = dict(event)

    def append_next(self, tenant: str, session: str, event: dict) -> int:
        k = key(tenant, session)
        for _ in range(20):
            with self._lock:
                log = self._events.setdefault(k, {})
                seq = (max(log) + 1) if log else 0
            try:
                self.append(tenant, session, seq, event)
                return seq
            except Conflict:
                continue
        raise Conflict("could not find a free sequence number")

    def events(self, tenant: str, session: str, after: int = -1) -> list[dict]:
        # `seq` is added on the way out, exactly as DynamoStore derives it from the
        # sort key — so neither store can carry a field the other does not.
        log = self._events.get(key(tenant, session), {})
        return [dict(log[s], seq=s) for s in sorted(log) if s > after]

    def commit(self, tenant: str, session: str, events: Iterable[dict], *,
               cursor: int, **fields: Any) -> int:
        """Append in one shot and move the cursor. Atomic here by the lock."""
        k = key(tenant, session)
        batch = list(events)
        with self._lock:
            log = self._events.setdefault(k, {})
            seq = cursor
            for event in batch:
                seq += 1
                if seq in log:
                    raise Conflict(f"sequence {seq} is taken")
            seq = cursor
            for event in batch:
                seq += 1
                log[seq] = dict(event)
            item = self._sessions.setdefault(k, {"pk": k, "tenant": tenant,
                                                 "session": session})
            item.update(fields)
            item["cursor"] = seq
        return seq

    def take_lease(self, tenant: str, session: str, holder: str,
                   seconds: int = 60) -> bool:
        k = key(tenant, session)
        now = now_ms()
        with self._lock:
            item = self._sessions.get(k)
            if item is None:
                return False
            until = item.get("lease_until") or 0
            if until > now and item.get("lease_holder") != holder:
                return False
            item["lease_holder"] = holder
            item["lease_until"] = now + seconds * 1000
            return True

    def release_lease(self, tenant: str, session: str, holder: str) -> None:
        with self._lock:
            item = self._sessions.get(key(tenant, session))
            if item and item.get("lease_holder") == holder:
                item["lease_until"] = 0

    def purge(self, tenant: str, session: str, *,
              keep_going: Callable[[], bool] = lambda: True) -> bool:
        k = key(tenant, session)
        with self._lock:
            self._events.pop(k, None)
            if k in self._sessions:
                self._sessions[k] = _tombstone(self._sessions[k])
        return True


# ── DynamoDB ───────────────────────────────────────────────────────────────

class DynamoStore:
    """
    The same interface over two tables.

    Written against boto3's resource API, which handles the type marshalling.
    Numbers come back as `Decimal`, so anything read is normalised on the way out —
    a `Decimal` cursor compared against an `int` is the kind of bug that only
    appears in the cloud.
    """

    def __init__(self, sessions_table: str, events_table: str, client=None) -> None:
        import boto3  # imported here so the module loads without boto3 present
        resource = client or boto3.resource("dynamodb")
        self._sessions = resource.Table(sessions_table)
        self._events = resource.Table(events_table)
        self._conditional = (
            resource.meta.client.exceptions.ConditionalCheckFailedException
        )

    @staticmethod
    def _event(item: dict) -> dict:
        """
        One event as the interface promises it: `seq`, not `sk`.

        The sort key *is* the sequence, but the name is storage's business. Leaving
        `sk` in was the first thing that differed between this store and the
        in-memory one — which is the whole reason one suite runs against both.
        """
        out = DynamoStore._plain(item) or {}
        out["seq"] = int(out.pop("sk"))
        out.pop("pk", None)
        return out

    @staticmethod
    def _plain(item: dict | None) -> dict | None:
        """
        DynamoDB's types to plain Python, all the way down.

        Numbers come back as `Decimal` at every depth — in a nested map like an
        event's `usage`, in a list like an agent's tools — and `json.dumps` refuses
        a Decimal. Converting only the top level worked until the first nested
        number reached a response, and then that endpoint answered 502. The
        in-memory store never produces a Decimal at all, which is why only the
        deployment saw it.
        """
        if item is None:
            return None
        return _plain_value(item)

    def create_session(self, tenant: str, session: str, **fields: Any) -> dict:
        item = {"pk": key(tenant, session), "sk": "meta",
                "tenant": tenant, "session": session,
                "cursor": 0, "status": E.TURN_OVER, "agent_state": {},
                "created_at": now_ms(), **fields}
        try:
            self._sessions.put_item(
                Item=_dynamo_safe(item), ConditionExpression="attribute_not_exists(pk)")
        except self._conditional as exc:
            raise Conflict(f"session {session} already exists") from exc
        self.append(tenant, session, 0, E.event(E.SESSION_CREATED))
        return item

    def get_session(self, tenant: str, session: str) -> dict | None:
        got = self._sessions.get_item(Key={"pk": key(tenant, session), "sk": "meta"})
        return self._plain(got.get("Item"))

    def list_sessions(self, tenant: str) -> list[dict]:
        """
        A scan filtered by tenant. Right at this size — listing sessions is rare and
        human-driven — and wrong at scale, where docs/data-model.md already names the
        month-sharded index that replaces it. It becomes that when a measurement
        says so, not before.
        """
        from boto3.dynamodb.conditions import Attr
        out: list[dict] = []
        kwargs = {"FilterExpression": Attr("sk").eq("meta") & Attr("tenant").eq(tenant)}
        while True:
            page = self._sessions.scan(**kwargs)
            out.extend(self._plain(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def append(self, tenant: str, session: str, seq: int, event: dict) -> None:
        try:
            self._events.put_item(
                Item=_dynamo_safe({"pk": key(tenant, session), "sk": seq, **event}),
                ConditionExpression="attribute_not_exists(sk)")
        except self._conditional as exc:
            raise Conflict(f"sequence {seq} is taken") from exc

    def append_next(self, tenant: str, session: str, event: dict) -> int:
        """
        Find the next free sequence and take it.

        The read is a hint and the write is the truth: between the two, another
        invocation may take the number, and then the condition refuses and the loop
        reads again. This is the mechanism that makes a queue unnecessary.
        """
        for _ in range(20):
            log = self.events(tenant, session, after=-1)
            seq = (max(e["seq"] for e in log) + 1) if log else 0
            try:
                self.append(tenant, session, seq, event)
                return seq
            except Conflict:
                continue
        raise Conflict("could not find a free sequence number")

    def events(self, tenant: str, session: str, after: int = -1) -> list[dict]:
        from boto3.dynamodb.conditions import Key as K
        out: list[dict] = []
        kwargs = {"KeyConditionExpression": K("pk").eq(key(tenant, session))
                  & K("sk").gt(after)}
        while True:
            page = self._events.query(**kwargs)
            out.extend(self._event(i) for i in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def commit(self, tenant: str, session: str, events: Iterable[dict], *,
               cursor: int, **fields: Any) -> int:
        """
        Events and the session item, in one transaction.

        Two costs accepted knowingly: a transaction is billed at twice a write, and
        the limits are 100 items and 4 MB. A step emits a handful of events, so the
        limits are headroom.
        """
        import boto3
        batch = list(events)
        pk = key(tenant, session)
        seq = cursor
        items = []
        for event in batch:
            seq += 1
            items.append({"Put": {
                "TableName": self._events.name,
                "Item": _marshal(_dynamo_safe({"pk": pk, "sk": seq, **event})),
                "ConditionExpression": "attribute_not_exists(sk)",
            }})

        sets, values = ["#c = :c"], {":c": seq}
        names = {"#c": "cursor"}
        for i, (field, value) in enumerate(fields.items()):
            sets.append(f"#f{i} = :v{i}")
            names[f"#f{i}"] = field
            values[f":v{i}"] = value
        items.append({"Update": {
            "TableName": self._sessions.name,
            "Key": _marshal({"pk": pk, "sk": "meta"}),
            "UpdateExpression": "SET " + ", ".join(sets),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _marshal(_dynamo_safe(values)),
        }})

        client = boto3.client("dynamodb")
        try:
            client.transact_write_items(TransactItems=items)
        except client.exceptions.TransactionCanceledException as exc:
            raise Conflict(f"commit refused: {exc}") from exc
        return seq

    def take_lease(self, tenant: str, session: str, holder: str,
                   seconds: int = 60) -> bool:
        now = now_ms()
        try:
            self._sessions.update_item(
                Key={"pk": key(tenant, session), "sk": "meta"},
                UpdateExpression="SET lease_holder = :h, lease_until = :u",
                ConditionExpression=(
                    "attribute_exists(pk) AND "
                    "(attribute_not_exists(lease_until) OR lease_until < :now "
                    " OR lease_holder = :h)"),
                ExpressionAttributeValues={":h": holder, ":u": now + seconds * 1000,
                                           ":now": now},
            )
            return True
        except self._conditional:
            return False

    def release_lease(self, tenant: str, session: str, holder: str) -> None:
        try:
            self._sessions.update_item(
                Key={"pk": key(tenant, session), "sk": "meta"},
                UpdateExpression="SET lease_until = :zero",
                ConditionExpression="lease_holder = :h",
                ExpressionAttributeValues={":zero": 0, ":h": holder})
        except self._conditional:
            pass


    def purge(self, tenant: str, session: str, *,
              keep_going: Callable[[], bool] = lambda: True) -> bool:
        """
        Delete the log, then the session item. True when nothing is left.

        The events go first and the session item last, so an interrupted purge
        leaves the tombstone — still `deleted`, still hidden — for the next one to
        find and finish. Each page is checked against `keep_going`, which is how the
        worker stops before its time runs out rather than in the middle of a batch.
        """
        from boto3.dynamodb.conditions import Key as K
        pk = key(tenant, session)
        kwargs = {"KeyConditionExpression": K("pk").eq(pk),
                  "ProjectionExpression": "pk, sk"}
        while True:
            page = self._events.query(**kwargs)
            with self._events.batch_writer() as batch:
                for item in page.get("Items", []):
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            if "LastEvaluatedKey" not in page:
                break
            if not keep_going():
                return False
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        got = self._sessions.get_item(Key={"pk": pk, "sk": "meta"}).get("Item")
        if got:
            # Put, not update: the tombstone replaces the item whole, so nothing of
            # the session survives in it but that it existed and was deleted.
            self._sessions.put_item(Item=_dynamo_safe(_tombstone(self._plain(got))))
        return True

def _tombstone(record: dict) -> dict:
    """
    What is left of a purged session: that it was, and was deleted — nothing it
    held. Kept so a second delete can answer as the first did (OpenAI's delete is
    idempotent) and a late wake-up knows there is nothing to do.
    """
    keep = {k: record[k] for k in ("pk", "sk", "tenant", "session", "deleted_at")
            if k in record}
    return {**keep, "status": "deleted", "purged": True}


def _dynamo_safe(value: Any) -> Any:
    """
    Python to what DynamoDB's resource API accepts, all the way down.

    It refuses a float outright and wants Decimal. The echo backend never produced
    one, so nothing broke until a real model reported its cost as a float — at which
    point the first answered turn would have failed to commit. Converted through str
    so 0.1 stays 0.1 rather than becoming its binary approximation.
    """
    from decimal import Decimal
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_safe(v) for v in value]
    return value


def _plain_value(value: Any) -> Any:
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(v) for v in value]
    if isinstance(value, set):
        return [_plain_value(v) for v in sorted(value, key=str)]
    return value


def _marshal(value: Any) -> Any:
    """Python to DynamoDB's wire types, for the low-level transaction call."""
    from boto3.dynamodb.types import TypeSerializer
    serializer = TypeSerializer()
    if isinstance(value, dict):
        return {k: serializer.serialize(v) for k, v in value.items()}
    return serializer.serialize(value)


def from_environment() -> Store:
    """
    `DynamoStore` when the table names are set, `MemoryStore` otherwise.

    This is the seam the parity test rests on: the same code, and only the
    environment injection differs.
    """
    sessions = os.environ.get("YAIT_SESSIONS_TABLE", "").strip()
    events_table = os.environ.get("YAIT_EVENTS_TABLE", "").strip()
    if sessions and events_table:
        return DynamoStore(sessions, events_table)
    return MemoryStore()
