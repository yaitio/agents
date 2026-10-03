#!/usr/bin/env python3
"""
Vaults, without a network: the seal, the store, and that no secret ever leaves.

    python3 tests/test_vault.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import render  # noqa: E402
import vault as V  # noqa: E402


class Results:
    def __init__(self, name: str) -> None:
        self.name, self.passed, self.failures = name, 0, []

    def check(self, what: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{what}: {detail}")

    def report(self) -> int:
        print(f"{self.name}: {self.passed}/{self.passed + len(self.failures)} checks passed")
        for f in self.failures:
            print(f"  FAIL  {f}")
        return 1 if self.failures else 0


BEARER = {"type": "static_bearer", "mcp_server_url": "https://mcp.exa.ai/mcp",
          "token": "exa-secret-123"}
OAUTH = {"type": "mcp_oauth", "mcp_server_url": "https://mcp.example.com/",
         "access_token": "at-secret", "expires_at": "2026-10-01T00:00:00Z",
         "refresh": {"client_id": "cid", "refresh_token": "rt-secret",
                     "token_endpoint": "https://auth.example.com/token",
                     "token_endpoint_auth": {"type": "client_secret_post",
                                             "client_secret": "cs-secret"},
                     "scope": "read"}}
SECRETS = ("exa-secret-123", "at-secret", "rt-secret", "cs-secret")


def sealing() -> int:
    res = Results("seal")
    os.environ.pop("YAIT_VAULT_KEY", None)
    os.environ.pop("YAIT_VAULT_KEY_SECRET", None)
    V.reset_cache()
    try:
        V.seal({"token": "x"}, tenant="t", vault_id="v", credential_id="c")
        res.check("without a key nothing is sealed", False, "it sealed")
    except V.NotConfigured:
        res.check("without a key nothing is sealed", True)

    os.environ["YAIT_VAULT_KEY"] = V.new_key()
    V.reset_cache()
    blob = V.seal({"token": "exa-secret-123"}, tenant="t", vault_id="v", credential_id="c")
    res.check("the sealed form does not contain the secret", "exa-secret" not in blob)
    res.check("and opens with the same key on the same record",
              V.unseal(blob, tenant="t", vault_id="v", credential_id="c")
              == {"token": "exa-secret-123"})
    for name, where in (("another tenant", {"tenant": "u", "vault_id": "v", "credential_id": "c"}),
                        ("another vault", {"tenant": "t", "vault_id": "w", "credential_id": "c"}),
                        ("another credential", {"tenant": "t", "vault_id": "v", "credential_id": "d"})):
        try:
            V.unseal(blob, **where)
            res.check(f"copied onto {name}, it does not open", False, "it opened")
        except Exception:  # noqa: BLE001 — cryptography's InvalidTag
            res.check(f"copied onto {name}, it does not open", True)
    other = V.seal({"token": "exa-secret-123"}, tenant="t", vault_id="v", credential_id="c")
    res.check("the same secret seals differently each time", other != blob)
    return res.report()


def storing() -> int:
    res = Results("store")
    os.environ["YAIT_VAULT_KEY"] = V.new_key()
    V.reset_cache()
    vaults = V.Vaults(V.MemoryVaults())
    v = vaults.create("acme", name="Alice's tools", metadata={"user": "alice"})
    res.check("a vault is made", v["vault_id"].startswith("vlt_") and v["name"] == "Alice's tools")
    c = vaults.add("acme", v["vault_id"], auth=BEARER, name="exa")
    res.check("a credential never carries its secret out",
              "secret" not in c and not any(s in json.dumps(c) for s in SECRETS), f"{c}")
    o = vaults.add("acme", v["vault_id"], auth=OAUTH)
    res.check("an OAuth credential keeps its refresh settings, not its tokens",
              o["auth"]["refresh"]["client_id"] == "cid"
              and o["auth"]["refresh"]["token_endpoint_auth"] == {"type": "client_secret_post"}
              and not any(s in json.dumps(o) for s in SECRETS), f"{o}")
    res.check("the server's address is kept without a trailing slash",
              o["auth"]["mcp_server_url"] == "https://mcp.example.com")
    for dialect in (render.ANTHROPIC, render.OPENAI):
        shown = json.dumps([render.credential(x, dialect)
                            for x in vaults.credentials("acme", v["vault_id"])])
        res.check(f"no secret in the {dialect} rendering", not any(s in shown for s in SECRETS))
    stored = json.dumps(list(vaults.backend._items.values()), default=str)
    res.check("no secret in the store in the clear", not any(s in stored for s in SECRETS))

    try:
        vaults.add("acme", v["vault_id"], auth=BEARER)
        res.check("a second credential for the same server is refused", False, "accepted")
    except V.Invalid:
        res.check("a second credential for the same server is refused", True)
    for name, auth in (("http, not https", {**BEARER, "mcp_server_url": "http://x"}),
                       ("no token", {"type": "static_bearer", "mcp_server_url": "https://x"}),
                       ("an unknown type", {**BEARER, "type": "password"}),
                       ("an environment variable, with no sandbox",
                        {"type": "environment_variable", "secret_name": "X"}),
                       ("an unknown field", {**BEARER, "extra": 1})):
        try:
            vaults.add("acme", v["vault_id"], auth=auth)
            res.check(f"{name} is refused", False, "accepted")
        except V.Invalid:
            res.check(f"{name} is refused", True)

    found = vaults.secret_for("acme", [v["vault_id"]], "https://mcp.exa.ai/mcp/")
    res.check("the worker finds the secret by the server's address",
              found and found["secret"] == {"token": "exa-secret-123"}, f"{found}")
    res.check("and nothing for a server no vault holds",
              vaults.secret_for("acme", [v["vault_id"]], "https://other.example") is None)
    try:
        vaults.get("other-tenant", v["vault_id"])
        res.check("another tenant cannot see the vault", False, "it could")
    except V.NotFound:
        res.check("another tenant cannot see the vault", True)
    try:
        vaults.check_ids("other-tenant", [v["vault_id"]])
        res.check("nor put it on a session", False, "it could")
    except V.Invalid:
        res.check("nor put it on a session", True)

    rotated = vaults.rotate("acme", v["vault_id"], c["credential_id"],
                            auth={**BEARER, "token": "exa-new"})
    res.check("rotating replaces the secret",
              vaults.secret_for("acme", [v["vault_id"]], BEARER["mcp_server_url"])
              ["secret"] == {"token": "exa-new"} and "exa-new" not in json.dumps(rotated))
    partial = vaults.rotate("acme", v["vault_id"], o["credential_id"],
                            auth={"type": "mcp_oauth", "expires_at": "2031-01-01T00:00:00Z"})
    kept = vaults.secret_for("acme", [v["vault_id"]], OAUTH["mcp_server_url"])["secret"]
    res.check("a partial update keeps the tokens it does not name",
              partial["auth"]["expires_at"] == "2031-01-01T00:00:00Z"
              and kept == {"access_token": "at-secret", "refresh_token": "rt-secret",
                           "client_secret": "cs-secret"}, f"{partial} / {kept}")
    try:
        vaults.rotate("acme", v["vault_id"], c["credential_id"],
                      auth={**BEARER, "mcp_server_url": "https://elsewhere.example"})
        res.check("rotating cannot move a credential to another server", False, "moved")
    except V.Invalid:
        res.check("rotating cannot move a credential to another server", True)

    archived = vaults.archive_credential("acme", v["vault_id"], c["credential_id"])
    res.check("an archived credential is not used",
              archived["archived_at"] and vaults.secret_for(
                  "acme", [v["vault_id"]], BEARER["mcp_server_url"]) is None)
    res.check("and its secret is gone from the store",
              vaults.backend._get(V.key("acme", v["vault_id"]),
                                  f"C#{c['credential_id']}")["secret"] is None)
    vaults.archive("acme", v["vault_id"])
    try:
        vaults.check_ids("acme", [v["vault_id"]])
        res.check("an archived vault cannot be put on a session", False, "it could")
    except V.Invalid:
        res.check("an archived vault cannot be put on a session", True)
    vaults.delete("acme", v["vault_id"])
    res.check("a deleted vault takes its credentials with it",
              not vaults.backend._items, f"{list(vaults.backend._items)}")
    return res.report()


def main() -> int:
    rc = sealing() | storing()
    os.environ.pop("YAIT_VAULT_KEY", None)
    V.reset_cache()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
