"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python3 code/main.py                      # audits dataset/deletion_requests.csv -> ./output.csv
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --out /tmp/sample_out.csv
    python3 code/main.py --trace traces.jsonl # also write per-case reasoning traces
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from ghostaudit.runner import build_auditor, run  # noqa: E402
from ghostaudit import usage  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None)
    ap.add_argument("--out", default=str(ROOT / "output.csv"))
    ap.add_argument("--trace", default=None)
    ap.add_argument("--refresh-vision", action="store_true",
                    help="re-extract screenshot facts with the vision model (needs ANTHROPIC_API_KEY)")
    a = ap.parse_args()
    dataset = Path(a.dataset)
    requests = Path(a.requests) if a.requests else dataset / "deletion_requests.csv"
    auditor = build_auditor(dataset, a.refresh_vision)
    rows, secs = run(dataset, requests, Path(a.out), Path(a.trace) if a.trace else None, auditor,
                     context_requests=[dataset / "deletion_requests.csv", dataset / "sample_deletion_requests.csv"])
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"wrote {len(rows)} rows to {a.out} in {secs:.1f}s  {counts}")
    print(usage.summary_line())


if __name__ == "__main__":
    main()
