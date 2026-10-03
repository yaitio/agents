#!/usr/bin/env python3
"""
Agents on real tasks, compared: the same model on two platforms, or two models on
ours.

An **arm** is a platform and a model — `ours:gpt-6-luna`,
`anthropic:claude-haiku-4-5-20251001`, `openai:gpt-6-luna`; a bare model name is
`ours:`. Which comparison a pair of arms makes follows from what differs:

* **same model, different platform** — does our engine do what the original does?
  Judged on **equivalence**: the paired accuracy difference within ±δ, at 90%
  two-sided. Time and tokens are reported as ratios beside it, because "the same
  answers, twice as slow" is a finding too.
* **same platform, different model** — can the model be switched? Judged on
  **non-inferiority**: the candidate no worse than the baseline by more than δ, at
  95% one-sided.

Either way, **gates** come first — invariants any model must hold: it answers, and
it obeys the instructions a case sets. One violation is a stop, whatever the
averages say. **Cost per successful task** is printed beside the verdict and never
folded into it: how price trades against accuracy is a decision, not a computation.

`aichain.eval` does the running and the counting — arms × cases × trials into a
durable ledger that resumes where it stopped; mean, pass^k and flips per arm.

Real money, so it never runs in CI and never runs whole unannounced: the scorers
against known-right and known-wrong answers first (free), two cases through every
arm (cents), and the whole run only with `--run`.

    python3 evals/quality.py --base-url https://… --key … \\
        --arms ours:claude-haiku-4-5-20251001,ours:gpt-6-luna   # controls, smoke
    python3 evals/quality.py … --run --trials 3                 # the whole run
    ANTHROPIC_API_KEY=… python3 evals/quality.py --base-url … --key … \\
        --arms anthropic:claude-haiku-4-5-20251001,ours:claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import pathlib
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict

import anthropic
from yait_aichain.eval import Case, Eval, Report, contains, normalise, regex
from yait_aichain.eval._records import Record

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import conformance_harness as harness  # noqa: E402
import results as R  # noqa: E402

RUNS = ROOT / "evals" / "runs"
DEFAULT_INSTRUCTIONS = "Answer briefly."
NOISE = "I don't know."
MIN_CASES = 10                   # below this, no margin worth having is testable
Z_95_ONE_SIDED = 1.645           # also the 90% two-sided bound equivalence uses
TURN_WAIT_S = 300
PLATFORMS = ("ours", "anthropic", "openai")
ORIGINAL = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


# ── cases, scoring, gates ──────────────────────────────────────────────────

def load_cases(path: pathlib.Path) -> list[Case]:
    """
    One JSON object per line. `input` is one message or a list of them, sent one
    turn at a time; `documents` are repository paths put in front of the first
    message; the answer scored and gated is the one to the last message.
    """
    cases = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        missing = {"id", "input", "score", "expect", "oracle"} - set(row)
        if missing:
            raise ValueError(f"{path.name}:{n}: missing {', '.join(sorted(missing))}")
        turns = row["input"] if isinstance(row["input"], list) else [row["input"]]
        if row.get("documents"):
            text = "\n\n".join(f"<document name=\"{d}\">\n{(ROOT / d).read_text()}\n"
                               f"</document>" for d in row["documents"])
            turns = [f"{text}\n\n{turns[0]}"] + turns[1:]
        if row.get("documents_inline"):
            turns = [f"<document>\n{row['documents_inline']}\n</document>\n\n{turns[0]}"] + turns[1:]
        cases.append(Case(id=row["id"], input=turns, expect=row["expect"],
                          group=row.get("group", ""),
                          meta={k: row.get(k) for k in
                                ("score", "oracle", "instructions", "must", "must_not",
                                 "checker", "level", "mcp", "mcp_as", "must_call",
                                 "must_not_call", "verify")}))
    return cases


def _numbers(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", str(text or "").replace(",", ""))]


def _has_number(case: Case, output) -> tuple[bool, float]:
    """
    The expected number appears in the answer. Not "the first" or "the last":
    a worked answer states its result and then restates the question around it —
    "22.1 litres for 340 km" — and either rule fails a right answer. Safe only
    because the controls check that the expected number is not in the question,
    so echoing the question cannot pass.
    """
    ok = any(abs(n - float(case.expect)) < 1e-9 for n in _numbers(output))
    return ok, 1.0 if ok else 0.0


def _json_value(text: str):
    """The JSON object or array in an answer — fenced or not, with prose around it."""
    text = re.sub(r"```(?:json)?", "", str(text or ""))
    # Whichever opens first: an array of objects is not its first object.
    pairs = sorted((("{", "}"), ("[", "]")),
                   key=lambda pair: (text.find(pair[0]) < 0, text.find(pair[0])))
    for open_, close in pairs:
        start, end = text.find(open_), text.rfind(close)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _json_object(text: str):
    got = _json_value(text)
    return got if isinstance(got, dict) else None


def _same(want, got, *, exact: bool = False) -> bool:
    """
    Equal as a person would read it: 1284.5 is "1284.50", «Ромашка» is Ромашка —
    and, for objects and arrays, the same all the way down. Loose, an object needs
    the expected keys and may have more; exact, it has exactly those.
    """
    if isinstance(want, dict):
        return (isinstance(got, dict) and all(k in got and _same(v, got[k], exact=exact)
                                              for k, v in want.items())
                and (not exact or set(got) == set(want)))
    if isinstance(want, list):
        return (isinstance(got, list) and len(got) == len(want)
                and all(_same(w, g, exact=exact) for w, g in zip(want, got)))
    if isinstance(want, bool) or want is None:
        return got == want
    if isinstance(want, (int, float)):
        try:
            return abs(float(str(got).replace(",", "")) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    squash = (lambda x: re.sub(r"[\W_]+", "", str(x)).casefold())
    return squash(want) == squash(got)


def _json_fields(case: Case, output) -> tuple[bool, float]:
    """Every expected field present and the same, to any depth; extra fields do not count."""
    got = _json_value(str(output or ""))
    want = case.expect
    if isinstance(want, list):
        ok = _same(want, got)
        return ok, 1.0 if ok else 0.0
    if not isinstance(got, dict):
        return False, 0.0
    hits = sum(1 for k, v in want.items() if k in got and _same(v, got[k]))
    return hits == len(want), hits / len(want)


def _json_exact(case: Case, output) -> tuple[bool, float]:
    """The same JSON, to any depth, with nothing missing and nothing added."""
    ok = _same(case.expect, _json_value(str(output or "")), exact=True)
    return ok, 1.0 if ok else 0.0


def _output(case: Case, output) -> tuple[bool, float]:
    """A program's output, line by line: fences and surrounding blank lines aside."""
    text = re.sub(r"```\w*", "", str(output or "")).strip("\n")
    got = [line.rstrip() for line in text.strip().splitlines()]
    want = [line.rstrip() for line in str(case.expect).strip().splitlines()]
    return got == want, 1.0 if got == want else 0.0


def _checker(case: Case, output) -> tuple[bool, float]:
    """A case whose right answers are many, judged by a rule of its own (checkers.py)."""
    import checkers
    return checkers.CHECKS[case.meta["checker"]](case.expect, output)


_SCORERS = {"contains": contains(), "regex": regex(), "number": _has_number,
            "json": _json_fields, "json_exact": _json_exact, "output": _output,
            "checker": _checker}


def score(case: Case, output):
    kind = case.meta.get("score")
    if kind not in _SCORERS:
        raise ValueError(f"case {case.id}: no scorer {kind!r}")
    return _SCORERS[kind](case, output)


def gates(case: Case, answer: str, stop_reason: str) -> list[str]:
    """The invariants this attempt broke. Empty is the only acceptable answer."""
    broken = []
    if stop_reason != "end_turn" or not answer.strip():
        broken.append(f"answered (stop_reason {stop_reason!r}, "
                      f"{len(answer.strip())} characters)")
    for pattern in case.meta.get("must") or []:
        if not re.search(pattern, answer):
            broken.append(f"must match {pattern!r}")
    for pattern in case.meta.get("must_not") or []:
        if re.search(pattern, answer):
            broken.append(f"must not match {pattern!r}")
    return broken


# ── tools: the MCP servers a case may name, and whose credential it uses ────

#: Each server: its address, and the environment variable holding the credential
#: for each identity a case may act as (A by default). The same server, key and
#: vault shape on every platform — ours and the originals — so a comparison
#: changes one thing.
MCP_SERVERS = {
    "faults": {"url_env": "MCP_FAULTS_URL",
               "keys": {"A": "MCP_FAULTS_TOKEN_A", "B": "MCP_FAULTS_TOKEN_B"}},
    "idntty": {"url": "https://mcp.idntty.io/parley/",
               "keys": {"A": "IDNTTY_KEY_A", "B": "IDNTTY_KEY_B"}},
    "exa": {"url": "https://mcp.exa.ai/mcp", "keys": {"A": "EXA_API_KEY", "B": "EXA_API_KEY"}},
    "stash": {"url": "https://mcp.idntty.io/stash/",
              "keys": {"A": "IDNTTY_KEY_A", "B": "IDNTTY_KEY_B"}},
}


# ── cases whose result is outside the answer ───────────────────────────────
#
# A case may do something in the world — publish a file, send a message — and
# say how to check it: after the attempt, a client of our own, with the key of
# the agent it acted as or wrote to, looks. Each attempt gets its own nonce in
# place of {nonce}, so parallel attempts and arms never read each other's work;
# what an attempt published is removed once it has been checked.

def personalize(case: Case) -> tuple[Case, str]:
    import secrets
    nonce = secrets.token_hex(4)
    if "{nonce}" not in json.dumps(case.input):
        return case, nonce
    return Case(id=case.id, input=[t.replace("{nonce}", nonce) for t in case.input],
                expect=case.expect, group=case.group, meta=case.meta), nonce


def _mcp_tools(server: str, who: str) -> dict:
    from yait_aichain.tools import MCPTools
    spec = MCP_SERVERS[server]
    url = spec.get("url") or _need(spec["url_env"])
    key = _need(spec["keys"][who])
    return {t.name: t for t in MCPTools(url, headers={"Authorization": f"Bearer {key}"})}


def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def verify(case: Case, nonce: str) -> list[str]:
    """What the case says must be true outside the answer, and is not."""
    failed = []
    fill = lambda v: json.loads(json.dumps(v).replace("{nonce}", nonce))
    for check in fill(case.meta.get("verify") or []):
        try:
            if check.get("file"):
                site, path = check["file"]["site"], check["file"]["path"]
                got = _mcp_tools("stash", check.get("as", "A"))["file_read"].run(
                    input={"site": site, "path": path})
                text = _as_text(got.get("content", got) if isinstance(got, dict) else got)
                for pattern in check.get("expect") or []:
                    if not re.search(pattern, text):
                        failed.append(f"{site}/{path} does not match {pattern!r}")
            elif check.get("message_from"):
                tools = _mcp_tools("idntty", check.get("as", "B"))
                channels = json.loads(_as_text(tools["channel_info"].run(input={})))
                texts = []
                for ch in channels.get("channels") or []:
                    if check["message_from"] in json.dumps(ch.get("participants") or ch):
                        read = json.loads(_as_text(tools["message_read"].run(
                            input={"channel_id": ch["channel_id"], "wait": False,
                                   "limit": 50})))
                        texts += [m.get("text", "") for m in read.get("messages") or []
                                  if m.get("handle") == check["message_from"]]
                mine = [t for t in texts if nonce in t]
                if not mine:
                    failed.append(f"no message from {check['message_from']} carries {nonce}")
                for pattern in check.get("expect") or []:
                    if mine and not any(re.search(pattern, t) for t in mine):
                        failed.append(f"the message does not match {pattern!r}")
        except Exception as exc:  # noqa: BLE001 — a check that cannot run has not passed
            failed.append(f"could not check {check}: {exc}"[:300])
    return failed


def clean_up(case: Case, nonce: str) -> None:
    for check in case.meta.get("verify") or []:
        if check.get("file"):
            try:
                _mcp_tools("stash", check.get("as", "A"))["file_delete"].run(input={
                    "site": check["file"]["site"],
                    "paths": [check["file"]["path"].replace("{nonce}", nonce)]})
            except Exception:  # noqa: BLE001 — tidiness, not a result
                pass


def checked(attempt):
    """An attempt, then the case's checks outside the answer, then the tidy-up."""
    def run(self, model: str, case: Case) -> dict:
        case, nonce = personalize(case)
        outcome = attempt(self, model, case)
        if case.meta.get("verify"):
            outcome["gates"] = outcome["gates"] + verify(case, nonce)
            outcome["nonce"] = nonce
            clean_up(case, nonce)
        return outcome
    return run


def mcp_for(case: Case) -> list[tuple[str, str, str]]:
    """(name, url, credential) for each server the case names."""
    out = []
    for name in case.meta.get("mcp") or []:
        spec = MCP_SERVERS[name]
        url = spec.get("url") or _need(spec["url_env"])
        out.append((name, url, _need(spec["keys"][case.meta.get("mcp_as") or "A"])))
    return out


def tool_gates(case: Case, calls: list[dict]) -> list[str]:
    """What the case says about the tools: called, or left alone."""
    names = [c["name"] for c in calls]
    broken = [f"did not call {n}" for n in case.meta.get("must_call") or [] if n not in names]
    broken += [f"called {n}, which it did not need" for n in
               case.meta.get("must_not_call") or [] if n in names]
    return broken


def _priced(model: str, used) -> float | None:
    from yait_aichain.models._usage import estimate_cost
    return estimate_cost(used, model)


# ── platforms ──────────────────────────────────────────────────────────────

class AnthropicDialect:
    """
    Ours, or Anthropic's own Managed Agents: the same official SDK, a different
    host. Each attempt is a fresh session on an agent with the arm's model and the
    case's instructions; turns are sent one at a time, each waited for. What it
    cost is measured from the tokens the session reports per request, priced by the
    same table the server's budget uses — a model with no price is unpriced, never
    free.
    """

    def __init__(self, base: str, key: str, *, original: bool = False) -> None:
        self.original, self.base, self.key = original, base, key
        # Against the original, `key` is the provider key. Against us it is the
        # installation key, and each model is called with its provider's key from
        # the environment, passed through as a client would pass its own — the
        # same key the original arm uses, so the comparison changes one thing.
        self.c = self._client(None)
        self._clients: dict = {}
        self._agents: dict = {}
        self._vaults: dict = {}
        self._environment: str | None = None if original else "env_none"
        self._lock = threading.Lock()

    def _client(self, model: str | None):
        if self.original:
            return harness.anthropic_client(self.base, provider_key=self.key,
                                            original=True, max_retries=2, timeout=300)
        return harness.anthropic_client(self.base, yait_key=self.key,
                                        provider_key=_provider_key(model),
                                        max_retries=2, timeout=300)

    def client_for(self, model: str):
        with self._lock:
            if model not in self._clients:
                self._clients[model] = self._client(model)
            return self._clients[model]

    def _setup(self, model: str, instructions: str,
                mcp: tuple = ()) -> tuple[str, str, list[str]]:
        with self._lock:
            if self._environment is None:
                self._environment = self.c.beta.environments.create(
                    name="yait-quality").id
            servers = tuple((name, url) for name, url, _ in mcp)
            if (model, instructions, servers) not in self._agents:
                extra = {}
                if servers:
                    extra = {"mcp_servers": [{"type": "url", "name": n, "url": u}
                                             for n, u in servers],
                             "tools": [{"type": "mcp_toolset", "mcp_server_name": n,
                                        "default_config": {"permission_policy":
                                                           {"type": "always_allow"}}}
                                       for n, _ in servers]}
                self._agents[(model, instructions, servers)] = self.c.beta.agents.create(
                    model=model, name="quality", system=instructions, **extra).id
            # One vault per set of credentials, on the platform under test: its
            # own vault, holding the same key, the way a user of it would.
            vault_ids = []
            if mcp:
                if mcp not in self._vaults:
                    vault = self.c.beta.vaults.create(display_name="yait-quality")
                    for name, url, token in mcp:
                        self.c.beta.vaults.credentials.create(
                            vault.id, display_name=name,
                            auth={"type": "static_bearer", "mcp_server_url": url,
                                  "token": token})
                    self._vaults[mcp] = vault.id
                vault_ids = [self._vaults[mcp]]
        return self._agents[(model, instructions, servers)], self._environment, vault_ids
    

    @checked
    def attempt(self, model: str, case: Case) -> dict:
        from yait_aichain.models._usage import Usage
        agent_id, environment, vault_ids = self._setup(
            model, case.meta.get("instructions") or DEFAULT_INSTRUCTIONS,
            tuple(mcp_for(case)))
        c = self.client_for(model)
        session = c.beta.sessions.create(agent=agent_id, environment_id=environment,
                                         **({"vault_ids": vault_ids} if vault_ids else {}))
        turn_seconds = []
        try:
            for n, message in enumerate(case.input, start=1):
                started = time.monotonic()
                c.beta.sessions.events.send(session.id, events=[{
                    "type": "user.message", "content": [{"type": "text", "text": message}]}])
                events = self._until_turns_end(session.id, n)
                turn_seconds.append(round(time.monotonic() - started, 2))
            last, stop, used = [], "", Usage()
            model_seconds, started = 0.0, {}
            calls: list[dict] = []
            for raw in events:
                if raw["type"] == "agent.mcp_tool_use":
                    calls.append({"id": raw["id"], "name": raw.get("name"),
                                  "input": raw.get("input"), "is_error": None})
                elif raw["type"] == "agent.mcp_tool_result":
                    for call in calls:
                        if call["id"] == raw.get("mcp_tool_use_id"):
                            call["is_error"] = bool(raw.get("is_error"))
                # How long the model took, by the API's own clock: from each
                # request's start to its end. The rest of a turn is the platform.
                if raw["type"] == "span.model_request_start":
                    started[raw["id"]] = raw.get("processed_at")
                elif raw["type"] == "span.model_request_end":
                    began = started.get(raw.get("model_request_start_id"))
                    if began and raw.get("processed_at"):
                        model_seconds += (raw["processed_at"] - began).total_seconds()
                if raw["type"] == "user.message":
                    last = []                   # the answer scored is the last turn's
                elif raw["type"] == "agent.message":
                    last += [b.get("text", "") for b in raw.get("content") or []]
                elif raw["type"] == "session.error":
                    error = raw.get("error") or {}
                    raise RuntimeError(f"{error.get('type')}: {error.get('message')}")
                elif raw["type"] == "session.status_idle":
                    stop = (raw.get("stop_reason") or {}).get("type", "")
                elif raw["type"] == "span.model_request_end":
                    u = raw.get("model_usage") or {}
                    used = used + Usage(
                        input_tokens=u.get("input_tokens") or 0,
                        output_tokens=u.get("output_tokens") or 0,
                        cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                        cache_write_tokens=u.get("cache_creation_input_tokens") or 0)
        finally:
            try:
                self.c.beta.sessions.delete(session.id)
            except anthropic.APIError:
                pass
        return _outcome(case, model, "".join(last), stop, used, turn_seconds,
                        model_seconds=round(model_seconds, 3) if started else None,
                        calls=calls)

    def _until_turns_end(self, session_id: str, turns: int) -> list[dict]:
        """
        Every event, once `turns` turns have ended. Counted by the idle events rather
        than read off the status, which may still say idle for a moment after a
        message is sent to a host that has not taken it up yet.
        """
        deadline = time.monotonic() + TURN_WAIT_S
        while True:
            events = [e.model_dump() for e in
                      self.c.beta.sessions.events.list(session_id, order="asc")]
            ended = sum(1 for e in events if e["type"] == "session.status_idle")
            if ended >= turns or any(e["type"] == "session.status_terminated"
                                     for e in events):
                return events
            if time.monotonic() > deadline:
                raise TimeoutError(f"turn {turns} did not end in {TURN_WAIT_S}s")
            time.sleep(0.25)

    def close(self) -> None:
        for agent_id in self._agents.values():
            try:
                self.c.beta.agents.archive(agent_id)
            except anthropic.APIError:
                pass
        # The credentials go with the run: a test key left in a vault is a key
        # nobody is watching.
        for vault_id in self._vaults.values():
            try:
                self.c.beta.vaults.delete(vault_id)
            except anthropic.APIError:
                pass
        if self.original and self._environment:
            try:
                self.c.beta.environments.delete(self._environment)
            except anthropic.APIError:
                pass


class OpenAIOriginal:
    """
    OpenAI's own Agents API, over HTTP — their SDK has no Agents API yet. The answer
    is the last assistant message item; the tokens come from the turns' usage.
    """

    def __init__(self, key: str) -> None:
        self.base, self.key = ORIGINAL["openai"], key
        self._agents: dict = {}
        self._vaults: dict = {}
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method)
        for name, value in harness.openai_headers(provider_key=self.key).items():
            req.add_header(name, value)
        if body is not None:
            req.add_header("content-type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=120) as reply:
                raw = reply.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read()[:300]!r}") from exc
        return json.loads(raw) if raw else {}

    @checked
    def attempt(self, model: str, case: Case) -> dict:
        from yait_aichain.models._usage import Usage
        instructions = case.meta.get("instructions") or DEFAULT_INSTRUCTIONS
        mcp = tuple(mcp_for(case))
        servers = tuple((name, url) for name, url, _ in mcp)
        with self._lock:
            if (model, instructions, servers) not in self._agents:
                self._agents[(model, instructions, servers)] = self._call(
                    "POST", "/agents", {"model": model, "name": "quality",
                                        "instructions": instructions,
                                        **({"tools": [{"type": "mcp", "server_label": n,
                                                       "transport": {"type": "http",
                                                                     "server_url": u}}
                                                      for n, u in servers]}
                                           if servers else {})})["id"]
            agent_id = self._agents[(model, instructions, servers)]
            vault_ids = []
            if mcp:
                if mcp not in self._vaults:
                    vault = self._call("POST", "/vaults", {"name": "yait-quality"})
                    for name, url, token in mcp:
                        self._call("POST", f"/vaults/{vault['id']}/credentials",
                                   {"name": name, "auth": {"type": "static_bearer",
                                                           "mcp_server_url": url,
                                                           "token": token}})
                    self._vaults[mcp] = vault["id"]
                vault_ids = [self._vaults[mcp]]
        turn_seconds = []
        started = time.monotonic()
        session = self._call("POST", "/agents/sessions", {
            "agent_id": agent_id, "environment": {"type": "none"}, "input": case.input[0],
            **({"vault_ids": vault_ids} if vault_ids else {})})
        try:
            turns = self._until_turns_end(session["id"], 1)
            turn_seconds.append(round(time.monotonic() - started, 2))
            for n, message in enumerate(case.input[1:], start=2):
                started = time.monotonic()
                self._call("POST", f"/agents/sessions/{session['id']}/events", {"events": [{
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "type": "message",
                               "content": [{"type": "input_text", "text": message}]}]}]})
                turns = self._until_turns_end(session["id"], n)
                turn_seconds.append(round(time.monotonic() - started, 2))
            failed = [t for t in turns if t.get("status") == "failed"]
            if failed:
                error = failed[-1].get("error") or {}
                raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
            items = self._call("GET", f"/agents/sessions/{session['id']}/items"
                                      f"?order=desc&limit=100").get("data") or []
            calls = [{"name": i.get("name"), "input": i.get("arguments"),
                      "is_error": i.get("status") == "failed" or bool(i.get("error"))}
                     for i in reversed(items) if i.get("type") == "mcp_call"]
            last_turn = turns[-1]["id"]
            answer = [p.get("text", "") for item in reversed(items)
                      if item.get("type") == "message" and item.get("role") == "assistant"
                      and item.get("turn_id") == last_turn
                      for p in item.get("content") or [] if p.get("type") == "output_text"]
            used = Usage()
            for t in turns:
                u = t.get("usage") or {}
                cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
                used = used + Usage(input_tokens=(u.get("input_tokens") or 0) - cached,
                                    output_tokens=u.get("output_tokens") or 0,
                                    cache_read_tokens=cached)
            stop = "end_turn" if turns[-1].get("status") == "completed" else turns[-1].get("status")
        finally:
            try:
                self._call("DELETE", f"/agents/sessions/{session['id']}")
            except RuntimeError:
                pass
        return _outcome(case, model, "".join(answer), stop, used, turn_seconds,
                        calls=calls)

    def _until_turns_end(self, session_id: str, count: int) -> list[dict]:
        deadline = time.monotonic() + TURN_WAIT_S
        while True:
            turns = self._call("GET", f"/agents/sessions/{session_id}/turns"
                                      f"?order=asc").get("data") or []
            if len(turns) >= count and all(
                    t.get("status") in ("completed", "failed", "cancelled", "waiting")
                    for t in turns[:count]):
                return turns[:count]
            if time.monotonic() > deadline:
                raise TimeoutError(f"turn {count} did not end in {TURN_WAIT_S}s")
            time.sleep(0.25)

    def close(self) -> None:
        for agent_id in self._agents.values():
            try:
                self._call("DELETE", f"/agents/{agent_id}")
            except RuntimeError:
                pass
        for vault_id in self._vaults.values():
            try:
                self._call("DELETE", f"/vaults/{vault_id}")
            except RuntimeError:
                pass


def _provider_key(model: str | None) -> str | None:
    return harness.provider_key_for(model)


def _outcome(case: Case, model: str, answer: str, stop: str, used,
             turn_seconds: list[float], *, model_seconds: float | None = None,
             calls: list[dict] | None = None) -> dict:
    cost = _priced(model, used)
    return {"output": answer, "cost": cost or 0.0,
            "tokens": used.input_tokens + used.output_tokens,
            "input_tokens": used.input_tokens, "output_tokens": used.output_tokens,
            # Kept apart from input: a provider bills a reused prefix at a fraction
            # of the fresh rate, and folding it in or leaving it out both mislead.
            "cache_read_tokens": used.cache_read_tokens,
            "cache_write_tokens": used.cache_write_tokens,
            "priced": cost is not None, "stop_reason": stop,
            "turn_seconds": turn_seconds, "model_seconds": model_seconds,
            # What the agent did with its tools: each call, and whether it failed.
            "tool_calls": [{"name": c["name"], "input": c.get("input"),
                            "is_error": c.get("is_error")} for c in calls or []],
            "gates": gates(case, answer, stop) + tool_gates(case, calls or [])}


#: Kept for callers that name the platform class by its first name.
OurApi = AnthropicDialect


def parse_arm(spec: str) -> tuple[str, str]:
    platform, _, model = spec.partition(":")
    if not model:
        return "ours", platform
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r} in {spec!r}; "
                         f"one of {', '.join(PLATFORMS)}")
    return platform, model


# ── controls: is the instrument any good? ──────────────────────────────────

def oracles(case: Case) -> list[str]:
    """Every known-right answer a case lists — one phrasing proves little."""
    given = case.meta["oracle"]
    return given if isinstance(given, list) else [given]


def controls(cases: list[Case]) -> dict:
    """
    The scorers fed known-right answers (each of a case's `oracle` phrasings) and a
    known-wrong one. Free, and the only check that can say the numbers themselves
    are worthless. Every oracle must also pass every gate, or the gate is wrong;
    and a number case's answer must not be in its question, or echoing the
    question would pass.

    A case lists several phrasings where one has been seen to fool a scorer: a
    single oracle is written to pass, and so proves the scorer on the one answer
    nobody would get wrong.
    """
    import tempfile
    variants = [Case(id=f"{c.id}#{n}", input=c.input, expect=c.expect, group=c.group,
                     meta={**c.meta, "oracle": o})
                for c in cases for n, o in enumerate(oracles(c))]
    with tempfile.TemporaryDirectory() as scratch:
        out = pathlib.Path(scratch)
        oracle = Eval(variants, {"oracle": lambda c: c.meta["oracle"]}, score,
                      trials=1, out=out / "oracle.jsonl", verbose=False).run()
        noise = Eval(cases, {"noise": lambda c: NOISE}, score, trials=1,
                     out=out / "noise.jsonl", verbose=False).run()
    verdict = Report.controls(oracle, noise)
    bad_gates = {v.id: g for v in variants
                 if (g := gates(v, v.meta["oracle"], "end_turn"))}
    echoed = [c.id for c in cases if c.meta.get("score") == "number"
              and _has_number(c, "\n".join(c.input))[0]]
    missed = [r.case for r in oracle.records if not r.ok]
    passed = [r.case for r in noise.records if r.ok]
    verdict.update(gates_on_oracle=bad_gates, oracle_misses=missed, noise_passes=passed,
                   answer_in_question=echoed)
    verdict["ok"] = verdict["ok"] and not missed and not bad_gates and not echoed
    return verdict


# ── what each arm did ──────────────────────────────────────────────────────

def _quantile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def measures(records: list[Record], arm: str) -> dict:
    """Time per turn, tokens per attempt, errors — what accuracy does not show."""
    rows = [r for r in records if r.arm == arm]
    done = [r for r in rows if not r.error]
    turns = [s for r in done for s in (r.meta or {}).get("turn_seconds") or []]
    timed = [r for r in done if (r.meta or {}).get("model_seconds") is not None]
    model = [(r.meta or {})["model_seconds"] for r in timed]
    platform = [sum((r.meta or {}).get("turn_seconds") or []) - (r.meta or {})["model_seconds"]
                for r in timed]
    return {"attempts": len(rows), "errors": len(rows) - len(done),
            "turn_p50_s": _quantile(turns, 0.5), "turn_p90_s": _quantile(turns, 0.9),
            "model_p50_s": _quantile(model, 0.5),
            "platform_p50_s": _quantile(platform, 0.5),
            "input_tokens": statistics.mean([(r.meta or {}).get("input_tokens", 0)
                                             for r in done]) if done else math.nan,
            "output_tokens": statistics.mean([(r.meta or {}).get("output_tokens", 0)
                                              for r in done]) if done else math.nan,
            "cached_tokens": statistics.mean([(r.meta or {}).get("cache_read_tokens", 0)
                                              for r in done]) if done else math.nan,
            # All the prompt the model read, fresh or from the cache: the size of
            # what was sent, which is what a harness adds to. Billing tells them apart.
            "prompt_tokens": statistics.mean([(r.meta or {}).get("input_tokens", 0)
                                              + (r.meta or {}).get("cache_read_tokens", 0)
                                              + (r.meta or {}).get("cache_write_tokens", 0)
                                              for r in done]) if done else math.nan}


def _share(v: float) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.0%}"


def _secs(v: float) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.1f}s"


def measures_table(records: list[Record], arms: list[str]) -> str:
    head = (f"{'arm':<40}{'turn p50':>10}{'p90':>8}{'model':>8}{'platform':>10}"
            f"{'in tok':>9}{'cached':>9}{'out tok':>9}{'errors':>8}")
    lines = [head, "─" * len(head)]
    for arm in arms:
        m = measures(records, arm)
        lines.append(f"{arm:<40}{m['turn_p50_s']:>9.1f}s{m['turn_p90_s']:>7.1f}s"
                     f"{_secs(m['model_p50_s']):>8}{_secs(m['platform_p50_s']):>10}"
                     f"{m['input_tokens']:>9.0f}{m['cached_tokens']:>9.0f}"
                     f"{m['output_tokens']:>9.0f}"
                     f"{m['errors']:>5}/{m['attempts']:<3}")
    return "\n".join(lines)


def errors_seen(records: list[Record], arm: str, limit: int = 3) -> list[str]:
    seen: dict = {}
    for r in records:
        if r.arm == arm and r.error:
            seen[r.error[:160]] = seen.get(r.error[:160], 0) + 1
    return [f"{n}× {e}" for e, n in list(seen.items())[:limit]]


# ── the decision ───────────────────────────────────────────────────────────

def decide(records: list[Record], baseline: str, candidate: str, *,
           delta: float = 0.05, mode: str | None = None) -> dict:
    """
    Candidate against baseline: gates, then the accuracy test the comparison calls
    for — equivalence when only the platform differs, non-inferiority otherwise —
    with cost per success, time and tokens beside it, never weighed into it.
    """
    if mode is None:
        (bp, bm), (cp, cm) = parse_arm(baseline), parse_arm(candidate)
        mode = "equivalence" if bm == cm and bp != cp else "non-inferiority"
    report = Report(records)
    rejected = report.reject()
    per_case: dict = defaultdict(lambda: defaultdict(list))
    broken: dict = defaultdict(list)
    unpriced: set = set()
    for r in records:
        if r.error:
            continue
        per_case[r.arm][r.case].append(1.0 if r.ok else 0.0)
        for g in (r.meta or {}).get("gates") or []:
            broken[r.arm].append(f"{r.case} t{r.trial}: {g}")
        if (r.meta or {}).get("priced") is False:
            unpriced.add(r.arm)

    common = sorted(set(per_case[baseline]) & set(per_case[candidate]))
    diffs = [statistics.mean(per_case[candidate][c]) - statistics.mean(per_case[baseline][c])
             for c in common]
    diff = statistics.mean(diffs) if diffs else 0.0
    se = statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else math.inf
    lower, upper = diff - Z_95_ONE_SIDED * se, diff + Z_95_ONE_SIDED * se

    def per_success(arm: str):
        if arm in unpriced:
            return None
        wins = sum(1 for r in records if r.arm == arm and r.ok)
        spent = sum(r.cost for r in records if r.arm == arm)
        return spent / wins if wins else math.inf

    mb, mc = measures(records, baseline), measures(records, candidate)

    def ratio(key: str):
        return mc[key] / mb[key] if mb[key] and not math.isnan(mb[key]) else math.nan

    # Same model on two platforms: an invariant the model breaks on both sides is
    # the model's, not the platform's. What counts against the candidate is only
    # what it broke on a case where the baseline never broke that invariant.
    if mode == "equivalence":
        def kinds(arm):
            return {(g.split(" t", 1)[0], g.split(": ", 1)[1]) for g in broken[arm]}
        only = kinds(candidate) - kinds(baseline)
        blamed = [g for g in broken[candidate]
                  if (g.split(" t", 1)[0], g.split(": ", 1)[1]) in only]
    else:
        blamed = broken[candidate]

    if baseline in rejected or candidate in rejected:
        verdict = "INVALID — " + "; ".join(f"{a}: {w}" for a, w in rejected.items())
    elif blamed and mode == "equivalence":
        verdict = (f"STOP — {candidate} broke {len(blamed)} invariant(s) the "
                   f"baseline did not, on the same model")
    elif broken[candidate] and mode != "equivalence":
        verdict = (f"STOP — {candidate} broke {len(broken[candidate])} invariant(s)"
                   + (f"; the baseline broke {len(broken[baseline])}"
                      if broken[baseline] else ""))
    elif len(common) < MIN_CASES:
        verdict = (f"INCONCLUSIVE — {len(common)} paired cases; at least {MIN_CASES} "
                   "are needed to test any margin worth having")
    elif mode == "equivalence":
        verdict = (f"EQUIVALENT within ±{delta}" if lower > -delta and upper < delta
                   else f"NOT SHOWN EQUIVALENT within ±{delta}")
    elif lower > -delta:
        verdict = f"NON-INFERIOR within δ={delta}"
    else:
        verdict = f"NOT SHOWN NON-INFERIOR within δ={delta}"
    return {"verdict": verdict, "mode": mode, "baseline": baseline,
            "candidate": candidate, "paired_cases": len(common), "diff": diff,
            "lower_95": lower, "upper_95": upper, "delta": delta,
            "paired_test": report.paired(candidate, baseline),
            "gates": {a: broken[a] for a in (baseline, candidate)},
            "blamed": blamed,
            "cost_per_success": {a: per_success(a) for a in (baseline, candidate)},
            "time_ratio": ratio("turn_p50_s"),
            "input_token_ratio": ratio("prompt_tokens"),
            "cached_share": {a: (m["cached_tokens"] / m["prompt_tokens"]
                                 if m["prompt_tokens"] else math.nan)
                             for a, m in ((baseline, mb), (candidate, mc))},
            "output_token_ratio": ratio("output_tokens"),
            "unpriced": sorted(unpriced)}


def describe(decision: dict) -> str:
    b, c = decision["baseline"], decision["candidate"]
    cps = decision["cost_per_success"]

    def money(v):
        return "unpriced" if v is None else ("no successes" if v == math.inf
                                             else f"${v:.5f}")

    def times(v):
        return "—" if v is None or math.isnan(v) else f"{v:.2f}×"
    bound = (f"90% interval [{decision['lower_95']:+.3f}, {decision['upper_95']:+.3f}]"
             if decision["mode"] == "equivalence"
             else f"95% one-sided lower bound {decision['lower_95']:+.3f}")
    lines = [f"{c} against {b}  ({decision['mode']})",
             f"  accuracy difference  {decision['diff']:+.3f}  ({bound}, "
             f"{decision['paired_cases']} paired cases)",
             f"  paired test          {decision['paired_test']['only_a']} only {c}, "
             f"{decision['paired_test']['only_b']} only {b}, "
             f"p={decision['paired_test']['p']:.3f}"
             + ("" if decision["paired_test"]["enough"] else "  (too few to test)"),
             f"  turn time            {times(decision['time_ratio'])} the baseline's (median)",
             f"  tokens               prompt {times(decision['input_token_ratio'])}, "
             f"output {times(decision['output_token_ratio'])} the baseline's; "
             f"from cache: {c} {_share(decision['cached_share'][c])}, "
             f"{b} {_share(decision['cached_share'][b])}",
             f"  cost per success     {c} {money(cps[c])}   {b} {money(cps[b])}"]
    shown = ({c: decision["blamed"]} if decision["mode"] == "equivalence"
             else {a: decision["gates"][a] for a in (b, c)})
    for arm, gs in shown.items():
        for g in gs[:10]:
            lines.append(f"  gate  {arm}: {g}")
    if decision["mode"] == "equivalence":
        lines.append(f"  invariants           {c} {len(decision['gates'][c])}, "
                     f"{b} {len(decision['gates'][b])}; "
                     f"{len(decision['blamed'])} broken only by {c}")
    lines.append(f"  verdict              {decision['verdict']}")
    return "\n".join(lines)


def differences(records: list[Record], baseline: str, candidate: str,
                limit: int = 15) -> str:
    """
    Where the two arms did not do the same thing, side by side: each case one arm
    passed more often than the other, with an answer from each. When only the
    platform differs, these are the places to read — the model is the same, so a
    difference is the engine, the harness around the model, or chance.
    """
    by: dict = defaultdict(lambda: defaultdict(list))
    for r in records:
        if not r.error:
            by[r.case][r.arm].append(r)
    lines = []
    for case in sorted(by):
        a, b = by[case].get(baseline) or [], by[case].get(candidate) or []
        if not a or not b:
            continue
        ra, rb = sum(r.ok for r in a) / len(a), sum(r.ok for r in b) / len(b)
        if ra == rb:
            continue

        def sample(rows):
            pick = next((r for r in rows if not r.ok), rows[0])
            return " ".join(str(pick.output or "").split())[:160]
        lines += [f"{case}: {baseline} {sum(r.ok for r in a)}/{len(a)}, "
                  f"{candidate} {sum(r.ok for r in b)}/{len(b)}",
                  f"    {baseline}: {sample(a)}",
                  f"    {candidate}: {sample(b)}"]
    if not lines:
        return f"no case went differently between {baseline} and {candidate}"
    shown = lines[:limit * 3]
    more = len(lines) // 3 - limit
    return ("cases that went differently\n" + "\n".join(shown)
            + (f"\n  … and {more} more" if more > 0 else ""))


def report_text(records: list[Record], arms: list[str], delta: float,
                on: datetime.date | None = None) -> str:
    records, notes = R.reprice(records, lambda arm: parse_arm(arm)[1],
                               on or datetime.date.today())
    report = Report(records)
    report.reject()
    parts = [report.table(), report.by_group(), measures_table(records, arms)]
    for spec in arms:
        seen = errors_seen(records, spec)
        if seen:
            parts.append(f"errors in {spec}:\n" + "\n".join(f"  {e}" for e in seen))
    for c in arms[1:]:
        parts.append(differences(records, arms[0], c))
        parts.append(describe(decide(records, arms[0], c, delta=delta)))
    if notes:
        parts.append(R.pricing_note(notes))
    return "\n\n".join(parts)


def read_ledger(path: pathlib.Path) -> list[Record]:
    from yait_aichain.eval import read
    return read(path)


def rescore(records: list[Record], cases: list[Case]) -> list[Record]:
    """
    The same answers, judged again by the current scorers and gates. A scorer fixed
    after a run is a fix to the instrument, and must not cost another run.
    """
    by_id = {c.id: c for c in cases}
    out = []
    for r in records:
        case = by_id.get(r.case)
        if case is None or r.error:
            out.append(r)
            continue
        ok, value, _ = normalise(score(case, r.output))
        meta = {**(r.meta or {}),
                "gates": gates(case, r.output or "", (r.meta or {}).get("stop_reason", ""))}
        out.append(Record(**{**r.as_dict(), "ok": ok, "score": value, "meta": meta}))
    return out


# ── the command ────────────────────────────────────────────────────────────

RATE_LIMIT_WAITS = (10, 20, 40, 80, 160)


def paced(attempt, limit: int | None):
    """
    An arm's attempts, within its account's limits.

    A provider account may allow only a request or two at a time; past that it
    answers 429. That is the account, not the model, so it must not be scored as
    the model: at most `limit` attempts of the arm run at once, and one refused for
    its rate is tried again after a growing pause, as any client would. The turn
    times are the successful try's own, so waiting is not read as slowness; how
    many tries it took is kept beside them.
    """
    gate = threading.Semaphore(limit) if limit else None

    def run(case):
        if gate:
            gate.acquire()
        try:
            for tries, wait in enumerate((*RATE_LIMIT_WAITS, None)):
                try:
                    out = attempt(case)
                    out["rate_limit_retries"] = tries
                    return out
                except RuntimeError as exc:
                    if wait is None or "rate_limit" not in str(exc):
                        raise
                    time.sleep(wait)
        finally:
            if gate:
                gate.release()
    return run


def platforms_for(arms: list[str], base: str | None, key: str) -> dict:
    made: dict = {}
    for spec in arms:
        platform, _ = parse_arm(spec)
        if platform in made:
            continue
        if platform == "ours":
            if not base:
                raise SystemExit("an ours: arm needs --base-url or --local")
            made[platform] = AnthropicDialect(base, key)
        elif platform == "anthropic":
            made[platform] = AnthropicDialect(ORIGINAL["anthropic"],
                                              _need("ANTHROPIC_API_KEY"), original=True)
        else:
            made[platform] = OpenAIOriginal(_need("OPENAI_API_KEY"))
    return made


KEYS_FROM_FILES = harness.KEYS_FROM_FILES
DEFAULT_ENV_FILES = harness.DEFAULT_ENV_FILES
load_keys = harness.load_keys


def _need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is needed for an arm on the original platform")
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", required=True,
                    help="platform:model, comma-separated; the first is the baseline")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases" / "agents.jsonl"))
    ap.add_argument("--base-url")
    ap.add_argument("--key")
    ap.add_argument("--local", action="store_true",
                    help="our API on a local server, aichain backend, keys from the env")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--delta", type=float, default=0.05)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--name", default="quality",
                    help="the run's name; its results go to evals/results/<date>-<name>/")
    ap.add_argument("--run", action="store_true", help="the whole run, after the smoke")
    ap.add_argument("--force", action="store_true",
                    help="run the whole set even if the smoke failed for some arm")
    ap.add_argument("--out", help="also write the report here, as markdown")
    ap.add_argument("--env-file", action="append",
                    help="read ANTHROPIC_API_KEY, OPENAI_API_KEY and YAIT_CLIENT_KEY "
                         "from here (repeatable; default: .env here and in ../aichain)")
    ap.add_argument("--limit", action="append", default=[], metavar="MODEL=N",
                    help="at most N attempts of a model at once — an account's limit "
                         "(repeatable); a 429 is retried with a growing pause either way")
    ap.add_argument("--rescore", metavar="RUN",
                    help="no model calls: a results directory (or a ledger) scored "
                         "again with the current scorers, gates and prices")
    a = ap.parse_args()

    for name in load_keys(a.env_file or DEFAULT_ENV_FILES):
        print(f"key     {name}")
    a.key = a.key or os.environ.get("YAIT_CLIENT_KEY", "")
    arms = [s.strip() for s in a.arms.split(",") if s.strip()]
    if len(arms) < 2:
        print("--arms needs a baseline and at least one candidate", file=sys.stderr)
        return 2
    try:
        for spec in arms:
            parse_arm(spec)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if a.base_url and a.local:
        print("give --base-url or --local, not both", file=sys.stderr)
        return 2
    if any(parse_arm(s)[0] == "ours" for s in arms) and not (
            a.base_url or a.local or a.rescore):
        print("an ours: arm needs --base-url or --local", file=sys.stderr)
        return 2
    cases = load_cases(pathlib.Path(a.cases))

    print(f"controls — the scorers on known-right and known-wrong answers ({len(cases)} cases)")
    checked = controls(cases)
    print(f"  oracle {checked['oracle']:.3f}   noise {checked['noise']:.3f}   "
          + ("ok" if checked["ok"] else f"BROKEN: {checked['why']} "
             f"{checked['oracle_misses']} {checked['noise_passes']} "
             f"{checked['gates_on_oracle']}"))
    if not checked["ok"]:
        return 1

    if a.rescore:
        target = pathlib.Path(a.rescore)
        ledger = target / "ledger.jsonl" if target.is_dir() else target
        stamp = R.read_passport(target) if target.is_dir() else None
        on = (datetime.date.fromisoformat(stamp["started_at"][:10]) if stamp
              else datetime.date.today())
        records = rescore(read_ledger(ledger), cases)
        text = report_text(records, arms, a.delta, on)
        print("\n" + text)
        if a.out:
            pathlib.Path(a.out).write_text(f"# rescored {a.rescore}\n\n```\n{text}\n```\n")
        return 0

    today = datetime.date.today()
    here = R.run_dir(a.name, today)
    hosts = {"ours": a.base_url or "local", **ORIGINAL}

    def work(base: str | None, key: str) -> int:
        made = platforms_for(arms, base, key)
        try:
            limits = {k: int(v) for k, _, v in (x.partition("=") for x in a.limit)}
            runners = {spec: paced(lambda case, p=made[parse_arm(spec)[0]],
                                   m=parse_arm(spec)[1]: p.attempt(m, case),
                                   limits.get(parse_arm(spec)[1]))
                       for spec in arms}
            # The passport first: a run that dies half-way still says what it was.
            stamp = R.read_passport(here) or R.passport(
                name=here.name, cases_path=pathlib.Path(a.cases), cases=cases,
                arms=[{"arm": spec, "platform": parse_arm(spec)[0],
                       "model": parse_arm(spec)[1], "host": hosts[parse_arm(spec)[0]]}
                      for spec in arms],
                trials=a.trials, delta=a.delta, concurrency=a.concurrency,
                prices=R.load_prices(), on=today)
            stamp["limits"] = {k: int(v) for k, _, v in (x.partition("=") for x in a.limit)}
            R.write_json(here / "passport.json", stamp)
            ev = Eval(cases, runners, score, trials=a.trials,
                      out=here / "ledger.jsonl", name=here.name)
            print("\nsmoke — two cases, every arm, once")
            smoke = ev.smoke()
            for spec in arms:
                for e in errors_seen(smoke.records, spec):
                    print(f"  {spec}: {e}")
            per_attempt = {s: smoke.spend(s)["cost"] / max(smoke.spend(s)["trials"], 1)
                           for s in arms}
            broken = [s for s in arms if any(r.error for r in smoke.records if r.arm == s)]
            if broken and a.run and not a.force:
                # A smoke that failed is the wiring, not the model: running the whole
                # set would spend the money and fill the ledger with the same error.
                print(f"smoke failed for {', '.join(broken)} — not running the whole set "
                      f"(--force runs it anyway)")
                return 1
            estimate = sum(per_attempt.values()) * len(cases) * a.trials
            print(f"estimated full run: {len(cases)} cases × {a.trials} trials × "
                  f"{len(arms)} arms ≈ ${estimate:.2f} at list prices"
                  + (" — some arms unpriced, so more" if any(
                      (r.meta or {}).get("priced") is False for r in smoke.records)
                     else ""))
            if not a.run:
                print("smoke only. Add --run for the whole run.")
                return 0
            report = ev.run(concurrency=a.concurrency)
            text = report_text(report.records, arms, a.delta, today)
            print("\n" + text)
            markdown = f"# {here.name}\n\n```\n{text}\n```\n"
            (here / "report.md").write_text(markdown)
            stamp.update(finished_at=datetime.datetime.now(datetime.timezone.utc)
                         .isoformat(timespec="seconds"),
                         attempts=len(report.records),
                         errors=sum(1 for r in report.records if r.error))
            R.write_json(here / "passport.json", stamp)
            if a.out:
                pathlib.Path(a.out).write_text(markdown)
            print(f"\nresults: {here.relative_to(ROOT)}/")
            return 0
        finally:
            for platform in made.values():
                platform.close()

    if a.local:
        with harness.serve_locally(8134, "/anthropic/v1/agents", backend="aichain") as base:
            return work(base, harness.KEY)
    return work(a.base_url, a.key or "")


if __name__ == "__main__":
    raise SystemExit(main())
