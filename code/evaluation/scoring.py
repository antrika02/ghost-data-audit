"""Scoring helpers shared by the evaluation workflow."""
from __future__ import annotations

import json
from collections import Counter

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def step_key(s: dict, with_evidence=True):
    return tuple(str(s.get(f, "")) for f in (FIELDS if with_evidence else FIELDS[:-1]))


def lcs(a, b):
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    return dp[0][0]


def score(pred_rows: dict, gold_rows: dict) -> dict:
    """pred_rows/gold_rows: case_id -> (status, steps list)."""
    n = len(gold_rows)
    status_ok = 0
    tp = fp = fn = 0
    tp_ne = fp_ne = fn_ne = 0
    field_hits = Counter()
    field_total = 0
    seq_exact = 0
    seq_lcs = 0.0
    case_exact = 0
    failures = []
    for cid, (gst, gsteps) in gold_rows.items():
        pst, psteps = pred_rows.get(cid, ("MISSING", []))
        status_ok += pst == gst
        gk = Counter(step_key(s) for s in gsteps)
        pk = Counter(step_key(s) for s in psteps)
        inter = sum((gk & pk).values())
        tp += inter
        fp += sum(pk.values()) - inter
        fn += sum(gk.values()) - inter
        gk2 = Counter(step_key(s, False) for s in gsteps)
        pk2 = Counter(step_key(s, False) for s in psteps)
        i2 = sum((gk2 & pk2).values())
        tp_ne += i2
        fp_ne += sum(pk2.values()) - i2
        fn_ne += sum(gk2.values()) - i2
        # field-level: align by position after sorting both in their own order
        for i, g in enumerate(gsteps):
            field_total += 1
            p = psteps[i] if i < len(psteps) else {}
            for f in FIELDS + ("step_no",):
                field_hits[f] += str(p.get(f, "")) == str(g.get(f, ""))
        ga = [step_key(s) for s in gsteps]
        pa = [step_key(s) for s in psteps]
        seq_exact += ga == pa
        seq_lcs += (lcs(ga, pa) / max(len(ga), len(pa))) if (ga or pa) else 1.0
        ok = pst == gst and ga == pa
        case_exact += ok
        if not ok:
            failures.append(cid)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    prec2 = tp_ne / (tp_ne + fp_ne) if tp_ne + fp_ne else 1.0
    rec2 = tp_ne / (tp_ne + fn_ne) if tp_ne + fn_ne else 1.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": prec, "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0,
        "step_f1_ignoring_evidence": 2 * prec2 * rec2 / (prec2 + rec2) if prec2 + rec2 else 0,
        "field_accuracy": {f: field_hits[f] / field_total for f in FIELDS + ("step_no",)} if field_total else {},
        "sequence_exact": seq_exact / n,
        "sequence_lcs": seq_lcs / n,
        "case_exact": case_exact / n,
        "failures": failures,
    }


def read_output(path) -> dict:
    import csv
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        out[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"]))
    return out
