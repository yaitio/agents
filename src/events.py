"""
The internal event vocabulary — ours, not a dialect's.

Two façades render from these; neither dictates them. An event carries what
happened, and each dialect's serializer decides what to call it and which fields
to show. Naming them after one dialect would quietly make the other a translation
of the first.

The log is a **superset of both dialects**: it records turn boundaries and item
identity although the Anthropic dialect never exposes them, and both ways a tool
call is addressed — by event id, and by `turn_id` + `call_id`. A log shaped to one
dialect makes the other's read projections underivable. See docs/api.md.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

# ── what the fold reads: committed facts about the conversation ─────────────

SESSION_CREATED = "session.created"     #: always sequence 0
TURN_CANCELLED = "turn.cancelled"       #: an interrupt ended this queued turn too
SESSION_UPDATED = "session.updated"     #: title, metadata or agent overrides changed
SESSION_DELETED = "session.deleted"     #: the last event; the log is purged after it
USER_MESSAGE = "user.message"
AGENT_MESSAGE = "agent.message"
AGENT_TOOL_CALL = "agent.tool_call"     #: one event per reply, all its calls
TOOL_RESULT = "tool_result"             #: one per call, keyed by call_id
USER_TOOL_RESULT = "user.tool_result"   #: a client-side tool answered by the client
APPROVAL_DECIDED = "approval.decided"
USER_INTERRUPT = "user.interrupt"

#: The set the fold folds. Anything outside it is observational: it may be read by
#: a human or a dialect, and it never changes what the model sees.
FOLDED = frozenset({
    USER_MESSAGE, AGENT_MESSAGE, AGENT_TOOL_CALL, TOOL_RESULT,
    USER_TOOL_RESULT, APPROVAL_DECIDED, USER_INTERRUPT,
})

# ── what the fold ignores ──────────────────────────────────────────────────

TOOL_CALL_STARTED = "tool_call.started"  #: written before the side effect, never folded
APPROVAL_REQUESTED = "approval.requested"
MODEL_REQUEST_START = "span.model_request_start"
MODEL_REQUEST_END = "span.model_request_end"
#: The model deliberated before this answer. A progress signal, as in the original:
#: what it thought is not kept, and nothing reads it back.
AGENT_THINKING = "agent.thinking"
STEP_FAILED = "step.failed"
SESSION_STATUS = "session.status"
#: The transitions a client waits on. An Anthropic client streams until it sees
#: status_idle, so a turn that ends without one never ends as far as the client knows.
STATUS_RUNNING = "session.status_running"
#: What kind of failure ended a turn, carried as `error_type` on step.failed. Ours,
#: not either dialect's: each has its own names, and render.py translates.
FAILURES = ("budget", "billing", "rate_limited", "overloaded", "authentication",
            "invalid_request", "not_found", "connection_failed", "server_error",
            "mcp_authentication", "mcp_connection", "unknown")

STATUS_IDLE = "session.status_idle"
SESSION_USAGE = "session.usage"

# ── the states the session item carries ────────────────────────────────────
#
# Richer than either dialect, because each is a lossy projection and they lose
# different things. docs/architecture.md holds the mapping.

QUEUED = "queued"                #: input accepted, nothing has picked it up yet
WORKING = "working"              #: an invocation holds the lease
BETWEEN_STEPS = "between_steps"  #: committed, nothing running
OWES_ANSWER = "owes_answer"     #: a person or a client must answer
TURN_OVER = "turn_over"         #: the user has the floor
AT_CEILING = "at_ceiling"       #: a budget or step ceiling was reached
FAILED = "failed"
ARCHIVED = "archived"
DELETED = "deleted"              #: gone from the API; its data is being purged

ALIVE = frozenset({QUEUED, WORKING, BETWEEN_STEPS, OWES_ANSWER, TURN_OVER,
                   AT_CEILING})


def now_ms() -> int:
    """
    The clock everything here uses: integer milliseconds.

    Not whole seconds, because truncating to a second makes a one-second lease last
    between one and two. Not floats, because DynamoDB's resource API refuses them
    and wants Decimal. Milliseconds are the same integer everywhere.
    """
    return int(time.time() * 1000)


_monotonic = {"ms": 0, "n": 0}
_monotonic_lock = threading.Lock()


def new_id(prefix: str) -> str:
    """
    A time-ordered identifier: prefix, milliseconds, a counter, then randomness.

    **Why the counter.** Sorting by a timestamp is not a total order — two records
    created in the same millisecond tie, and a tie under pagination means a client
    walking pages sees an item twice or misses one. Putting the time in the id does
    not fix that on its own: within one millisecond the rest of the id is random, so
    the order is random too. The counter increments while the millisecond repeats,
    so ids from one process are monotonic, and sorting by the id agrees with the
    order they were made in.

    **What it does not fix, honestly.** Two processes writing in the same
    millisecond still tie, and there the phrase "creation order" has no meaning
    anyway. What pagination actually needs is *stability* — the same order on every
    read — and that holds either way, because the id itself is the key.

    Eleven hex digits of milliseconds run out in the year 5000; four of counter
    allow 65 536 ids per millisecond per process.
    """
    with _monotonic_lock:
        ms = now_ms()
        if ms == _monotonic["ms"]:
            _monotonic["n"] += 1
        else:
            _monotonic.update(ms=ms, n=0)
        counter = _monotonic["n"]
    return f"{prefix}_{ms:011x}{counter:04x}{uuid.uuid4().hex[:8]}"


def event(type_: str, *, turn_id: str | None = None, item_id: str | None = None,
          actor: str = "system", processed: bool = True, **payload: Any) -> dict:
    """
    One event, before it has a sequence number.

    The sequence is assigned by the write that wins, not by the caller — see
    `store.append`. So an event has no `seq` until it is committed, and the absence
    is deliberate rather than an omission to fill in later.

    `event_id` is separate from the sequence because clients dedupe by it: after a
    dropped stream, the consolidation both dialects prescribe merges the stream with
    the history and drops what it has seen, by id.

    `processed=False` is for input nobody has acted on yet. Both dialects have a
    native way to say so — `processed_at: null` in one, a `queued` turn in the other
    — so accepting a message before anything can answer it is not a pretence.
    """
    now = now_ms()
    body = {
        "type": type_,
        "event_id": new_id("evt"),
        "ts": now,
        "processed_at": now if processed else None,
        "actor": actor,
        "turn_id": turn_id,
        "item_id": item_id or new_id("item"),
    }
    body.update(payload)
    # processed_at is kept even when None: its absence and its nullness mean
    # different things to a client waiting for its message to be taken up.
    return {k: v for k, v in body.items() if v is not None or k == "processed_at"}
