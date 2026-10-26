"""Deletion audit engine.

Pipeline per request:
  1. intent      - does the text authorize deletion? (else NOT_APPLICABLE, [])
  2. identity    - current + historical emails, phones, hashes, stable user_id
  3. discovery   - every evidence observation carrying one of those identifiers,
                   plus the subject's deletion run in system_activity (account and
                   identifier-less records such as anonymized analytics profiles)
  4. state       - per resource, the latest evidence at or before audit_at
                   (workflow results in system_activity are claims, never state)
  5. policy      - deletion_rules.csv category -> required treatment
  6. plan        - findings, remediation and verification in the contract order
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .identity import IdentityIndex, Subject
from .ingest import Obs, load_evidence, system_for, EMAIL_RE, USER_RE
from .intent import classify
from .policy import (Rules, categorize, assess, FINDING_ORDER, REMEDIATION_ORDER, VERIFICATION_ORDER, STATE)


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


ID_MATCH_FIELDS = ("email", "phone", "email_hash", "customer_reference", "subject_reference")

# Deletion-run cadence: the deletion worker processes one subject's downstream records on a
# fixed schedule after the primary profile, so their timestamps share the account's
# second-of-minute phase (within a small set of offsets). Used only to attach records that
# carry no identifier at all (e.g. already-anonymized analytics profiles).
RUN_WINDOW_S = 4 * 3600


@dataclass
class Case:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


class Auditor:
    def __init__(self, dataset: Path, image_facts: dict, link_lookback_days: int = 7):
        self.dataset = dataset
        self.obs, self.files = load_evidence(dataset, image_facts)
        self.ids = IdentityIndex(dataset)
        self.rules = Rules(dataset)
        self.lookback = link_lookback_days * 86400
        self.activities = list(csv.DictReader(open(dataset / "system_activity.csv", encoding="utf-8")))
        self._catalog()
        self.by_rid = defaultdict(list)
        for o in self.obs:
            self.by_rid[o.resource_id].append(o)
        self.act_by_rid = defaultdict(list)
        for a in self.activities:
            self.act_by_rid[a["resource_id"]].append(a)
        self.account_deletes = sorted(
            [a for a in self.activities if a["source_system"] == "deletion_worker"
             and a["target_system"] == "account_service" and a["event_type"] == "DELETE_RECORD"],
            key=lambda a: a["event_time"])
        self.run_offsets = None

    # ------------------------------------------------------------------ catalog links
    def _catalog(self):
        """LINK_RESOURCE_IDENTITY activities map display keys used in screenshots and documents
        to internal resource ids and to the subject identifiers."""
        alias = {}
        for a in self.activities:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = re.search(r"linked (\S+) \(([^)]+)\) to (\S+?) and (user_\d+)", a["details"]) or \
                re.search(r"confirmed (\S+) \(([^)]+)\) belongs only to (\S+?) and (user_\d+)", a["details"])
            if not m:
                continue
            rid, display, email, user = m.groups()
            alias[display] = rid
            # the catalog statement itself is an identity link (not a state claim)
            self.obs.append(Obs(a["activity_id"], a["event_time"], a["event_time"], a["target_system"], rid, None,
                                {"email": email, "customer_reference": user,
                                 "sole_subject": "belongs only" in a["details"]},
                                {email.lower(), user}, "activity:catalog_link", "data_catalog"))
        for o in self.obs:
            if o.resource_id in alias and o.source.startswith("image"):
                o.resource_id = alias[o.resource_id]
                o.system = system_for(o.resource_id) or o.system
        # PDF documents that carry a catalogued display key (statement number, attachment name)
        for fid, f in self.files.items():
            cov = f.get("coverage") or {}
            txt = cov.get("text")
            if not txt:
                continue
            for display, rid in alias.items():
                if display in txt or display.replace(".pdf", "") in txt:
                    users = cov.get("users", [])
                    emails = cov.get("emails", [])
                    self.obs.append(Obs(fid, f["captured_at"], f["captured_at"], system_for(rid) or "", rid, "FOUND",
                                        {"email": emails[0] if emails else None,
                                         "customer_reference": users[0] if users else None,
                                         "record_type": "USER_EXPORT" if rid.startswith("export") else None},
                                        set(users) | set(emails), "pdf:catalogued_document", f["origin"]))
        self.alias = alias

    # ------------------------------------------------------------------ linking
    def subject_matches(self, o: Obs, s: Subject) -> bool:
        if o.keys & s.keys():
            return True
        for k in ID_MATCH_FIELDS:
            if s.matches_value(k, o.fields.get(k)):
                return True
        return False

    def owners(self, rid: str) -> set:
        """user_ids that any evidence ties to this resource (identifier based)."""
        out = set()
        for o in self.by_rid.get(rid, []):
            for k in o.keys:
                if k.startswith("user_"):
                    out.add(k)
                elif "@" in k:
                    out |= self.ids.by_email.get(k, set())
            for k in ("customer_reference", "subject_reference"):
                v = o.fields.get(k)
                if isinstance(v, str) and USER_RE.fullmatch(v):
                    out.add(v)
        return out

    def linked_resources(self, case: Case, s: Subject) -> dict:
        lo = ts(case.requested_at) - self.lookback
        hi = ts(case.audit_at)
        out = defaultdict(list)
        for o in self.obs:
            t = ts(o.t)
            if t > hi or t < lo:
                continue
            if self.subject_matches(o, s):
                out[o.resource_id].append(o)
        return out

    # ------------------------------------------------------------------ deletion runs
    def account_candidates(self, case: Case):
        rep = ts(case.reported_at)
        return [a for a in self.account_deletes if rep - 3 * 3600 <= ts(a["event_time"]) <= rep + 1800
                and ts(a["event_time"]) <= ts(case.audit_at)]

    def _chain_acts(self, rids, exclude_rid=None):
        out = []
        for rid in rids:
            if rid == exclude_rid or rid.startswith("account_"):
                continue
            for a in self.act_by_rid.get(rid, []):
                if a["source_system"] == "deletion_worker":
                    out.append(a)
        return out

    def learn_run_offsets(self, pairs):
        """Learn the phase (seconds mod 30) between an account deletion and the same subject's
        downstream policy actions, from high-confidence (identifier-linked) subject/account pairs.
        The deletion worker emits a subject's downstream actions on a 30-second grid relative to
        its profile deletion; different runs sit on different phases."""
        hist = defaultdict(int)
        by_key = defaultdict(lambda: defaultdict(int))
        for acc_act, rids in pairs:
            ta = ts(acc_act["event_time"])
            for a in self._chain_acts(rids):
                d = ts(a["event_time"]) - ta
                if 0 < d <= RUN_WINDOW_S:
                    hist[int(d) % 30] += 1
                    by_key[(a["event_type"], a["reported_result"])][int(d) % 30] += 1
        self.run_hist_by_key = {k: dict(v) for k, v in by_key.items()}
        self.run_offset_hist = dict(sorted(hist.items()))
        peak = max(hist.values()) if hist else 0
        strong = sorted(k for k, v in hist.items() if peak and v >= 0.1 * peak)
        # group adjacent phases into families (e.g. 9-10 and 20-22)
        fams, cur = [], []
        for k in strong:
            if cur and k - cur[-1] > 1:
                fams.append(cur)
                cur = []
            cur.append(k)
        if cur:
            fams.append(cur)
        self.run_families = [(min(f) - 1, max(f) + 1) for f in fams]
        self.run_offsets = True

    def phase_loglik(self, ta: float, t: float, act: dict | None = None) -> float:
        """log-likelihood ratio of this phase under the learned run cadence vs. chance
        (per action type when enough examples exist, else pooled)."""
        import math
        h = self.run_offset_hist
        if act is not None:
            hk = self.run_hist_by_key.get((act["event_type"], act["reported_result"]))
            if hk and sum(hk.values()) >= 8:
                h = hk
        tot = sum(h.values()) or 1
        p = (h.get(int(t - ta) % 30, 0) + 0.5) / (tot + 15)
        return math.log(p * 30)

    def phase_family(self, ta: float, t: float):
        """Index of the run-phase family that (t - ta) falls in, or None."""
        p = int(t - ta) % 30
        for i, (lo, hi) in enumerate(self.run_families or []):
            if lo <= p <= hi:
                return i
        return None

    def offset_ok(self, ta: float, t: float) -> bool:
        return self.phase_family(ta, t) is not None

    def plan_runs(self, cases: list[Case]):
        """Assign each deletion case its account-deletion activity and the identifier-less
        records processed in its run. Solved globally because runs of different subjects
        interleave in the activity stream."""
        subjects, linked = {}, {}
        for c in cases:
            if classify(c.request_text)[0] == "NON_DELETE":
                continue
            s = self.ids.resolve(c.email)
            if not s:
                continue
            subjects[c.case_id] = s
            linked[c.case_id] = self.linked_resources(c, s)
        case_by_id = {c.case_id: c for c in cases}
        cands = {cid: self.account_candidates(case_by_id[cid]) for cid in subjects}
        owner = {}
        for cid in subjects:
            for a in cands[cid]:
                owner.setdefault(a["resource_id"], self.owners(a["resource_id"]))
        # 1. learn the run cadence from accounts whose owner is proven by identifiers
        pairs = []
        for cid, s in subjects.items():
            for a in cands[cid]:
                if s.user_id in owner[a["resource_id"]]:
                    pairs.append((a, list(linked[cid])))
        if self.run_offsets is None:
            self.learn_run_offsets(pairs)
        # 2. score every (case, account-deletion) pair
        scored = []
        for cid, s in subjects.items():
            chain = self._chain_acts(linked[cid])
            for a in cands[cid]:
                own = owner[a["resource_id"]]
                if own and s.user_id not in own:
                    continue
                ta = ts(a["event_time"])
                sc = 100.0 if s.user_id in own else 0.0
                for x in chain:
                    d = ts(x["event_time"]) - ta
                    if 0 < d <= RUN_WINDOW_S:
                        sc += self.phase_loglik(ta, ts(x["event_time"]), x)
                    elif -RUN_WINDOW_S < d <= 0:
                        sc -= 1
                # weak support: identifier-less downstream actions in phase with this account
                fams = {self.phase_family(ta, ts(x["event_time"])) for x in chain
                        if 0 < ts(x["event_time"]) - ta <= RUN_WINDOW_S}
                for x in self.activities_after(ta):
                    f = self.phase_family(ta, ts(x["event_time"]))
                    if f is not None and not self.owners(x["resource_id"]) and (not fams or f in fams):
                        sc += 0.25
                scored.append((sc, -abs(ts(case_by_id[cid].reported_at) - ta), cid, a))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        assign, used = {}, set()
        for sc, _, cid, a in scored:
            if cid in assign or a["activity_id"] in used:
                continue
            assign[cid] = a
            used.add(a["activity_id"])
        # 3. identifier-less downstream records claimed by run cadence (closest phase wins)
        claims = defaultdict(list)
        for cid, acc in assign.items():
            ta = ts(acc["event_time"])
            fams = {self.phase_family(ta, ts(x["event_time"])) for x in self._chain_acts(linked[cid])
                    if 0 < ts(x["event_time"]) - ta <= RUN_WINDOW_S} - {None}
            for x in self.activities_after(ta):
                rid = x["resource_id"]
                f = self.phase_family(ta, ts(x["event_time"]))
                if self.owners(rid) or f is None:
                    continue
                conflict = 1 if (fams and f not in fams) else 0
                claims[rid].append((conflict, ts(x["event_time"]) - ta, cid))
        run_items = defaultdict(set)
        for rid, cl in claims.items():
            cl.sort()
            run_items[cl[0][2]].add(rid)
        self.subjects, self.linked, self.assign, self.run_items = subjects, linked, assign, run_items

    def activities_after(self, ta: float):
        if not hasattr(self, "_dw"):
            self._dw = sorted([(ts(a["event_time"]), a) for a in self.activities
                               if a["source_system"] == "deletion_worker" and a["target_system"] != "account_service"],
                              key=lambda x: x[0])
            self._dw_t = [t for t, _ in self._dw]
        import bisect
        i = bisect.bisect_right(self._dw_t, ta)
        out = []
        while i < len(self._dw) and self._dw[i][0] - ta <= RUN_WINDOW_S:
            out.append(self._dw[i][1])
            i += 1
        return out

    # ------------------------------------------------------------------ state
    def state_at(self, rid: str, audit_at: str, extra: list | None = None):
        """Latest citable observation (file captured <= audit_at, observed <= audit_at)."""
        cut = ts(audit_at)
        obs = [o for o in self.by_rid.get(rid, []) + (extra or [])
               if o.status is not None and ts(o.t) <= cut and ts(o.captured_at) <= cut
               and o.file_id.startswith("evidence_")]
        if not obs:
            return None
        latest_t = max(ts(o.t) for o in obs)
        # observations in the same file at the same moment describe the same instant (primary + replicas)
        top = [o for o in obs if ts(o.t) >= latest_t - 1]
        found = [o for o in top if o.status == "FOUND"]
        chosen = found[0] if found else top[0]
        merged = {}
        for o in (found or top):
            for k, v in o.fields.items():
                if v is not None and merged.get(k) is None:
                    merged[k] = v
                merged.setdefault(k, v)
        return {"present": chosen.status == "FOUND", "fields": merged, "file_id": chosen.file_id,
                "source": chosen.source, "obs": top}

    def coverage_file(self, system: str, audit_at: str):
        """Latest citable snapshot that covers a system (used to evidence absence)."""
        cut = ts(audit_at)
        best = None
        for fid, f in self.files.items():
            if ts(f["captured_at"]) > cut:
                continue
            cov = f.get("coverage") or {}
            systems = set(cov.get("systems", []))
            for tb, info in (cov.get("tables") or {}).items():
                if info.get("system"):
                    systems.add(info["system"])
            systems |= set((cov.get("scope") or {}).keys())
            if system in systems and (best is None or f["captured_at"] > best[1]):
                best = (fid, f["captured_at"])
        return best[0] if best else None

    # ------------------------------------------------------------------ audit
    def audit(self, case: Case):
        label, why = classify(case.request_text)
        if label == "AMBIGUOUS":
            from .llm import adjudicate
            llm_label = adjudicate(case.request_text)
            # without a model answer a deletion workflow request is treated as a deletion request
            label, why = (llm_label or "DELETE"), f"ambiguous text; llm={llm_label}"
        trace = {"intent": label, "intent_reason": why}
        if label == "NON_DELETE":
            return "NOT_APPLICABLE", [], trace
        s = self.subjects.get(case.case_id) or self.ids.resolve(case.email)
        if s is None:
            trace["error"] = "subject not resolved"
            return "INCOMPLETE", [], trace
        trace["subject"] = s.user_id
        linked = self.linked.get(case.case_id, {})
        resources = {}
        catalogued = set(self.alias.values())
        for rid, obs in linked.items():
            # a screenshot shows a whole screen; a record seen only on a screenshot is in scope
            # when the data catalog ties it to the subject or other evidence corroborates it
            if all(o.source.startswith("image") for o in obs) and rid not in catalogued:
                continue
            sysid = next((o.system for o in obs if o.system), None) or system_for(rid)
            resources[rid] = sysid
        acc = self.assign.get(case.case_id)
        accounts = [r for r in resources if r.startswith("account_")]
        if acc:
            for r in accounts:
                if r != acc["resource_id"]:
                    resources.pop(r)
            resources[acc["resource_id"]] = "account_service"
        elif len(accounts) > 1:
            keep = max(accounts, key=lambda r: max(o.t for o in linked[r]))
            for r in accounts:
                if r != keep:
                    resources.pop(r)
        for rid in self.run_items.get(case.case_id, set()):
            resources.setdefault(rid, system_for(rid))
        trace["account_activity"] = acc["activity_id"] if acc else None

        findings, remediation, verification = [], [], []
        decisions = []
        for rid, sysid in sorted(resources.items()):
            if not sysid or not re.fullmatch(r"[a-z_]+_\d+", rid):
                continue
            st = self.state_at(rid, case.audit_at)
            fields = st["fields"] if st else {}
            present = st["present"] if st else None
            src = st["source"] if st else ""
            cat = categorize(sysid, rid, fields, src)
            if cat == "UNKNOWN":
                continue
            if present is None:
                # no citable evidence of the record at audit time
                if cat in ("USER_PROFILE",):
                    present = False
                elif cat == "SECURITY_AUDIT_LOG":
                    present = True
                else:
                    continue
            f_op, r_op, v_op = assess(cat, present, fields, self.rules)
            if v_op is None:
                continue
            ev = st["file_id"] if st else ""
            if not ev and v_op == "VERIFY_RECORD_ABSENT":
                ev = self.coverage_file(sysid, case.audit_at) or ""
            decisions.append({"resource_id": rid, "system_id": sysid, "category": cat, "present": present,
                              "finding": f_op, "remediation": r_op, "verification": v_op, "evidence": ev})
            if f_op:
                findings.append((sysid, rid, f_op, ev))
                remediation.append((sysid, rid, r_op, ""))
            verification.append((sysid, rid, v_op, ev))
        trace["decisions"] = decisions
        status = "INCOMPLETE" if findings else "COMPLETE"
        steps = []

        def emit(phase, items, order):
            for sysid, rid, op, ev in sorted(items, key=lambda x: (order.index(x[2]), x[0], x[1])):
                steps.append({"step_no": len(steps) + 1, "phase": phase, "system_id": sysid, "resource_id": rid,
                              "operation": op, "state": STATE[op],
                              "evidence_id": ev if status == "COMPLETE" or phase == "FINDING" else ""})
        if status == "INCOMPLETE":
            emit("FINDING", findings, FINDING_ORDER)
            emit("REMEDIATION", remediation, REMEDIATION_ORDER)
        emit("VERIFICATION", verification, VERIFICATION_ORDER)
        return status, steps, trace


def load_cases(path: Path) -> list[Case]:
    return [Case(**r) for r in csv.DictReader(open(path, encoding="utf-8"))]
