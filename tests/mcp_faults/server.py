"""
A test MCP server whose tools fail on purpose — for the tool loop's reliability,
and as a server the original agent APIs can reach too, so their behaviour under
the same failures can be recorded.

Every request needs a bearer token from MCP_FAULTS_TOKENS (`label=token,...`);
`whoami` says which one came, so a test can check that a session used the
credential its own vault holds. Nothing here is secret or real.

    MCP_FAULTS_TOKENS=alice=t1,bob=t2 python server.py      # :8020/mcp
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections import Counter

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

TOKENS = dict(pair.split("=", 1) for pair in
              os.environ.get("MCP_FAULTS_TOKENS", "").split(",") if "=" in pair)

ORDERS = {
    "5521": {"order_id": "5521", "customer": "Brightline Studio", "status": "shipped",
             "items": [{"sku": "MUG-BLUE", "qty": 2, "price": 12.5},
                       {"sku": "PEN-02", "qty": 4, "price": 4.5}], "total": 43.0},
    "5522": {"order_id": "5522", "customer": "Nordik AB", "status": "processing",
             "items": [{"sku": "CH-2201", "qty": 1, "price": 18900}], "total": 18900},
    "5523": {"order_id": "5523", "customer": "Heron Labs", "status": "cancelled",
             "items": [{"sku": "NB-11", "qty": 3, "price": 12.0}], "total": 36.0},
}

mcp = FastMCP("yait-mcp-faults")
_flaky: Counter = Counter()


def _who() -> str:
    auth = (get_http_headers(include_all=True) or {}).get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    for label, known in TOKENS.items():
        if hmac.compare_digest(known, token):
            return label
    return ""


@mcp.tool
def whoami() -> str:
    """Which test credential this call carried."""
    return _who()


@mcp.tool
def lookup_order(order_id: str) -> dict:
    """An order by its id: customer, status, items and total."""
    if order_id not in ORDERS:
        raise ToolError(f"no order {order_id}")
    return ORDERS[order_id]


@mcp.tool
def add(a: float, b: float) -> float:
    """a + b."""
    return a + b


@mcp.tool
def fail(message: str = "this tool always fails") -> str:
    """Always fails, with the message given."""
    raise ToolError(message)


@mcp.tool
async def slow(seconds: float) -> str:
    """Waits the given seconds (at most 120), then answers."""
    await asyncio.sleep(min(max(seconds, 0), 120))
    return f"waited {seconds} s"


@mcp.tool
def big(kilobytes: int = 512) -> str:
    """A very long answer, of the given size in kilobytes (at most 4096)."""
    line = "0123456789abcdef" * 4 + "\n"
    return line * (min(max(kilobytes, 1), 4096) * 1024 // len(line))


@mcp.tool
def flaky(key: str = "default") -> str:
    """Fails on every odd call for a key, succeeds on every even one."""
    _flaky[key] += 1
    if _flaky[key] % 2:
        raise ToolError(f"transient failure #{_flaky[key]} for {key!r}")
    return f"ok on call #{_flaky[key]} for {key!r}"


class RequireToken:
    """401 without a known bearer token — the answer a real server gives."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            auth = headers.get("authorization", "")
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            if not any(hmac.compare_digest(t, token) for t in TOKENS.values()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        if scope["type"] == "http" and scope["path"] == "/health":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await self.app(scope, receive, send)


app = RequireToken(mcp.http_app(path="/mcp", host_origin_protection=None))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8020")))
