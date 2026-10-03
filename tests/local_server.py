"""
The same handler over a stdlib HTTP server.

It synthesises an API Gateway REST proxy event and calls `handler` — the same
entry point Lambda calls, through the same resolver. So a contract test that
passes locally has exercised the production code path, not a second
implementation of it.

    python3 tests/local_server.py --port 8080

It lives beside the tests, not in `src/`, because `src/` is the function: the
deploy packages that directory wholesale, so anything in it ships to Lambda. This
never runs there.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import handler as H  # noqa: E402


def proxy_event(method: str, url: str, headers: dict, body: bytes) -> dict:
    """An API Gateway REST (payload format 1.0) proxy event."""
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    is_text = True
    try:
        text = body.decode()
    except UnicodeDecodeError:
        text, is_text = base64.b64encode(body).decode(), False
    return {
        "httpMethod": method,
        "path": parts.path,
        "resource": parts.path,
        "headers": dict(headers),
        "multiValueHeaders": {k: [v] for k, v in headers.items()},
        "queryStringParameters": {k: v[-1] for k, v in query.items()} or None,
        "multiValueQueryStringParameters": query or None,
        "pathParameters": None,
        "stageVariables": None,
        "body": text or None,
        "isBase64Encoded": not is_text,
        "requestContext": {
            "stage": "local",
            "httpMethod": method,
            "path": parts.path,
            "requestId": "local",
            "identity": {"sourceIp": "127.0.0.1"},
        },
    }


class LocalContext:
    """
    The four attributes Powertools' `inject_lambda_context` reads.

    Passing None breaks the decorator, and a local server that cannot exercise the
    decorators is a local server that tests a different function. So the context is
    faked rather than the decorator dropped.
    """

    function_name = "yait-agents-api-local"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:local:000000000000:function:yait-agents-api-local"
    aws_request_id = "local"


class _Handler(BaseHTTPRequestHandler):
    server_version = "yait-agents-skeleton"

    def _run(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        event = proxy_event(self.command, self.path,
                            {k: v for k, v in self.headers.items()}, body)

        response = H.handler(event, LocalContext())
        payload = (response.get("body") or "").encode()
        self.send_response(response.get("statusCode", 200))
        for k, v in (response.get("headers") or {}).items():
            self.send_header(k, str(v))
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _run

    def log_message(self, fmt: str, *args) -> None:
        print(json.dumps({"at": "local", "line": fmt % args}, ensure_ascii=False))


def main() -> int:
    # A local run may reach a model server on this machine — an Ollama, say. The
    # cloud never may: there, a named server is https and not local (endpoints.py).
    os.environ.setdefault("YAIT_ENDPOINTS_ALLOW_LOCAL", "1")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), _Handler)
    print(f"listening on http://{a.host}:{a.port} "
          f"({len(H.R.ROUTES)} routes, both dialects)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
