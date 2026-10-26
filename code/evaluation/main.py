"""Evaluation workflow.

    python3 code/evaluation/main.py            # score the agent on the public sample set
    python3 code/evaluation/main.py --custom   # also run the hand-built evaluation cases

The sample set is audited with the same planner as the full run (runs are planned over all
known requests, because deletion runs of different subjects interleave in system_activity).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
ROOT = CODE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE))

from ghostaudit.runner import build_auditor, run  # noqa: E402
from scoring import score, read_output  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--out", default=str(HERE / "sample_predictions.csv"))
    ap.add_argument("--custom", action="store_true")
    ap.add_argument("--show", action="store_true", help="print diffs for failed cases")
    ap.add_argument("--ablation", action="store_true", help="account-linking ablation on the full request set")
    a = ap.parse_args()
    ds = Path(a.dataset)
    auditor = build_auditor(ds)
    run(ds, ds / "sample_deletion_requests.csv", Path(a.out), HERE / "sample_traces.jsonl", auditor,
        context_requests=[ds / "deletion_requests.csv"])
    pred = read_output(a.out)
    gold = read_output(ds / "sample_output.csv")
    res = score(pred, gold)
    print(json.dumps({k: v for k, v in res.items() if k != "failures"}, indent=2))
    print("failed cases:", res["failures"])
    if a.show:
        for cid in res["failures"]:
            print("=" * 20, cid, "pred", pred[cid][0], "gold", gold[cid][0])
            gs = [tuple(s[k] for k in ("phase", "system_id", "resource_id", "operation", "evidence_id")) for s in gold[cid][1]]
            ps = [tuple(s[k] for k in ("phase", "system_id", "resource_id", "operation", "evidence_id")) for s in pred[cid][1]]
            for s in gs:
                print("   ", "  " if s in ps else "- ", s)
            for s in ps:
                if s not in gs:
                    print("    + ", s)
    if a.custom:
        import custom_cases
        custom_cases.run_all(auditor)
    if a.ablation:
        import ablation
        ablation.run(auditor, ds)


if __name__ == "__main__":
    main()
