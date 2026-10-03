#!/usr/bin/env python3
"""
The model backends, without a network.

The library's Agent is real here; only its one call to a provider is replaced, by a
stand-in that reports the usage a provider would. What is tested is our side: that
a step's usage reaches the Reply split the way the dialect reports it, and that the
failures are named.

    python3 tests/test_model.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import model as M  # noqa: E402


class Results:
    def __init__(self) -> None:
        self.passed, self.failures = 0, []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    def report(self) -> int:
        print(f"\nmodel: {self.passed}/{self.passed + len(self.failures)} checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


def run() -> int:
    res = Results()
    from yait_aichain.agent import _agent as library
    from yait_aichain.models._usage import Usage

    def answered(self, skill):
        # What a provider call leaves behind: the reply, and its usage on the Skill.
        skill.last_usage = Usage(input_tokens=120, output_tokens=30, total_tokens=150,
                                 cost=0.0021, cache_read_tokens=100,
                                 cache_write_tokens=7)
        return "hello"

    original = library.Agent._reply
    library.Agent._reply = answered
    try:
        backend = M.AichainModel()
        state: dict = {}
        with M.using_key("test-not-a-key"):
            reply = backend.step([{"role": "user", "parts": ["hi"]}], state,
                                 model_name="claude-sonnet-5")
            second = backend.step([{"role": "user", "parts": ["hi"]}], state,
                                  model_name="claude-sonnet-5")
    finally:
        library.Agent._reply = original

    res.check("the answer comes through", reply.text == "hello", f"{reply}")
    built = backend._agents["claude-sonnet-5"]
    res.check("prompt caching is on for the model",
              getattr(built.model, "cache_control", None) is True,
              f"{getattr(built.model, 'cache_control', None)!r}")
    res.check("Claude 5 thinks before answering, deciding for itself how long",
              built.model.extra.get("thinking") == {"type": "adaptive"}
              and getattr(built.model, "reasoning", None) is None and reply.thought,
              f"extra={built.model.extra}, thought={reply.thought}")
    older = M._options("claude-haiku-4-5-20251001",
                       M.thinking_for("claude-haiku-4-5-20251001"), {})
    res.check("an older Claude thinks within a budget",
              older.get("reasoning") == "medium" and "extra" not in older, f"{older}")
    res.check("a named server's model is left to that server",
              M._options("openrouter/anthropic/claude-opus-5-5",
                         M.thinking_for("openrouter/anthropic/claude-opus-5-5"),
                         {"provider": {"quantizations": ["fp8"]}})
              == {"cache_control": True, "max_tokens": M.DEFAULT_MAX_TOKENS,
                  "extra": {"provider": {"quantizations": ["fp8"]}}})
    res.check("input and output tokens are reported apart",
              reply.usage.get("input_tokens") == 120
              and reply.usage.get("output_tokens") == 30, f"{reply.usage}")
    res.check("and the cache reads and writes",
              reply.usage.get("cache_read_input_tokens") == 100
              and reply.usage.get("cache_creation_input_tokens") == 7, f"{reply.usage}")
    res.check("the cost is this step's, not the running total",
              second.usage.get("cost_usd") == 0.0021, f"{second.usage}")

    res.check("an answer may take the platform's ceiling, not the library's default",
              getattr(built.model, "max_tokens", None) == M.DEFAULT_MAX_TOKENS,
              f"{getattr(built.model, 'max_tokens', None)!r}")
    os.environ["YAIT_MAX_TOKENS"] = "50000"
    try:
        res.check("and the installation may set it",
                  M._options("deepseek-v4-pro", None, {})["max_tokens"] == 50000)
    finally:
        del os.environ["YAIT_MAX_TOKENS"]

    # An answer that spent the whole ceiling and said nothing is a failure, named —
    # not an empty end_turn.
    def spent(self, skill):
        skill.last_usage = Usage(input_tokens=900, output_tokens=M.DEFAULT_MAX_TOKENS,
                                 total_tokens=900 + M.DEFAULT_MAX_TOKENS)
        return ""
    library.Agent._reply = spent
    try:
        with M.using_key("test-not-a-key"):
            M.AichainModel().step([{"role": "user", "parts": ["hi"]}], {},
                                  model_name="deepseek-v4-pro")
        res.check("an answer cut off at the ceiling fails", False, "it returned")
    except M.AnswerCutOff as exc:
        res.check("an answer cut off at the ceiling fails, as a server error, saying why",
                  M.error_category(exc) == "server_error" and "YAIT_MAX_TOKENS" in str(exc),
                  str(exc))
    finally:
        library.Agent._reply = original

    # A step's whole prompt is cached — its last block, whatever it is — because
    # the next step's prompt begins with it.
    import warnings
    from yait_aichain import Model
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _, body = Model("claude-haiku-4-5-20251001", api_key="x",
                        options={"cache_control": True}).to_request([
            {"role": "system", "parts": [{"type": "text", "text": "sys"}]},
            {"role": "user", "parts": [{"type": "text", "text": "find it"}]},
            {"role": "assistant", "tool_calls": [{"id": "t1", "name": "search",
                                                  "arguments": {"q": "x"}}]},
            {"role": "tool", "call_id": "t1",
             "parts": [{"type": "text", "text": "result"}]}], {})
    marks = [(i, b.get("type")) for i, m in enumerate(body["messages"])
             for b in (m["content"] if isinstance(m["content"], list) else [])
             if isinstance(b, dict) and "cache_control" in b]
    res.check("the cache mark is on the step's last block, a tool result included",
              marks == [(len(body["messages"]) - 1, "tool_result")], f"{marks}")

    from yait_aichain.skills import _skill
    used = _skill.extract_usage({"usage": {"input_tokens": 1000, "output_tokens": 50,
                                           "input_tokens_details": {"cached_tokens": 800}}})
    res.check("a Responses API cached prefix is read, and taken out of the fresh input",
              used.cache_read_tokens == 800 and used.input_tokens == 200, f"{used}")

    # The deployment never pays for tokens: with no key from the caller, nothing
    # else is tried — not the process environment, not a secret.
    os.environ["OPENAI_API_KEY"] = "a-key-the-deployment-must-not-use"
    try:
        M.AichainModel().step([{"role": "user", "parts": ["hi"]}], {},
                              model_name="gpt-6-luna")
        res.check("with no key from the caller, the model is not called", False, "it ran")
    except M.MissingKey as exc:
        res.check("with no key from the caller, the model is not called — "
                  "and the error says how to send one",
                  "X-Yait-Key" in str(exc) and "x-api-key" in str(exc), str(exc))
    finally:
        del os.environ["OPENAI_API_KEY"]
    with M.using_key("sk-caller"):
        brought = M._key_for("OPENAI_API_KEY"), M._key_for("ANTHROPIC_API_KEY")
    res.check("the caller's key is the key, for whatever provider the model is",
              brought == ("sk-caller", "sk-caller"), f"{brought}")
    res.check("and it is gone after the run", M._key_for("OPENAI_API_KEY") == "")
    res.check("a price of zero is a price; none is not",
              M.AichainModel().priced("claude-sonnet-5")
              and not M.AichainModel().priced("private/no-such-model"))
    # ── named model servers ──
    import endpoints as EP
    servers = EP.parse("mygpu=https://gpu.example.com/v1/\n# a comment\n\n"
                       "lab=https://llm.lab.example.org/v1")
    res.check("named servers are read, over the built-in ones",
              {k: v["url"] for k, v in servers.items()}
              == {"openrouter": "https://openrouter.ai/api/v1",
                  "mygpu": "https://gpu.example.com/v1",
                  "lab": "https://llm.lab.example.org/v1"}, f"{servers}")
    res.check("a model is sent to its server by prefix, with the rest as its name",
              EP.resolve("mygpu/llama-3.3-70b", servers)
              == ("https://gpu.example.com/v1", "llama-3.3-70b", {})
              and EP.resolve("openrouter/z-ai/glm-5.3-flash", servers)
              == ("https://openrouter.ai/api/v1", "z-ai/glm-5.3-flash", {}))
    pinned = EP.parse('openrouter-fp8=https://openrouter.ai/api/v1 '
                      '{"provider": {"quantizations": ["fp8"]}}')
    res.check("a server may carry request fields — OpenRouter pinned to FP8 hosts",
              EP.resolve("openrouter-fp8/qwen/qwen3.8-27b", pinned)
              == ("https://openrouter.ai/api/v1", "qwen/qwen3.8-27b",
                  {"provider": {"quantizations": ["fp8"]}}), f"{pinned}")
    res.check("a provider's own prefix is left to the library",
              EP.resolve("openai/gpt-6-luna", servers) is None
              and EP.resolve("claude-haiku-4-5-20251001", servers) is None)
    for name, text, says in (
            ("plain http", "mygpu=http://gpu.example.com/v1", "https"),
            ("a local address in the cloud", "mygpu=https://localhost:8000/v1", "local"),
            ("a provider's name", "openai=https://example.com/v1", "provider"),
            ("a name twice", "a=https://x.example/v1\na=https://y.example/v1", "twice"),
            ("a line that is not name=url", "just a url", "name="),
            ("a name with capitals", "MyGPU=https://x.example/v1", "lowercase"),
            ("fields that are not JSON", "a=https://x.example/v1 {provider}", "JSON"),
            ("fields that would change the request itself",
             'a=https://x.example/v1 {"model": "other"}', "own fields")):
        try:
            EP.parse(text)
            res.check(f"{name} is refused", False, "it was accepted")
        except EP.Invalid as exc:
            res.check(f"{name} is refused, with the line named",
                      says in str(exc) and "line " in str(exc),
                      str(exc))
    res.check("a local server is allowed for a local run",
              EP.parse("ollama=http://localhost:11434/v1", allow_local=True)["ollama"]["url"]
              == "http://localhost:11434/v1")
    res.check("a line naming openrouter replaces the built-in one",
              EP.parse("openrouter=https://proxy.example/v1")["openrouter"]["url"]
              == "https://proxy.example/v1")

    library.Agent._reply = answered
    try:
        os.environ["YAIT_ENDPOINTS"] = json.dumps({"mygpu": {
            "url": "https://gpu.example.com/v1",
            "extra": {"provider": {"quantizations": ["fp8"]}}}})
        routed = M.AichainModel()
        with M.using_key("sk-caller"):
            got = routed.step([{"role": "user", "parts": ["hi"]}], {},
                              model_name="mygpu/llama-3.3-70b")
        built = routed._agents["mygpu/llama-3.3-70b"].model
    finally:
        library.Agent._reply = original
        del os.environ["YAIT_ENDPOINTS"]
    res.check("a named server's model is the library's private one, at that address",
              got.text == "hello" and built.name == "llama-3.3-70b"
              and "gpu.example.com" in str(getattr(built, "client_options", "")
                                          or vars(built.client)),
              f"{built.name} {getattr(built, 'client_options', None)}")
    res.check("and its request fields travel with every request",
              built.extra == {"provider": {"quantizations": ["fp8"]}}, f"{built.extra}")
    return res.report()


if __name__ == "__main__":
    raise SystemExit(run())
