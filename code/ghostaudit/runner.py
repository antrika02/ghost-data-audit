"""Shared runner used by code/main.py and the evaluation workflow."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .engine import Auditor, load_cases

CODE = Path(__file__).resolve().parent.parent


def build_auditor(dataset: Path, refresh_vision: bool = False) -> Auditor:
    from .vision import load_image_facts
    facts = load_image_facts(dataset, CODE / "cache" / "image_facts.json", refresh=refresh_vision)
    return Auditor(dataset, facts)


def run(dataset: Path, requests: Path, out: Path, trace_path: Path | None = None, auditor: Auditor | None = None,
        context_requests: list[Path] | None = None):
    t0 = time.time()
    auditor = auditor or build_auditor(dataset)
    cases = load_cases(requests)
    # deletion runs of different subjects interleave, so runs are planned over every request we know about
    all_cases = list(cases)
    for p in context_requests or []:
        if p.exists() and p.resolve() != requests.resolve():
            all_cases += load_cases(p)
    auditor.plan_runs(all_cases)
    rows, traces = [], []
    for c in sorted(cases, key=lambda c: (len(c.case_id), c.case_id)):
        status, steps, trace = auditor.audit(c)
        rows.append({"case_id": c.case_id, "deletion_status": status,
                     "steps_json": json.dumps(steps, ensure_ascii=False, separators=(",", ":"))})
        traces.append({"case_id": c.case_id, "status": status, **trace})
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if trace_path:
        with open(trace_path, "w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
    return rows, time.time() - t0


