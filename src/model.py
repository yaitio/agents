"""
The model, behind the smallest interface the step loop needs.

`step(messages, state) -> Reply` mirrors `aichain`'s `Agent.step()`: ask for the
next decision and return it without executing it — text, or tool calls. Slice four
puts the library behind this interface; the worker does not change when it does.

**The echo backend is not a mock hidden in production.** It is selected by
`YAIT_MODEL_BACKEND`, reported in every answer's event, and it exists so the step
loop — lease, fold, commit, handoff — can be built and verified before a provider key
is in the account. A deployment running it says so.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
from dataclasses import dataclass, field

import endpoints


class NotConfigured(Exception):
    """No backend is configured, or the configured one is not available yet."""


class AnswerCutOff(RuntimeError):
    """The model spent its whole answer ceiling and said nothing."""


class MissingKey(NotConfigured):
    """The deployment holds no key for the provider this model needs."""


@dataclass
class Reply:
    """One decision: a text answer, or calls to make. Never both empty."""

    text: str | None = None
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    backend: str = ""
    #: The model was asked to think before this reply.
    thought: bool = False


#: How much a provider's model deliberates before answering, where the original
#: agent API has it think by default. Managed Agents runs Claude with extended
#: thinking; without it the same model answered the hard sets worse through us
#: than through the original — haiku 0.33 against 0.58 (evals, 2026-09-30). The
#: library turns a level into Anthropic's thinking budget: medium is 10,000
#: tokens, above the most the original was seen to spend on one answer (~5,000).
THINKING = {"anthropic": "medium"}

#: Claude from its fifth generation refuses a thinking budget ("thinking.type.enabled
#: is not supported for this model") and decides for itself how long to think.
#: It is asked for that, with the provider's default effort.
_ADAPTIVE = re.compile(r"^claude-[a-z]+-(\d+)")

#: The most one answer may take, thinking included. Neither original agent API lets
#: a caller set it — they expose how hard the model works (effort), and the room to
#: answer is the platform's — so it is the installation's, in YAIT_MAX_TOKENS. The
#: library's defaults are for one call, not an agent's step: 4,096 for DeepSeek
#: left a third of its hard-set answers empty, spent on thinking (evals,
#: 2026-09-30). Every model we run took 32,768 when asked.
DEFAULT_MAX_TOKENS = 32768


def answer_ceiling() -> int:
    raw = os.environ.get("YAIT_MAX_TOKENS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_TOKENS


def adaptive(model_name: str) -> bool:
    match = _ADAPTIVE.match(model_name)
    return bool(match) and int(match.group(1)) >= 5


def thinking_for(model_name: str) -> str | None:
    """
    The level a model thinks at, by the provider the library routes it to. A named
    server's model is left alone: what it takes is that server's business.
    """
    if endpoints.resolve(model_name) is not None:
        return None
    try:
        from yait_aichain.models._base import _resolve_provider
        provider = _resolve_provider(model_name)
    except Exception:  # noqa: BLE001 — without the library, or a name it does not know
        provider = "anthropic" if model_name.startswith("claude") else ""
    return THINKING.get(provider)


class EchoModel:
    """
    Answers with the user's own words. Deterministic, free, and instant.

    It reads the conversation the fold produced, so it exercises the fold: the
    answer quotes the last user turn in the history the worker built, not the text
    of the request that triggered it. If the fold dropped a message, the echo would
    show it.
    """

    name = "echo"

    def priced(self, model_name: str) -> bool:
        """Free, and a budget measures free exactly."""
        return True

    def step(self, messages: list[dict], state: dict, *,
             model_name: str = "", tools: list | None = None) -> Reply:
        state["steps"] = state.get("steps", 0) + 1
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            # After a tool, it reports what the tool said: the loop closes.
            text = " ".join(p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in last.get("parts") or [])
            return Reply(text=f"echo: the tool said: {text[:300]}",
                         usage={"input_tokens": 0, "output_tokens": 0},
                         backend=self.name, thought=bool(thinking_for(model_name)))
        asked = " ".join(p.get("text", "") if isinstance(p, dict) else str(p)
                         for p in last.get("parts") or [])
        # "use <tool> <json arguments>" calls that tool, when the agent has it.
        if tools and asked.startswith("use "):
            name, _, rest = asked[4:].partition(" ")
            if name in {t.name for t in tools}:
                return Reply(calls=[{"id": f"call_{state['steps']}", "name": name,
                                     "arguments": json.loads(rest or "{}")}],
                             usage={"input_tokens": 0, "output_tokens": 0},
                             backend=self.name, thought=bool(thinking_for(model_name)))
        said = []
        for message in reversed(messages):
            if message.get("role") != "user":
                break
            for part in message.get("parts") or []:
                said.append(part.get("text", "") if isinstance(part, dict) else str(part))
        said.reverse()
        # It thinks where the real backend would, so a trace from it has the
        # original's shape — agent.thinking before a Claude model's answer.
        return Reply(text="echo: " + " / ".join(said) if said else "echo: (nothing)",
                     usage={"input_tokens": 0, "output_tokens": 0}, backend=self.name,
                     thought=bool(thinking_for(model_name)))


# ── provider keys ──────────────────────────────────────────────────────────

#: The key the caller brought with its request, for the length of one worker run —
#: the only key a model is ever called with. The installation pays for the
#: infrastructure and never for tokens: a caller registers with any provider and
#: brings the key, and is billed by that provider directly. Used for whatever model
#: the agent names; a key that provider will not take fails the turn as an
#: authentication error, which is the caller's to fix.
_brought: contextvars.ContextVar = contextvars.ContextVar("provider_key", default=None)


@contextlib.contextmanager
def using_key(key: str | None):
    """Call models with the caller's key for the length of the block."""
    token = _brought.set(key or None)
    try:
        yield
    finally:
        _brought.reset(token)


def _key_for(env_key: str) -> str:
    return _brought.get() or ""


def _gpt6_through_responses() -> None:
    """
    aichain 3.0.0 sends only `gpt-5*` through OpenAI's Responses API. gpt-6 goes to
    Chat Completions, which refuses function tools alongside reasoning ("use
    /v1/responses") — so no gpt-6 model could call a tool. Until the library knows
    the family, it is told here; remove this when aichain does.
    """
    try:
        from yait_aichain.clients._families import _openai_compat as oc
    except ImportError:
        return
    if not any("gpt-6".startswith(p) for p in oc._RESPONSES_API_PREFIXES):
        oc._RESPONSES_API_PREFIXES = (*oc._RESPONSES_API_PREFIXES, "gpt-6")


_gpt6_through_responses()


def _cache_the_whole_step() -> None:
    """
    Anthropic's prompt cache, marked where an agent step needs it: on the last
    block of the last message.

    aichain marks the second-to-last message, which is right for a chat whose
    newest message changes every call, and marks only a text block. A step of an
    agent is neither: its log only grows, so the whole prompt of this step is
    the prefix of the next; and within a turn the newest messages are a tool call
    and its result, which carry no text block. Measured on a three-step turn
    (search, read, publish): 15,000 tokens written to the cache and never read,
    because the mark landed one step late — where the original agent API sends
    about 25 fresh tokens a step and reads the rest back.
    """
    try:
        from yait_aichain.clients._families.anthropic import AnthropicClient
    except ImportError:
        return
    if getattr(AnthropicClient.build_request, "_whole_step", False):
        return
    original = AnthropicClient.build_request

    def build_request(self, messages, output, params, tools=None):
        path, body = original(self, messages, output, params, tools=tools)
        if params.get("cache_control") is not True or not body.get("messages"):
            return path, body
        for message in body["messages"]:
            for block in message["content"] if isinstance(message["content"], list) else []:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
        last = body["messages"][-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        if last["content"]:
            last["content"][-1]["cache_control"] = {"type": "ephemeral"}
        return path, body

    build_request._whole_step = True
    AnthropicClient.build_request = build_request


_cache_the_whole_step()


def _read_responses_cache() -> None:
    """
    OpenAI's Responses API reports a reused prefix as
    `usage.input_tokens_details.cached_tokens`; aichain 3.0.0 reads only Chat
    Completions' `prompt_tokens_details`, so every gpt-6 step counted its cached
    prefix as fresh input — a cost and a budget that overstate what was paid.
    Read here, inside the input as OpenAI counts it, and taken out of it.
    """
    try:
        from yait_aichain.skills import _skill
    except ImportError:
        return
    if getattr(_skill.extract_usage, "_responses_cache", False):
        return
    original = _skill.extract_usage

    def extract_usage(response):
        used = original(response)
        u = response.get("usage") if isinstance(response, dict) else None
        details = (u or {}).get("input_tokens_details") if isinstance(u, dict) else None
        cached = (details or {}).get("cached_tokens") or 0
        if cached and not used.cache_read_tokens:
            import dataclasses
            used = dataclasses.replace(used, cache_read_tokens=cached,
                                       input_tokens=max(0, used.input_tokens - cached))
        return used

    extract_usage._responses_cache = True
    _skill.extract_usage = extract_usage


_read_responses_cache()


def declaring(agent_class):
    """
    The library's Agent offering exactly the tools it is given. Left to itself it
    adds `pool` — run one tool over a list, in parallel — which it executes in its
    own loop; here the step worker executes every call, one committed step at a
    time, so a tool only the library could run is not offered.
    """
    class Declaring(agent_class):
        def _tool_schemas(self):
            return [t.schema() for t in self.tools]
    return Declaring


def capturing(agent_class):
    """
    The library's Agent, keeping the last call's usage where the step can read it.

    `Agent.step()` folds a call's usage into two running totals — tokens and cost —
    and drops the rest: input against output, cache reads and writes. The dialect
    reports exactly those per request, and a cost measured from them is exact where
    the running total in cents is not. The Skill holding them lives only inside the
    call, so this keeps a reference as `_reply` hands it back — one override of a
    private method, the price of reading what the library already knows.
    """
    class Capturing(agent_class):
        last_usage = None

        def _reply(self, skill):
            try:
                return super()._reply(skill)
            finally:
                self.last_usage = getattr(skill, "last_usage", None)
    return Capturing


def _options(model_name: str, thinking: str | None, extra: dict) -> dict:
    """The library options for one model: caching, thinking, a server's own fields."""
    # Prompt caching on: the library marks the end of the stable prefix —
    # everything before the newest message — so each later step of a growing
    # conversation reads it back at a tenth of the price. Anthropic needs the
    # mark; OpenAI caches by itself; a provider without caching declines it with a
    # note, and the request goes as it would have.
    options: dict = {"cache_control": True, "max_tokens": answer_ceiling()}
    extra = dict(extra or {})     # a named server's own request fields — say, FP8
    if thinking and adaptive(model_name):
        extra["thinking"] = {"type": "adaptive"}
    elif thinking:
        options["reasoning"] = thinking
    if extra:
        options["extra"] = extra
    return options


class AichainModel:
    """
    Any provider `aichain` knows, behind the same `step()`.

    One library `Model` per model name, built on first use and kept for the warm
    environment. The key is a callable, resolved per request rather than at
    construction — the library's own seam for this, and what lets one warm model
    serve each run with that run's caller's key.

    The name is whatever the agent says. `aichain` resolves bare names itself —
    `claude-opus-5` is Anthropic, `gpt-5` is OpenAI — and a `provider/` prefix
    steers it explicitly, which is how `private/…` reaches a server of your own.
    """

    name = "aichain"

    def __init__(self) -> None:
        self._agents: dict = {}
        self._thinking: list = []      # the agents built to think

    def _agent_for(self, model_name: str):
        from yait_aichain.models._base import PROVIDERS, _resolve_provider
        # A named server first: `openrouter/<model>` is the library's private/
        # provider at the address the owner listed (endpoints.py). Everything else
        # is routed by the library itself.
        server = endpoints.resolve(model_name)
        if server is not None:
            url, rest, extra = server
            provider = model_name.split("/", 1)[0]
            env_key = PROVIDERS["private"]["provider"]["env_key"]
            library_name, client = f"private/{rest}", {"url": url}
        else:
            provider = _resolve_provider(model_name)
            env_key = PROVIDERS[provider]["provider"]["env_key"]
            library_name, client, extra = model_name, None, {}
        # Said here, by name, rather than left to the library's own error — which
        # is right for a library and opaque to a client, who would read it as an
        # internal failure with nothing to do about it.
        if not _key_for(env_key):
            raise MissingKey(
                f"no provider key was sent: {model_name!r} is called with the "
                f"caller's own {provider} key, sent as x-api-key or "
                f"Authorization: Bearer beside X-Yait-Key")
        agent = self._agents.get(model_name)
        thinking = thinking_for(model_name)
        if agent is None:
            from yait_aichain import Agent, Model
            model = Model(library_name, api_key=lambda _context: _key_for(env_key),
                          options=_options(model_name, thinking, extra),
                          client_options=client)
            # Instructions are not passed here: the fold already put the system
            # turn at the head of the history, in the same shape the library's own
            # opening() builds.
            agent = self._agents[model_name] = capturing(Agent)(model)
            if thinking:
                self._thinking.append(agent)
        return agent

    def priced(self, model_name: str) -> bool:
        """
        Whether the library has a list price for this model — asked before the
        call, because a budget that cannot measure a model must not let it run.
        A price of zero is a price; only a missing one is not.
        """
        from yait_aichain.models._usage import Usage, estimate_cost
        return estimate_cost(Usage(input_tokens=1, output_tokens=1), model_name) is not None

    def step(self, messages: list[dict], state: dict, *,
             model_name: str = "", tools: list | None = None) -> Reply:
        if not model_name:
            raise NotConfigured("the agent names no model")
        agent = self._agent_for(model_name)
        thought = agent in self._thinking
        if tools:
            # The session's tools belong to this call, not to the model: the same
            # model serves sessions with other tools, or none.
            from yait_aichain import Agent
            agent = capturing(declaring(Agent))(agent.model, tools=list(tools))
        # The library's counters are cumulative over the run. What this step cost is
        # the difference — recording the running total per step would bill the
        # first turn again on every later one.
        # The library's counters, for a session that has none yet. It reads them
        # after the call, so a missing one failed the turn once the provider had
        # already been paid — every first turn of every session.
        for field, value in agent.new_state().items():
            state.setdefault(field, value)
        tokens_before = state.get("tokens") or 0
        cost_before = state.get("cost") or 0.0
        agent.last_usage = None
        answer = agent.step(messages, state)
        last = agent.last_usage
        usage = {"tokens": (state.get("tokens") or 0) - tokens_before,
                 "cost_usd": round((state.get("cost") or 0.0) - cost_before, 8),
                 "input_tokens": getattr(last, "input_tokens", 0) or 0,
                 "output_tokens": getattr(last, "output_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(last, "cache_read_tokens", 0) or 0,
                 "cache_creation_input_tokens":
                     getattr(last, "cache_write_tokens", 0) or 0}
        text = answer if isinstance(answer, str) else getattr(answer, "text", None)
        if (usage["output_tokens"] >= answer_ceiling() and not (text or "").strip()
                and not getattr(answer, "calls", None)):
            # Neither dialect has a stop reason for this: an agent's turn ends,
            # needs the caller, gives up, or runs out of budget. An empty answer
            # passed off as end_turn would read as the model having nothing to say.
            raise AnswerCutOff(
                f"{model_name} spent its whole answer ceiling ({answer_ceiling()} "
                f"tokens) without answering — thinking, most likely. The ceiling "
                f"is the installation's YAIT_MAX_TOKENS.")
        if isinstance(answer, str):
            return Reply(text=answer, usage=usage, backend=f"aichain:{model_name}",
                         thought=thought)
        calls = [{"id": c.id, "name": c.name, "arguments": c.arguments or {}}
                 for c in getattr(answer, "calls", [])]
        return Reply(text=getattr(answer, "text", None), calls=calls, usage=usage,
                     backend=f"aichain:{model_name}", thought=thought)


# ── what a failure was ─────────────────────────────────────────────────────

#: The library's error classes, by name, to our failure kinds (events.FAILURES). By
#: name so the echo backend — and a deployment without the library — need not
#: import it.
_KINDS = {
    "InsufficientCreditsError": "billing",
    "RateLimitError": "rate_limited",
    "AuthenticationError": "authentication",
    "InvalidRequestError": "invalid_request",
    "NotFoundError": "not_found",
    "NetworkError": "connection_failed",
    "ServerError": "server_error",
    "TruncatedResponseError": "server_error",
    "AnswerCutOff": "server_error",
    "InvalidStructuredOutputError": "server_error",
}


def error_category(exc: BaseException) -> str:
    """
    The kind of failure, in our own vocabulary: `billing` means top up the account,
    `authentication` that the provider's key is missing or refused,
    `rate_limited` means wait. Each dialect has its own names for these, so the log
    keeps ours and the renderings translate. Recorded with the failure, not derived
    at read time, because the exception is gone by then.
    """
    if isinstance(exc, MissingKey):
        return "authentication"
    for cls in type(exc).__mro__:
        if cls.__name__ == "ServerError" and getattr(exc, "status", 0) == 529:
            return "overloaded"
        if cls.__name__ in _KINDS:
            return _KINDS[cls.__name__]
    return "unknown"


def from_environment():
    backend = os.environ.get("YAIT_MODEL_BACKEND", "echo").strip() or "echo"
    if backend == "echo":
        return EchoModel()
    if backend == "aichain":
        return AichainModel()
    raise NotConfigured(f"unknown model backend {backend!r}")
