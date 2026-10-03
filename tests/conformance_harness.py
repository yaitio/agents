"""
What the two conformance suites share: the outcome ledger with its known-gap
semantics, and a local server to run against when no deployment is named.

A known gap is a check we know fails, listed with its reason. It is expected to
fail, and the suite stays green while it does. If it starts passing, the suite goes
red and says so — a fix cannot go unrecorded and a gap cannot be forgotten.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import tokens  # noqa: E402

#: A key pair made for this run, as `scripts/keys.py keygen` makes one for an
#: installation: the local server gets the public half, the suite a token signed
#: with the private half — a real JWT, checked the way the gateway checks it.
LOCAL_KID = "local"
_PRIVATE, _PUBLIC = tokens.keypair()
LOCAL_PUBLIC_KEYS = json.dumps({LOCAL_KID: _PUBLIC})
KEY = tokens.issue(_PRIVATE, kid=LOCAL_KID, tenant="default", name="conformance")


#: The local server's vault key, made for the run like the signing key.
VAULT_KEY = __import__("vault").new_key()


def local_token(tenant: str, name: str = "") -> str:
    """Another token from the run's key — a second user of the local server."""
    return tokens.issue(_PRIVATE, kid=LOCAL_KID, tenant=tenant, name=name)


class Outcomes:
    def __init__(self, known_gaps: dict[str, str]) -> None:
        self.known_gaps = known_gaps
        self.rows: list[tuple[str, str, str]] = []

    def record(self, name: str, errors: list[str]) -> None:
        expected = name in self.known_gaps
        if not errors:
            state = "UNEXPECTED PASS" if expected else "pass"
        else:
            state = "known gap" if expected else "FAIL"
        detail = "; ".join(errors[:4])
        if state == "known gap":
            detail = self.known_gaps[name]
        self.rows.append((name, state, detail))

    def report(self) -> int:
        width = max(len(n) for n, _, _ in self.rows)
        for name, state, detail in self.rows:
            line = f"  {state:15}  {name:<{width}}"
            if state in ("FAIL", "known gap") and detail:
                line += f"\n  {'':15}  {'':<{width}}  {detail}"
            if state == "UNEXPECTED PASS":
                line += "  <- the gap is closed; remove it from KNOWN_GAPS"
            print(line)
        counts = {s: sum(1 for _, x, _ in self.rows if x == s)
                  for s in ("pass", "known gap", "FAIL", "UNEXPECTED PASS")}
        print("\n" + "   ".join(f"{k}: {v}" for k, v in counts.items()))
        return 1 if counts["FAIL"] or counts["UNEXPECTED PASS"] else 0


def main(run: Callable[[str, str], int], probe: str) -> int:
    """`--base-url` runs against a deployment; without it, against a local server."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--key")
    ap.add_argument("--port", type=int, default=8131)
    a = ap.parse_args()

    if a.base_url is not None:
        if not a.base_url.strip():
            print("--base-url was given but empty; refusing to fall back to a local "
                  "server.", file=sys.stderr)
            return 2
        return run(a.base_url, a.key or KEY)

    with serve_locally(a.port, probe) as base:
        return run(base, KEY)


#: The test MCP server's tokens for a local run: who each one is, as `whoami` says.
MCP_TOKENS = {"alice": "local-mcp-token-alice", "bob": "local-mcp-token-bob"}


@contextlib.contextmanager
def serve_mcp_faults(port: int = 8132):
    """The test MCP server (tests/mcp_faults), on this machine, for as long as the
    block runs. Yields its URL."""
    environment = {**os.environ, "PORT": str(port),
                   "MCP_FAULTS_TOKENS": ",".join(f"{k}={v}" for k, v in MCP_TOKENS.items())}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mcp_faults" / "server.py")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=environment)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def is_local(base: str) -> bool:
    return base.startswith(("http://127.0.0.1", "http://localhost"))


@contextlib.contextmanager
def serve_locally(port: int, probe: str, backend: str = "echo"):
    """
    A local server for as long as the block runs — on the echo backend unless told
    otherwise: deterministic and free. `aichain` takes provider keys from the
    environment, as the function does when no secret is configured.
    """
    environment = {**os.environ, "YAIT_JWT_PUBLIC_KEYS": LOCAL_PUBLIC_KEYS,
                   "YAIT_VAULT_KEY": VAULT_KEY, "YAIT_MODEL_BACKEND": backend,
                   "YAIT_ALLOW_LOCAL_MCP": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "local_server.py"), "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + probe, timeout=1)
            except urllib.error.HTTPError:
                break                               # it answers; a 401 is fine here
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ── clients, the way a real one is set up ──────────────────────────────────

def anthropic_client(base: str, *, yait_key: str | None = None,
                     provider_key: str | None = None, original: bool = False, **kw):
    """
    The official SDK against the original or against us. Against us, the
    installation key goes in X-Yait-Key and the provider key where it always goes;
    with no provider key the SDK is told to leave that header out — it refuses to
    send a request with no credential otherwise — and the deployment's own keys
    answer.
    """
    import anthropic
    if original:
        return anthropic.Anthropic(base_url=base.rstrip("/"), api_key=provider_key, **kw)
    headers = {"X-Yait-Key": yait_key or ""}
    if not provider_key:
        headers["X-Api-Key"] = anthropic.omit
    return anthropic.Anthropic(base_url=f"{base.rstrip('/')}/anthropic",
                               api_key=provider_key, default_headers=headers, **kw)


def openai_headers(*, yait_key: str | None = None,
                   provider_key: str | None = None) -> dict:
    """What an OpenAI-dialect request carries: ours, theirs, or both."""
    headers = {"OpenAI-Beta": "agents=v1"}     # the original requires it
    if yait_key:
        headers["X-Yait-Key"] = yait_key
    if provider_key:
        headers["Authorization"] = f"Bearer {provider_key}"
    return headers


def provider_key_for(model: str | None) -> str | None:
    """
    The provider key a client would bring for this model — its provider's, from the
    environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, …). None when there is none,
    which on a deployment means the model will not be called: the installation
    never pays for tokens.
    """
    if not model:
        return None
    # A named server's key goes by its name — mygpu/… takes MYGPU_API_KEY — or by
    # the name of another server at the same host: openrouter-fp8/… is OpenRouter,
    # and takes OPENROUTER_API_KEY. The list is the deployment's (YAIT_ENDPOINTS),
    # which a client comparing against a deployment sets to the same value.
    server = model.split("/", 1)[0] if "/" in model else ""
    if server:
        sys.path.insert(0, str(ROOT / "src"))
        import endpoints
        from urllib.parse import urlparse
        servers = endpoints.from_environment()
        if server in servers:
            host = urlparse(servers[server]["url"]).hostname
            names = [server] + [n for n, s in servers.items()
                                if n != server and urlparse(s["url"]).hostname == host]
            for name in names:
                key = os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY")
                if key:
                    return key
            return None
    try:
        from yait_aichain.models._base import PROVIDERS, _resolve_provider
        env_key = PROVIDERS[_resolve_provider(model)]["provider"]["env_key"]
    except Exception:  # noqa: BLE001 — a name the library does not know: none
        return None
    return os.environ.get(env_key) or None


# ── keys from env files ────────────────────────────────────────────────────

#: What --env-file may take from a file, and nothing else: a .env beside other
#: projects' settings holds far more than this run needs.
KEYS_FROM_FILES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "YAIT_CLIENT_KEY",
                   "GOOGLE_AI_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY",
                   "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "OPENROUTER_API_KEY",
                   # The MCP servers the tool cases use, and their test credentials.
                   "EXA_API_KEY", "IDNTTY_KEY_A", "IDNTTY_KEY_B",
                   "MCP_FAULTS_URL", "MCP_FAULTS_TOKEN_A", "MCP_FAULTS_TOKEN_B")
DEFAULT_ENV_FILES = (ROOT / ".env", ROOT.parent / "aichain" / ".env")


def load_keys(paths) -> list[str]:
    """
    The run's keys from env files, never overriding the process environment and
    never printed. Returns the names found, so the caller can say which it has.
    """
    found = []
    for path in paths:
        path = pathlib.Path(path)
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            name, sep, value = line.strip().partition("=")
            name = name.removeprefix("export").strip()
            if not sep or name not in KEYS_FROM_FILES or os.environ.get(name):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()   # a note after the value
            if value:
                os.environ[name] = value
                found.append(f"{name} ({path})")
    return found


