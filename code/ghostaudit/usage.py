"""Model usage accounting (calls, tokens, estimated cost) for the usage report."""
from __future__ import annotations

from collections import defaultdict

# USD per million tokens (input, output) as published in Oct 2026; update if pricing changes
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (1.0, 5.0),
}

_calls = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_hits": 0})


def record(model: str, input_tokens: int, output_tokens: int, cached: bool = False):
    u = _calls[model]
    if cached:
        u["cached_hits"] += 1
        return
    u["calls"] += 1
    u["input_tokens"] += input_tokens
    u["output_tokens"] += output_tokens


def totals():
    out = {}
    for m, u in _calls.items():
        pin, pout = PRICES.get(m, (0.0, 0.0))
        out[m] = {**u, "cost_usd": u["input_tokens"] / 1e6 * pin + u["output_tokens"] / 1e6 * pout}
    return out


def summary_line():
    t = totals()
    if not t or not any(v["calls"] for v in t.values()):
        hits = sum(v["cached_hits"] for v in t.values())
        return f"model calls this run: 0 (deterministic pipeline; {hits} cached vision extractions reused)"
    parts = [f"{m}: {v['calls']} calls, {v['input_tokens']} in / {v['output_tokens']} out, ${v['cost_usd']:.4f}"
             for m, v in t.items()]
    return "model usage: " + "; ".join(parts)
