"""
Named model servers: `openrouter/<model>` or `mygpu/<model>` reaches a server by
the name its owner gave it.

Any OpenAI-compatible server — an aggregator like OpenRouter, a vLLM or Ollama of
your own — is reached the same way: the library's `private/` provider, pointed at
the server's address. The address is never the caller's to choose. The owner of
the installation names servers in the `YAIT_ENDPOINTS` setting, one per line:

    mygpu=https://gpu.example.com/v1
    lab=https://llm.lab.example.org/v1

and a caller asks for `mygpu/llama-3.3-70b`, bringing that server's key in the
standard header like any other provider key.

A line may end with a JSON object of fields to add to every request body — how a
server is told something the OpenAI shape has no word for. OpenRouter, for one,
picks among many hosts of a model at different precisions, so a comparison of a
model "in FP8" has to say so:

    openrouter-fp8=https://openrouter.ai/api/v1 {"provider": {"quantizations": ["fp8"]}} A worker that went wherever a request
told it could be pointed at the platform's own internals; a worker that goes only
where the owner listed cannot.

OpenRouter is built in, so it works with no setting; a line naming `openrouter`
replaces it.

The deploy parses the setting before it changes anything, so a mistake stops the
release with its line named rather than surfacing on some caller's first request.
It reaches the worker as JSON in its environment.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

BUILT_IN = {"openrouter": "https://openrouter.ai/api/v1"}

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Invalid(ValueError):
    """The setting cannot be used as written."""


def _reserved() -> set[str]:
    """
    Provider names the library already routes by prefix — `openai/gpt-6-luna` —
    which a server of the same name would make ambiguous.
    """
    try:
        from yait_aichain.models._data import PROVIDERS
        return set(PROVIDERS)
    except Exception:  # noqa: BLE001 — without the library, nothing is routed by it
        return set()


def parse(text: str, *, allow_local: bool = False) -> dict[str, dict]:
    """
    `name=url [json]` lines to `{name: {"url": …, "extra": {…}}}`, over the
    built-in ones. Blank lines and `#` comments are ignored; anything else that
    does not fit raises, naming the line.
    """
    found = {name: {"url": url, "extra": {}} for name, url in BUILT_IN.items()}
    seen: set[str] = set()
    reserved = _reserved()
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, rest = (part.strip() for part in line.partition("="))
        url, _, extra_text = rest.partition(" ")
        where = f"YAIT_ENDPOINTS line {number} ({line!r})"
        extra: dict = {}
        if extra_text.strip():
            try:
                extra = json.loads(extra_text)
            except json.JSONDecodeError as exc:
                raise Invalid(f"{where}: after the address, a JSON object of request "
                              f"fields — {exc.msg}") from exc
            if not isinstance(extra, dict):
                raise Invalid(f"{where}: after the address, a JSON object of request fields")
            if {"model", "messages", "stream"} & set(extra):
                raise Invalid(f"{where}: the request's own fields cannot be set here")
        if not sep or not url:
            raise Invalid(f"{where}: expected name=https://…")
        if not _NAME.match(name):
            raise Invalid(f"{where}: a name is lowercase letters, digits and hyphens")
        if name in reserved:
            raise Invalid(f"{where}: {name!r} is a provider the library already routes "
                          f"by prefix; pick another name")
        if name in seen:
            raise Invalid(f"{where}: {name!r} is named twice")
        parsed = urlparse(url)
        local = parsed.hostname in _LOCAL_HOSTS
        if parsed.scheme != "https" and not (allow_local and local and parsed.scheme == "http"):
            raise Invalid(f"{where}: the address must be https://"
                          + (" (http://localhost is for local runs only)" if local else ""))
        if local and not allow_local:
            raise Invalid(f"{where}: a local address is for local runs only")
        if not parsed.hostname:
            raise Invalid(f"{where}: no host in {url!r}")
        seen.add(name)
        found[name] = {"url": url.rstrip("/"), "extra": extra}
    return found


def from_environment() -> dict[str, dict]:
    """
    The worker's list: JSON written by the deploy, or — for a local run — the
    same lines the GitHub variable holds.
    """
    raw = os.environ.get("YAIT_ENDPOINTS", "").strip()
    if raw.startswith("{"):
        built = {name: {"url": url, "extra": {}} for name, url in BUILT_IN.items()}
        return {**built, **json.loads(raw)}
    return parse(raw, allow_local=os.environ.get("YAIT_ENDPOINTS_ALLOW_LOCAL") == "1")


def resolve(model: str, servers: dict[str, dict] | None = None) -> tuple[str, str, dict] | None:
    """
    `openrouter/z-ai/glm-5.3-flash` → (its address, `z-ai/glm-5.3-flash`, the
    fields that server adds to a request).
    """
    name, sep, rest = (model or "").partition("/")
    servers = from_environment() if servers is None else servers
    if sep and rest and name in servers:
        return servers[name]["url"], rest, servers[name].get("extra") or {}
    return None
