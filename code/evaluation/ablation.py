"""Ablation: how well does run-cadence linking recover accounts whose owner is proven by identifiers?

All identifier-proven (subject, account) pairs are hidden at once, the global run planner is
re-solved, and the recovered accounts are compared with the proven ones.
"""
from __future__ import annotations

from pathlib import Path

from ghostaudit.engine import load_cases


def run(auditor, dataset: Path):
    cases = load_cases(dataset / "sample_deletion_requests.csv") + load_cases(dataset / "deletion_requests.csv")
    auditor.plan_runs(cases)
    truth = {cid: a["resource_id"] for cid, a in auditor.assign.items()
             if auditor.subjects[cid].user_id in auditor.owners(a["resource_id"])}
    hide = set(truth.values())
    orig_owners, orig_link = auditor.owners, auditor.linked_resources
    auditor.owners = lambda rid: set() if rid in hide else orig_owners(rid)
    auditor.linked_resources = lambda c, s: {k: v for k, v in orig_link(c, s).items() if k not in hide}
    try:
        auditor.plan_runs(cases)
        ok = sum(1 for cid, r in truth.items() if auditor.assign.get(cid, {}).get("resource_id") == r)
    finally:
        auditor.owners, auditor.linked_resources = orig_owners, orig_link
        auditor.plan_runs(cases)
    print(f"account recovery without identifiers: {ok}/{len(truth)} = {100 * ok / max(1, len(truth)):.1f}%")
    return ok, len(truth)
