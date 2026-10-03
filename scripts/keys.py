#!/usr/bin/env python3
"""
The installation's tokens, from the owner's machine.

    python3 scripts/keys.py keygen --region us-west-1          # once
    python3 scripts/keys.py issue --tenant acme --name alice    # per user
    python3 scripts/keys.py vault-key --region us-west-1       # once, for vaults

`keygen` makes an Ed25519 key pair, keeps the private key here —
~/.config/yait/<prefix>/<kid>.pem, readable by you alone — and puts the public
key in the agents table, where the deployment reads it to check tokens. It runs
with your own AWS credentials; the deployment never sees the private key, so it
can check a token but never make one.

`issue` signs a token with that private key and prints it. It is sent as
X-Yait-Key; its tenant decides whose agents and sessions are whose. A token has
no expiry: to cut every token off, remove its public key from the table
(`revoke-key`) — a warm function may trust it for five more minutes.
"""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import names as N  # noqa: E402
import tokens  # noqa: E402

HOME = pathlib.Path(os.environ.get("YAIT_KEYS_DIR",
                                   pathlib.Path.home() / ".config" / "yait" / N.PREFIX))


def _table(region: str):
    import boto3
    return boto3.resource("dynamodb", region_name=region).Table(N.AGENTS_TABLE)


def _private(kid: str) -> pathlib.Path:
    return HOME / f"{kid}.pem"


def keygen(a) -> int:
    kid = a.kid or datetime.date.today().strftime("k%Y%m%d")
    path = _private(kid)
    if path.exists():
        print(f"{path} exists; pick another --kid to add a second key", file=sys.stderr)
        return 2
    private, public = tokens.keypair()
    HOME.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)
    path.write_text(private)
    path.chmod(0o600)
    _table(a.region).put_item(Item={
        "pk": tokens.KEYS_PARTITION, "sk": tokens.KEY_ITEM_PREFIX + kid,
        "public_key": public,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    print(f"key {kid}: private key at {path}; public key in {N.AGENTS_TABLE}")
    return 0


def _default_kid() -> str | None:
    found = sorted(HOME.glob("*.pem")) if HOME.exists() else []
    return found[-1].stem if found else None


def issue(a) -> int:
    kid = a.kid or _default_kid()
    if not kid or not _private(kid).exists():
        print(f"no private key in {HOME}: run keygen first", file=sys.stderr)
        return 2
    print(tokens.issue(_private(kid).read_text(), kid=kid, tenant=a.tenant, name=a.name))
    return 0


def revoke_key(a) -> int:
    _table(a.region).delete_item(
        Key={"pk": tokens.KEYS_PARTITION, "sk": tokens.KEY_ITEM_PREFIX + a.kid})
    print(f"key {a.kid} removed: every token it signed stops working within "
          f"{tokens.CACHE_SECONDS // 60} minutes. The private key stays in {HOME}.")
    return 0


def vault_key(a) -> int:
    """The key vault secrets are sealed with, made once, in Secrets Manager."""
    import boto3
    import vault
    client = boto3.client("secretsmanager", region_name=a.region)
    try:
        client.create_secret(Name=N.VAULT_KEY_SECRET, SecretString=vault.new_key(),
                             Description="yait agents: the key vault secrets are sealed with")
    except client.exceptions.ResourceExistsException:
        print(f"{N.VAULT_KEY_SECRET} exists already: replacing it would make every "
              f"stored secret unreadable", file=sys.stderr)
        return 2
    print(f"vault key made in {N.VAULT_KEY_SECRET}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("keygen", help="a key pair; the public half to the agents table")
    g.add_argument("--region", required=True)
    g.add_argument("--kid", help="the key's id (default: k<today>)")
    g.set_defaults(run=keygen)
    i = sub.add_parser("issue", help="a token, printed")
    i.add_argument("--tenant", default="default")
    i.add_argument("--name", default="", help="who it is for, for logs")
    i.add_argument("--kid", help="which key signs it (default: the newest)")
    i.set_defaults(run=issue)
    r = sub.add_parser("revoke-key", help="remove a public key, and every token it signed")
    r.add_argument("--region", required=True)
    r.add_argument("--kid", required=True)
    r.set_defaults(run=revoke_key)
    v = sub.add_parser("vault-key", help="the key vault secrets are sealed with, once")
    v.add_argument("--region", required=True)
    v.set_defaults(run=vault_key)
    a = ap.parse_args()
    return a.run(a)


if __name__ == "__main__":
    raise SystemExit(main())
