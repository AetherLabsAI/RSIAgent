#!/usr/bin/env python3
"""Spend meter (PREREG_E7 v2.2 §5, B8): a FROZEN price table + accumulators
over the per-call usage lines that llm/client.py writes into every run .out:

    [INFO forge.llm] z-ai/glm-5.2 usage: in=3062 out=616 reasoning=209

The reasoning suffix is informational only — the provider bills reasoning
inside completion_tokens (out=), so in/out are the complete billable pair.

B8: the table is FROZEN at registration (MappingProxyType) and unit-tested
against a fixture log (tests/test_spend.py). The $60 checkpoint arithmetic
(M4) and the $230 breaker (M14) both ride on these numbers — they must not
drift silently, and a model missing from the table must SURFACE (unpriced),
never bill as zero in silence.
"""
import glob
import re
import types

# USD per MILLION tokens, (input, output) — the e7-era seats, frozen at
# registration. A new seat means a NEW registered table, not an edit here.
PRICE_TABLE = types.MappingProxyType({
    "glm-5.2": (0.74, 2.32),     # z-ai/glm-5.2        (actor + curriculum)
    "kimi-k3": (3.0, 15.0),      # moonshotai/kimi-k3  (eyes + verifier agent)
})

# model token is the \S+ immediately before "usage:"; in=None lines (a
# provider omitting usage) simply do not match — counted nowhere.
_USAGE = re.compile(r"(\S+) usage: in=(\d+) out=(\d+)")


def _price_for(model: str):
    for sub, p in PRICE_TABLE.items():
        if sub in model:
            return p
    return None


def parse_night_cost(out_file: str) -> dict:
    """Sum one run .out file's usage lines.

    Returns {"usd", "per_model": {model: {in, out, calls, usd}}, "unpriced"}.
    unpriced = models seen in the log but absent from PRICE_TABLE; they
    contribute $0 but are surfaced so the driver can log the gap.
    """
    per = {}
    try:
        fh = open(out_file, encoding="utf-8", errors="ignore")
    except OSError:
        return {"usd": 0.0, "per_model": {}, "unpriced": []}
    with fh:
        for line in fh:
            m = _USAGE.search(line)
            if not m:
                continue
            d = per.setdefault(m.group(1),
                               {"in": 0, "out": 0, "calls": 0, "usd": 0.0})
            d["in"] += int(m.group(2))
            d["out"] += int(m.group(3))
            d["calls"] += 1
    total, unpriced = 0.0, []
    for model, d in per.items():
        p = _price_for(model)
        if p is None:
            unpriced.append(model)
            continue
        d["usd"] = round(d["in"] / 1e6 * p[0] + d["out"] / 1e6 * p[1], 6)
        total += d["usd"]
    return {"usd": round(total, 6), "per_model": per,
            "unpriced": sorted(unpriced)}


def era_spend(dir_glob) -> dict:
    """Accumulate parse_night_cost over a glob (or list of globs) of .out
    files — the driver's nightly era_spend_usd (M4/M14).

    Returns {"usd", "n_files", "per_model", "unpriced"}.
    """
    patterns = [dir_glob] if isinstance(dir_glob, str) else list(dir_glob)
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    total, per, unpriced = 0.0, {}, set()
    for path in sorted(set(files)):
        night = parse_night_cost(path)
        total += night["usd"]
        unpriced.update(night["unpriced"])
        for model, d in night["per_model"].items():
            agg = per.setdefault(model,
                                 {"in": 0, "out": 0, "calls": 0, "usd": 0.0})
            for k in ("in", "out", "calls"):
                agg[k] += d[k]
            agg["usd"] = round(agg["usd"] + d["usd"], 6)
    return {"usd": round(total, 6), "n_files": len(set(files)),
            "per_model": per, "unpriced": sorted(unpriced)}
