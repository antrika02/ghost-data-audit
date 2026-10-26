"""Hand-built evaluation cases that probe behaviours the public sample does not isolate.

Each case perturbs a real request (or injects one synthetic observation) and states the
expected behaviour. Run with:  python3 code/evaluation/main.py --custom
"""
from __future__ import annotations

import copy
import json

from ghostaudit.engine import Case, load_cases
from ghostaudit.ingest import Obs


def _sample(auditor, cid):
    from pathlib import Path
    for c in load_cases(Path(auditor.dataset) / "sample_deletion_requests.csv"):
        if c.case_id == cid:
            return c
    raise KeyError(cid)


def _ops(steps):
    return [(s["phase"], s["system_id"], s["resource_id"], s["operation"]) for s in steps]


def case_negated_deletion(auditor):
    """A request that mentions deletion only to rule it out must not authorize deletion."""
    c = copy.copy(_sample(auditor, "case_24"))
    c.case_id = "custom_01"
    c.request_text = ("Please don't delete anything on my account. I only need a copy of my order history "
                      "for a warranty claim.")
    st, steps, _ = auditor.audit(c)
    return st == "NOT_APPLICABLE" and steps == [], f"got {st} with {len(steps)} steps"


def case_historical_email(auditor):
    """A request sent from a previous email must resolve to the same subject and the same plan."""
    base = _sample(auditor, "case_29")
    c = copy.copy(base)
    c.case_id = "custom_02"
    c.email = "noah.f4380@outlook.com"  # historical address from user_audit_log
    auditor.subjects[c.case_id] = auditor.ids.resolve(c.email)
    auditor.linked[c.case_id] = auditor.linked_resources(c, auditor.subjects[c.case_id])
    auditor.assign[c.case_id] = auditor.assign.get(base.case_id)
    auditor.run_items[c.case_id] = auditor.run_items.get(base.case_id, set())
    st1, s1, _ = auditor.audit(base)
    st2, s2, _ = auditor.audit(c)
    ok = st1 == st2 and _ops(s1) == _ops(s2) and auditor.subjects[c.case_id].user_id == "user_29"
    return ok, f"subject={auditor.subjects[c.case_id].user_id} same_plan={_ops(s1) == _ops(s2)}"


def case_evidence_after_audit(auditor):
    """Evidence captured after audit_at must not be cited, and must not change the answer."""
    base = _sample(auditor, "case_24")
    c = copy.copy(base)
    c.case_id = "custom_03"
    c.audit_at = "2026-09-15T09:00:00Z"  # before evidence_92 (11:44) and evidence_51 (12:00) were captured
    auditor.subjects[c.case_id] = auditor.subjects[base.case_id]
    auditor.linked[c.case_id] = auditor.linked_resources(c, auditor.subjects[c.case_id])
    auditor.assign[c.case_id] = auditor.assign.get(base.case_id)
    auditor.run_items[c.case_id] = set()
    st, steps, _ = auditor.audit(c)
    late = [s for s in steps if s["evidence_id"] in ("evidence_92", "evidence_51", "evidence_02")]
    return not late, f"status={st} late_citations={len(late)}"


def case_replica_drift(auditor):
    """A profile deleted from the primary but still present in a replica is a remaining record."""
    base = _sample(auditor, "case_27")  # COMPLETE in the gold sample
    c = copy.copy(base)
    c.case_id = "custom_04"
    s = auditor.subjects[base.case_id]
    acc = auditor.assign[base.case_id]["resource_id"]
    ghost = Obs("evidence_92", "2026-09-15T11:44:55Z", "2026-09-15T11:40:00Z", "account_service", acc, "FOUND",
                {"email": s.emails[0], "_table": "customer_profiles_replica", "_role": "replica"},
                {s.emails[0].lower()}, "sql:customer_profiles_replica", "custom")
    auditor.by_rid[acc].append(ghost)
    auditor.subjects[c.case_id] = s
    auditor.linked[c.case_id] = auditor.linked[base.case_id]
    auditor.assign[c.case_id] = auditor.assign[base.case_id]
    auditor.run_items[c.case_id] = auditor.run_items.get(base.case_id, set())
    try:
        st, steps, _ = auditor.audit(c)
    finally:
        auditor.by_rid[acc].remove(ghost)
    want = ("FINDING", "account_service", acc, "REPORT_REMAINING_RECORD")
    order_ok = [x[0] for x in _ops(steps)] == sorted([x[0] for x in _ops(steps)],
                                                     key=["FINDING", "REMEDIATION", "VERIFICATION"].index)
    return st == "INCOMPLETE" and want in _ops(steps) and order_ok, f"status={st}"


def case_restore_not_proven_blocked(auditor):
    """A backup holding the subject's data with no proof that restore is blocked is a restore risk."""
    from ghostaudit.policy import assess
    f, r, v = assess("BACKUP_ARCHIVE", True, {"email": "x@example.com"}, auditor.rules)
    f2, r2, v2 = assess("BACKUP_ARCHIVE", True, {"restore_allowed": False}, auditor.rules)
    ok = (f, r, v) == ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED") and f2 is None \
        and v2 == "VERIFY_RESTORE_BLOCKED"
    return ok, f"unknown->{f}, blocked->{f2}/{v2}"


def case_multilingual_intent(auditor):
    """Deletion requests in Korean / Portuguese / Japanese are authorized; an opt-out is not."""
    from ghostaudit.intent import classify
    tests = {
        "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.": "DELETE",
        "Solicito a exclusão permanente da conta associada a este e-mail.": "DELETE",
        "このメールアドレスに関連するアカウントの完全な削除を依頼します。": "DELETE",
        "Please remove me from marketing lists only. Keep my account.": "NON_DELETE",
    }
    got = {t: classify(t)[0] for t in tests}
    return got == tests, json.dumps(list(got.values()))


CASES = [case_negated_deletion, case_historical_email, case_evidence_after_audit, case_replica_drift,
         case_restore_not_proven_blocked, case_multilingual_intent]


def run_all(auditor):
    passed = 0
    for fn in CASES:
        ok, info = fn(auditor)
        passed += bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {fn.__name__:34s} {info}")
    print(f"custom cases: {passed}/{len(CASES)} passed")
    return passed, len(CASES)
