#!/usr/bin/env python3
"""
Contract tests for the skeleton.

They run against a base URL, which is either the local server or a deployed
stage — the same tests, so what passes locally is what the deploy is checked
against. This is the smallest version of the parity test the specification asks
for: one suite, two environments, and the only permitted difference is the host.

    python3 tests/test_api.py                          # starts a local server
    python3 tests/test_api.py --base-url https://…/v1  # against a deployment

Stdlib only, deliberately: this runs on a fresh runner with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import conformance_harness as harness  # noqa: E402
import routes as R  # noqa: E402

#: The token the suite sends. Locally it is signed by a key pair made for the run,
#: whose public half the local server is started with; against a deployment it
#: has to be one the owner issued (scripts/keys.py), and --key says which.
KEY = harness.KEY


# ── a tiny assertion harness, so there is no dependency to install ──────────

class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []
        self.skipped: list[str] = []

    def skip(self, name: str, why: str) -> None:
        """Not run, and said so: a skipped check must never read as a pass."""
        self.skipped.append(f"{name}: {why}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print(f"\n{self.passed}/{total} checks passed"
              + (f", {len(self.skipped)} skipped" if self.skipped else ""))
        for s in self.skipped:
            print(f"  skip  {s}")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


#: `key=KEY` as a default would be evaluated once, when the function is defined —
#: so --key would set the global and every call would still send the old value. A
#: sentinel makes the default mean "whatever KEY is now".
DEFAULT_KEY = object()

#: Against a deployment, a model is called only with the caller's own provider key
#: — the installation pays for no tokens. A run without one cannot see a model
#: answer, so it checks instead that the turn was refused for want of a key, and
#: says which checks it could not run. Set by main().
WITHOUT_MODEL = False

#: The model the contract scenario's agent uses, and the key a client would bring
#: for it. Computed once: the suite is one client.
CONTRACT_MODEL = os.environ.get("YAIT_CONTRACT_MODEL", "gpt-6-luna")
PROVIDER_KEY = harness.provider_key_for(CONTRACT_MODEL)


def call(base: str, method: str, path: str, *, key=DEFAULT_KEY,
         header: str = "x-api-key", provider: str | None = None,
         body: dict | None = None) -> tuple[int, dict]:
    """
    `key` is the installation key, sent as X-Yait-Key. `provider` is a provider key,
    sent in the standard header named by `header` — by default the key for the
    contract model from the environment, as a client brings its own. Without one a
    deployment will not call the model: the installation never pays for tokens.
    """
    if key is DEFAULT_KEY:
        key = KEY
    if provider is None:
        provider = PROVIDER_KEY
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    if key:
        req.add_header("X-Yait-Key", key)
    if provider:
        req.add_header(header, provider if header == "x-api-key" else f"Bearer {provider}")
    if method in ("POST", "PUT", "PATCH"):
        req.add_header("content-type", "application/json")
        req.data = json.dumps(body or {}).encode()
    # Always a mapping, never a string. A body that is not JSON — which is what a
    # deleted API, a proxy, or a network error gives you — used to come back as a
    # str and every `body.get(...)` raised AttributeError. A verification suite
    # must fail, not crash: a traceback is indistinguishable from a broken test.
    def wrap(raw: bytes | str) -> dict:
        text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError:
            return {"__not_json__": text[:200]}

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, wrap(r.read())
    except urllib.error.HTTPError as e:
        return e.code, wrap(e.read())
    except urllib.error.URLError as e:
        return 0, {"__unreachable__": str(e)}


def sample_path(dialect: str, path: str) -> str:
    """A concrete path for a template, with plausible ids substituted."""
    out = R.full_path(dialect, path)
    for name in R.path_params(path):
        out = out.replace("{%s}" % name, f"x-{name.replace('_', '-')}-1")
    return out


# ── the suite ──────────────────────────────────────────────────────────────

def run(base: str) -> int:
    res = Results()

    # 1. Every declared route answers, and resolves to the operation it should.
    #    Operations with behaviour behind them are checked by their own suite: they
    #    no longer echo an operation_id, and loosening this check to accommodate
    #    them would quietly stop it testing anything.
    for dialect, method, path, op_id, _ in R.ROUTES:
        if op_id in R.IMPLEMENTED:
            continue
        url_path = sample_path(dialect, path)
        status, body = call(base, method, url_path)
        res.check(f"{method} {url_path} -> 200", status == 200, f"got {status}: {body}")
        res.check(f"{method} {url_path} dialect", body.get("dialect") == dialect,
                  f"expected {dialect!r}, got {body.get('dialect')!r}")
        res.check(f"{method} {url_path} operation", body.get("operation_id") == op_id,
                  f"expected {op_id!r}, got {body.get('operation_id')!r}")

    # 2. Path parameters are captured, not swallowed by a greedy proxy. On a route
    #    that is still a stub, because the real ones answer 404 for an id that does
    #    not exist — which is correct, and tells us nothing about capture.
    status, body = call(base, "GET", "/anthropic/v1/sessions/sess-42/events/stream")
    res.check("path parameter captured",
              body.get("path_parameters", {}).get("session_id") == "sess-42",
              f"got {body}")

    # 3. The prefix decides the dialect for a path the two dialects share.
    #    Both declare GET /agents; only the prefix tells them apart. Now that the
    #    route is real, the tell is the envelope rather than an echoed name:
    #    Anthropic paginates with cursors, OpenAI marks the list with `object`.
    _, a_body = call(base, "GET", "/anthropic/v1/agents")
    _, o_body = call(base, "GET", "/openai/v1/agents")
    res.check("same path, different dialect by prefix",
              "data" in a_body and "object" not in a_body
              and o_body.get("object") == "list",
              f"{a_body} / {o_body}")

    # 4. OpenAI's static /agents/sessions wins over /agents/{agent_id}.
    #    Now that the route is real, the tell is what comes back: a list of
    #    sessions, not a 404 for an agent whose id is "sessions".
    status, body = call(base, "GET", "/openai/v1/agents/sessions")
    res.check("static segment beats path parameter",
              status == 200 and body.get("object") == "list",
              f"got {status}: {body}")

    # 5. A provider key rides in either SDK's convention beside the installation
    #    key, and is passed on rather than checked.
    for header in ("x-api-key", "authorization"):
        status, _ = call(base, "GET", "/anthropic/v1/sessions", header=header,
                         provider="sk-a-provider-key")
        res.check(f"a provider key in {header} is passed on", status == 200,
                  f"got {status}")

    # 6. Without the installation key, nobody gets in — not even with a provider
    #    key, which is the old way of calling and must not be mistaken for ours.
    status, body = call(base, "GET", "/anthropic/v1/sessions", key=None)
    res.check("no installation key -> 401", status == 401, f"got {status}: {body}")
    status, body = call(base, "GET", "/anthropic/v1/sessions", key=None,
                        provider=KEY)
    res.check("our key in the provider's header is not an installation key",
              status == 401, f"got {status}: {body}")
    # Anything that is not a token this installation signed is refused.
    status, body = call(base, "GET", "/anthropic/v1/sessions",
                        key=f"stranger-{time.time_ns()}")
    res.check("a string that is not a token is refused", status == 401,
              f"got {status}: {body}")
    import tokens
    forged = tokens.issue(tokens.keypair()[0], kid=harness.LOCAL_KID, tenant="default")
    status, body = call(base, "GET", "/anthropic/v1/sessions", key=forged)
    res.check("a token signed by some other key is refused", status == 401,
              f"got {status}: {body}")

    # 7. An undeclared path is a 404 in our own error shape — in both
    #    environments. On a deployment that holds only because the AWS variant of
    #    the document adds a catch-all; without it API Gateway would answer its
    #    own 403 "Missing Authentication Token" and the two environments would
    #    disagree. The stage root is deliberately not in this list: `/{proxy+}`
    #    matches paths with at least one segment, and the root of a stage is not
    #    part of our surface.
    #    The envelopes differ, and that is the point: the Anthropic SDK parses
    #    `{"type": "error", "error": {...}}`, the OpenAI SDK parses `{"error":
    #    {message, type, param, code}}`. A single shared envelope would break one of
    #    them, so each façade renders its own and this asserts both.
    for path in ("/anthropic/v1/nonsense", "/openai/v1/agents/sessions/x/nonsense",
                 "/nonsense", "/openai/v2/agents"):
        status, body = call(base, "GET", path)
        res.check(f"unknown {path} -> 404", status == 404, f"got {status}")
        if path.startswith("/openai/"):
            res.check(f"unknown {path} in the OpenAI error shape",
                      set(body.get("error", {})) >= {"message", "type", "param",
                                                     "code"},
                      f"got {body}")
        else:
            res.check(f"unknown {path} in the Anthropic error shape",
                      body.get("type") == "error"
                      and set(body.get("error", {})) == {"type", "message"},
                      f"got {body}")

    # 8. A declared path under a wrong method is a 405.
    status, _ = call(base, "DELETE", "/anthropic/v1/sessions")
    res.check("wrong method -> 405", status == 405, f"got {status}")

    # 9. The stream route answers and says plainly that this transport cannot
    #    carry it — so nobody mistakes the stub for something that could become
    #    real on API Gateway.
    status, body = call(base, "GET", "/anthropic/v1/sessions/s1/events/stream")
    res.check("stream route is marked", status == 200 and "note" in body,
              f"got {status}: {body}")

    # 10. One session, two dialects — the claim slice two exists to prove.
    #     Everything below goes over HTTP, so it runs against a deployment exactly
    #     as it runs locally.
    oai = {"header": "authorization"}
    # The cheapest model that proves the path: every deploy runs this against a real
    # provider, so it should cost fractions of a cent, not a real task's worth.
    _, agent = call(base, "POST", "/anthropic/v1/agents",
                    body={"model": CONTRACT_MODEL,
                          "system": "Reply with one short sentence.",
                          "name": "contract-test"})
    status, sess = call(base, "POST", "/anthropic/v1/sessions",
                        body={"agent": agent.get("id"), "environment_id": "env_none"})
    res.check("a session is created through the Anthropic tree",
              status == 200 and sess.get("status") == "idle", f"{status} {sess}")
    sid = sess.get("id", "missing")

    status, sent = call(base, "POST", f"/anthropic/v1/sessions/{sid}/events", body={
        "events": [{"type": "user.message", "content": [{"type": "text", "text": "one"}]},
                   {"type": "user.message", "content": [{"type": "text", "text": "two"}]}]})
    res.check("messages are accepted and marked unprocessed",
              status == 200 and [e.get("processed_at") for e in sent.get("data", [])]
              == [None, None], f"{status} {sent}")

    # The worker answers asynchronously in the cloud and inline locally, so wait for
    # the floor to come back rather than asserting a moment in between. The window
    # between "running" and "idle" is a unit test's business (test_sessions,
    # test_worker); over HTTP it is a race.
    a_view = {}
    for _ in range(240):  # two minutes: a cold worker importing the library, then a call
        _, a_view = call(base, "GET", f"/anthropic/v1/sessions/{sid}")
        if a_view.get("status") == "idle":
            break
        time.sleep(0.5)
    _, o_view = call(base, "GET", f"/openai/v1/agents/sessions/{sid}", **oai)
    res.check("the worker answered and handed the floor back, in both views",
              a_view.get("status") == "idle" and o_view.get("status") == "idle",
              f"{a_view.get('status')} / {o_view.get('status')}")

    _, turns = call(base, "GET", f"/openai/v1/agents/sessions/{sid}/turns", **oai)
    if WITHOUT_MODEL:
        codes = [(x.get("status"), (x.get("error") or {}).get("code"))
                 for x in turns.get("data", [])]
        res.check("with no provider key the turn is refused, not paid for by anyone",
                  codes == [("failed", "authentication_error")] * 2, f"{codes}")
        for name in ("two messages, two turns, both completed",
                     "the model answered each message with text",
                     "the Anthropic events follow the documented order"):
            res.skip(name, "no provider key for the contract model — run it as a "
                           "client with one: " + CONTRACT_MODEL)
        status, done = call(base, "POST", f"/anthropic/v1/sessions/{sid}/archive")
        res.check("an idle session archives",
                  status == 200 and done.get("status") == "terminated", f"{status} {done}")
        return res.report()
    # One message, one turn, one answer — as the original answers each, even two
    # sent in one request.
    res.check("two messages, two turns, both completed",
              [x.get("status") for x in turns.get("data", [])] == ["completed"] * 2,
              f"{turns}")
    _, items = call(base, "GET",
                    f"/openai/v1/agents/sessions/{sid}/items?order=asc", **oai)
    _, evs = call(base, "GET", f"/anthropic/v1/sessions/{sid}/events")
    errors = [e.get("error", {}).get("message") for e in evs.get("data", [])
              if e.get("type") == "session.error"]
    # If the turn failed, say why in the failure itself — "credit balance too low"
    # is a different problem from "the fold dropped a message", and the log line is
    # the only place a release reports it.
    why = f" — the turn failed: {errors}" if errors else ""
    roles = [(i.get("role"), i.get("content", [{}])[0].get("text", ""))
             for i in items.get("data", [])]
    res.check("both messages are user items, in order",
              roles[:2] == [("user", "one"), ("user", "two")], f"{roles}{why}")
    replies = [text for role, text in roles if role == "assistant"]
    if replies and replies[0].startswith("echo:"):
        # The echo backend is deterministic, so hold it to the exact answers — each
        # quotes the message it answers, as the fold built the conversation.
        res.check("each message got its own answer",
                  replies == ["echo: one", "echo: two"], f"{replies}{why}")
    else:
        # A real model answers in its own words. What is fixed is the shape: two
        # assistant items, one per message, each with text.
        res.check("the model answered each message with text",
                  len(replies) == 2 and all(r.strip() for r in replies),
                  f"{roles}{why}")

    kinds = [e.get("type") for e in evs.get("data", [])]
    res.check("the Anthropic events follow the documented order",
              # The original's order: running is said before the message that set
              # it going, and the usage snapshot closes the turn. Its thread events
              # are a known divergence (tests/behaviour.py); the echo model does not think.
              # A queued message is listed when it is taken up, after the answer
              # before it — the original's order.
              kinds == ["session.status_running",
                        "user.message", "span.model_request_start", "agent.message",
                        "span.model_request_end",
                        "user.message", "span.model_request_start", "agent.message",
                        "span.model_request_end",
                        "session.usage", "session.status_idle"],
              f"{kinds}{why}")
    res.check("and the messages now read as processed",
              all(e.get("processed_at") for e in evs.get("data", [])
                  if e.get("type") == "user.message"), f"{evs}")

    status, done = call(base, "POST", f"/anthropic/v1/sessions/{sid}/archive")
    res.check("an idle session archives",
              status == 200 and done.get("status") == "terminated", f"{status} {done}")

    status, err = call(base, "POST", "/openai/v1/agents/sessions", **oai,
                       body={"agent_id": agent.get("id"),
                             "environment": {"type": "openai_hosted"}, "input": "x"})
    res.check("an environment we cannot honour is refused by name",
              status == 400 and "none" in err.get("error", {}).get("message", ""),
              f"{status} {err}")

    return res.report()


def spec_checks() -> int:
    """The document is a build artifact; assert it has not drifted, and is sane."""
    res = Results()
    build = ROOT / "openapi" / "build.py"
    out = subprocess.run([sys.executable, str(build), "--check"],
                         capture_output=True, text=True)
    res.check("openapi/agents.json is current", out.returncode == 0,
              (out.stdout + out.stderr).strip())

    doc_path = ROOT / "openapi" / "agents.json"
    if doc_path.exists():
        doc = json.loads(doc_path.read_text())
        res.check("openapi is 3.0.x (API Gateway cannot import 3.1)",
                  doc.get("openapi", "").startswith("3.0"), doc.get("openapi"))
        declared = {(m.upper(), p) for p, item in doc["paths"].items() for m in item}
        expected = {(m.upper(), R.full_path(d, p)) for d, m, p, _, _ in R.ROUTES}
        res.check("document covers the route table exactly",
                  declared == expected,
                  f"missing {sorted(expected - declared)}, extra {sorted(declared - expected)}")
        res.check("no AWS extension leaked into the neutral document",
                  "x-amazon-apigateway-integration" not in doc_path.read_text())
        res.check("the neutral document has no catch-all",
                  "/{proxy+}" not in doc["paths"])

        # operationId must be unique across the whole document — OpenAPI requires
        # it, and a generated client turns each one into a method name. Both
        # dialects declare GET /agents, so without the dialect prefix they would
        # collide and the document would be invalid. Checking the prefix as well
        # makes that impossible by construction rather than by luck.
        ids = [op["operationId"]
               for item in doc["paths"].values() for op in item.values()]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        res.check("operationIds are unique", not duplicates, f"duplicated: {duplicates}")
        mislabelled = [
            (op["operationId"], tag)
            for item in doc["paths"].values() for op in item.values()
            for tag in op["tags"] if not op["operationId"].startswith(tag)
        ]
        res.check("each operationId begins with its dialect", not mislabelled,
                  f"{mislabelled}")

        # The AWS variant is what the gateway imports. Two things about it are
        # load-bearing and neither is visible in the neutral document.
        sys.path.insert(0, str(ROOT / "openapi"))
        import build as B  # noqa: PLC0415
        aws = B.with_aws(B.document(), cfn=True)
        res.check("the AWS variant adds the catch-all that keeps 404 behaviour equal",
                  "/{proxy+}" in aws["paths"])
        integrations = [
            op["x-amazon-apigateway-integration"]
            for item in aws["paths"].values() for op in item.values()
        ]
        res.check("every operation carries an integration",
                  len(integrations) == sum(len(v) for v in aws["paths"].values()))
        res.check("no payloadFormatVersion (an HTTP API key a REST import rejects)",
                  not any("payloadFormatVersion" in i for i in integrations))
    else:
        res.check("openapi/agents.json exists", False, "not found")
    return res.report()


def self_test() -> int:
    """
    Check that the suite fails when there is nothing to test.

    This exists because it once did the opposite. The workflow passed an empty
    `--base-url`, an empty string is falsy, and the suite quietly started a local
    server and reported 106 passing checks "against the deployment" — for a deploy
    that had created nothing. A fallback inside a verification path turns a failure
    into a false pass, and nothing else in CI can catch that.
    """
    res = Results()
    cases = [
        ("an unreachable host",  ["--base-url", "https://nothing-here.invalid/prod"]),
        ("an empty base url",    ["--base-url", ""]),
        ("a base url of spaces", ["--base-url", "   "]),
    ]
    for name, args in cases:
        out = subprocess.run(
            [sys.executable, __file__, *args, "--skip-spec"],
            capture_output=True, text=True,
        )
        res.check(f"{name} fails", out.returncode != 0,
                  f"exit={out.returncode} — it reported success with nothing to test")
    return res.report()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", help="a deployed stage; omitted starts a local server")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--skip-spec", action="store_true")
    ap.add_argument("--key", help="the client key to send; defaults to the local one")
    ap.add_argument("--self-test", action="store_true",
                    help="check that the suite fails when there is nothing to test")
    a = ap.parse_args()

    if a.key:
        global KEY
        KEY = a.key

    if a.self_test:
        print("== the suite must fail when there is nothing to test ==")
        return self_test()

    rc = 0 if a.skip_spec else spec_checks()

    if a.base_url is not None:
        # An empty value is an error, never a fallback. Passing --base-url "" and
        # silently testing a local server is how a suite reports 106 passing checks
        # against a deployment that does not exist.
        if not a.base_url.strip():
            print("--base-url was given but empty. Refusing to fall back to a "
                  "local server: that would report a pass for a deployment "
                  "nobody tested.", file=sys.stderr)
            return 2
        global WITHOUT_MODEL
        WITHOUT_MODEL = PROVIDER_KEY is None
        print(f"\n== against {a.base_url} =="
              + (" — no provider key, so no model is called" if WITHOUT_MODEL else ""))
        return run(a.base_url) or rc

    print(f"\n== against a local server on :{a.port} ==")
    # The local server needs a key configured, because a deployment without one
    # refuses everything — which is the correct default and has to be exercised.
    environment = {**os.environ, "YAIT_JWT_PUBLIC_KEYS": harness.LOCAL_PUBLIC_KEYS}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "local_server.py"), "--port", str(a.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
    )
    try:
        base = f"http://127.0.0.1:{a.port}"
        for _ in range(50):
            if call(base, "GET", "/anthropic/v1/agents")[0]:
                break
            time.sleep(0.1)
        else:
            print("local server did not start")
            return 1
        return run(base) or rc
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
