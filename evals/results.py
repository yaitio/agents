"""
Where a quality run's results live, what they were run under, and what they cost.

A run is a directory under `evals/results/`, kept where it ran: the ledger (every
attempt, as it happened), the passport (what the run was run under), and the
report. A comparison months later is only as good as the answer to "were these
two run the same way?", and the passport is that answer.

Cost is priced here, from `evals/prices.json`, never from a library's table: each
price there says where it was read and when, and a report reprices a ledger from
its tokens — so a price that changes, or one that was missing at the time, never
needs the models called again.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.metadata
import json
import pathlib
import platform
import subprocess
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRICES = ROOT / "evals" / "prices.json"
RESULTS = ROOT / "evals" / "results"
PER = 1_000_000


# ── prices ─────────────────────────────────────────────────────────────────

def load_prices(path: pathlib.Path = PRICES) -> dict:
    return json.loads(path.read_text())["models"]


def period(prices: dict, model: str, on: datetime.date) -> dict | None:
    """
    The price in force on a date. A run before the earliest known price is priced
    at that earliest one, and says so: the table began after some of the runs.
    """
    periods = prices.get(model) or []
    for p in periods:
        start = datetime.date.fromisoformat(p["from"])
        end = datetime.date.fromisoformat(p["until"]) if p.get("until") else None
        if start <= on and (end is None or on <= end):
            return p
    if periods and on < datetime.date.fromisoformat(periods[0]["from"]):
        return {**periods[0], "note": (periods[0].get("note", "") +
                                       "; priced at the earliest known price").lstrip("; ")}
    return None


def cost(meta: dict, p: dict) -> tuple[float, list[str]]:
    """What one attempt cost at price `p`, and anything the price could not say."""
    notes = []
    fresh = meta.get("input_tokens") or 0
    out = meta.get("output_tokens") or 0
    read = meta.get("cache_read_tokens") or 0
    write = meta.get("cache_write_tokens") or 0
    rate_read, rate_write = p.get("cache_read"), p.get("cache_write")
    if read and rate_read is None:
        notes.append("cache reads priced as fresh input — no published cache price")
        rate_read = p["input"]
    if write and rate_write is None:
        notes.append("cache writes priced as fresh input — no published cache price")
        rate_write = p["input"]
    usd = (fresh * p["input"] + out * p["output"]
           + read * (rate_read or 0) + write * (rate_write or 0)) / PER
    return usd, notes


def reprice(records: list, model_of: Callable[[str], str], on: datetime.date,
            prices: dict | None = None) -> tuple[list, dict]:
    """
    The records with `cost` from the price table, and per model what the pricing
    could not say. A record whose model has no price keeps cost 0 and is marked
    unpriced — never free.
    """
    from yait_aichain.eval._records import Record
    prices = prices if prices is not None else load_prices()
    out, notes = [], {}
    for r in records:
        model = model_of(r.arm)
        p = period(prices, model, on)
        meta = dict(r.meta or {})
        if p is None:
            meta["priced"] = False
            notes.setdefault(model, set()).add("no price in evals/prices.json")
            out.append(Record(**{**r.as_dict(), "cost": 0.0, "meta": meta}))
            continue
        usd, why = cost(meta, p)
        meta.update(priced=True, price_from=p["from"], price_source=p["source"])
        for n in why + ([p["note"]] if p.get("note") else []):
            notes.setdefault(model, set()).add(n)
        out.append(Record(**{**r.as_dict(), "cost": usd, "meta": meta}))
    return out, {m: sorted(n) for m, n in notes.items()}


def pricing_note(notes: dict) -> str:
    if not notes:
        return ""
    return "pricing\n" + "\n".join(f"  {m}: {'; '.join(n)}" for m, n in sorted(notes.items()))


# ── the passport ───────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def passport(*, name: str, arms: list[dict], cases_path: pathlib.Path, cases: list,
             trials: int, delta: float, concurrency: int, prices: dict,
             on: datetime.date) -> dict:
    """
    What a run was run under — enough to say, later, whether two runs are
    comparable. The code is named by its commit, and a working tree with changes
    says so: a result from uncommitted code is not reproducible from the history.
    """
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    groups: dict = {}
    for c in cases:
        groups[c.group or ""] = groups.get(c.group or "", 0) + 1
    models = sorted({a["model"] for a in arms})
    return {
        "name": name,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "code": {"commit": _git("rev-parse", "HEAD"), "uncommitted_changes": bool(dirty),
                 "instrument_sha256": _sha256(pathlib.Path(__file__).with_name("quality.py"))},
        "versions": {"yait-aichain": _version("yait-aichain"),
                     "anthropic": _version("anthropic"),
                     "python": platform.python_version()},
        "cases": {"path": str(cases_path.relative_to(ROOT)) if cases_path.is_relative_to(ROOT)
                  else str(cases_path),
                  "sha256": _sha256(cases_path), "count": len(cases), "groups": groups},
        "arms": arms,
        "trials": trials, "delta": delta, "concurrency": concurrency,
        "prices": {m: period(prices, m, on) for m in models},
    }


# ── storage ────────────────────────────────────────────────────────────────

def run_dir(name: str, on: datetime.date) -> pathlib.Path:
    """`evals/results/<date>-<name>/` — dated, so a listing reads as a history."""
    stem = name if name[:4].isdigit() else f"{on.isoformat()}-{name}"
    path = RESULTS / stem
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: pathlib.Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False, default=str) + "\n")


def read_passport(directory: pathlib.Path) -> dict | None:
    path = directory / "passport.json"
    return json.loads(path.read_text()) if path.exists() else None
