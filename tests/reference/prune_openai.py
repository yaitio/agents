#!/usr/bin/env python3
"""
Cut the Agents API, and the vaults its sessions use, out of OpenAI's published OpenAPI document.

The whole document is ~4 MB of YAML; the part the conformance suite validates
against is the `/agents` paths and every schema they reach. That part is kept in
the repository, with where it came from and the hash of what it was cut from, so a
run never depends on the network and a re-baseline is a visible diff.

    python3 tests/reference/prune_openai.py                 # fetch upstream
    python3 tests/reference/prune_openai.py --source oai.yaml
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import urllib.request

import yaml

SOURCE = "https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml"
OUT = pathlib.Path(__file__).with_name("openai-agents.json")


def refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            found.add(ref)
        for value in node.values():
            refs(value, found)
    elif isinstance(node, list):
        for value in node:
            refs(value, found)


def prune(doc: dict) -> dict:
    paths = {p: ops for p, ops in doc["paths"].items()
             if p in ("/agents", "/vaults")
             or p.startswith(("/agents/", "/vaults/"))}
    wanted: set[str] = set()
    refs(paths, wanted)
    seen: set[str] = set()
    while wanted - seen:
        ref = sorted(wanted - seen)[0]
        seen.add(ref)
        _, _, kind, name = ref.split("/", 3)
        refs(doc["components"][kind][name], wanted)
    components: dict = {}
    for ref in sorted(seen):
        _, _, kind, name = ref.split("/", 3)
        components.setdefault(kind, {})[name] = doc["components"][kind][name]
    return {"openapi": doc["openapi"], "info": doc["info"], "paths": paths,
            "components": components}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="a local copy instead of fetching upstream")
    a = ap.parse_args()
    if a.source:
        raw = pathlib.Path(a.source).read_bytes()
    else:
        with urllib.request.urlopen(SOURCE, timeout=60) as reply:
            raw = reply.read()
    doc = yaml.load(raw, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    pruned = prune(doc)
    pruned["x-yait-provenance"] = {
        "source": SOURCE,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "api_version": doc["info"].get("version"),
        "pruned_on": datetime.date.today().isoformat(),
    }
    OUT.write_text(json.dumps(pruned, indent=1, sort_keys=True, default=str) + "\n")
    print(f"{OUT.name}: {len(pruned['paths'])} paths, "
          f"{sum(len(v) for v in pruned['components'].values())} components, "
          f"{OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
