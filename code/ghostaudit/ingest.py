"""Evidence ingestion: turn every supplied evidence file into normalized observations.

An observation is one statement about one resource seen in one evidence file:

    Obs(file_id, captured_at, t, system, resource_id, status, fields, keys, source)

* ``t``        - when the statement was observed (record time when the file has one,
                 otherwise the file capture time)
* ``status``   - FOUND / NOT_FOUND / None (unknown)
* ``fields``   - the raw attribute values relevant to deletion policy
                 (email, phone, name, email_hash, customer_reference, enabled, ...)
* ``keys``     - every identifier string attached to the observation (lookup keys,
                 correlation keys, emails, user refs, hashes) used for identity linking

Parsers are format driven (SQL, JSON, logs, text, PDF, screenshots) and never look at
case ids or expected answers.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RESOURCE_PREFIX_SYSTEM = [
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("data_flow_", "integration_service"),
    ("backup_", "backup_service"),
    ("audit_record_", "security_service"),
    ("tombstone_", "identity_service"),
    ("marketing_", "marketing_platform"),
    ("cache_", "cache_service"),
    ("email_delivery_", "email_delivery_service"),
    ("legal_hold_", "legal_service"),
    ("telemetry_", "telemetry_service"),
    ("object_", "object_storage"),
]

RID_RE = re.compile(r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|marketing|cache|email_delivery|legal_hold|telemetry|object)_\d+\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")


def system_for(resource_id: str) -> str | None:
    for p, s in RESOURCE_PREFIX_SYSTEM:
        if resource_id.startswith(p):
            return s
    return None


@dataclass
class Obs:
    file_id: str
    captured_at: str
    t: str
    system: str
    resource_id: str
    status: str | None
    fields: dict = field(default_factory=dict)
    keys: set = field(default_factory=set)
    source: str = ""          # table / collection / log event: provenance hint
    origin: str = ""          # evidence origin from the manifest

    def to_json(self):
        d = self.__dict__.copy()
        d["keys"] = sorted(self.keys)
        return d


NULLS = {None, "", "null", "None", "NULL", "-", "—", "no direct identifier"}


def clean(v):
    if isinstance(v, str):
        v = v.strip()
    return None if v in NULLS else v


def keys_from_value(v) -> set:
    out = set()
    if v is None:
        return out
    if isinstance(v, (dict, list)):
        for x in (v.values() if isinstance(v, dict) else v):
            out |= keys_from_value(x)
        return out
    s = str(v).strip()
    if not s or s in NULLS:
        return out
    for e in EMAIL_RE.findall(s):
        out.add(e.lower())
    for u in USER_RE.findall(s):
        out.add(u)
    if re.fullmatch(r"[0-9a-f]{64}", s):
        out.add(s)
    return out


ID_FIELDS = ("email", "requester_email", "owner_email", "customer_email", "subject_reference",
             "customer_reference", "user_id", "subject_ref", "subject_key", "lookup_key", "match_key",
             "scan_key", "correlation_key", "matched_value", "email_hash", "phone", "owner_phone",
             "requester_phone", "full_name", "display_name", "requester_name", "name", "selector", "account",
             "lookup_value", "subject", "subject_selector", "uploaded_by", "message_text")


NESTED = ("document", "profile", "customer", "subject", "owner", "requester", "contact")


def flatten(rec: dict) -> dict:
    """Lift identifier-bearing nested objects (e.g. search ``document``) to the top level."""
    out = dict(rec)
    for k in NESTED:
        v = rec.get(k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                if kk not in out or out[kk] in NULLS:
                    out[kk] = vv
    return out


def normalize_fields(rec: dict) -> dict:
    """Map heterogeneous attribute names onto one vocabulary."""
    rec = flatten(rec)
    f = {}
    alias = {
        "email": "email", "requester_email": "email", "owner_email": "email", "customer_email": "email",
        "matched_value": "email", "subject_selector": "email", "uploaded_by": "email",
        "phone": "phone", "owner_phone": "phone", "requester_phone": "phone",
        "full_name": "name", "display_name": "name", "requester_name": "name", "name": "name",
        "email_hash": "email_hash",
        "customer_reference": "customer_reference", "user_id": "customer_reference",
        "subject_reference": "subject_reference",
    }
    txt = rec.get("message_text")
    if isinstance(txt, str):
        found = EMAIL_RE.findall(txt)
        f["message_has_identifier"] = bool(found)
        if found and not clean(rec.get("email")):
            f["email"] = found[0]
    for k, v in rec.items():
        if k in alias:
            v = clean(v)
            tgt = alias[k]
            if v is not None or tgt not in f:
                f[tgt] = v if v is not None else f.get(tgt)
        elif k in ("enabled", "restore_allowed", "archive_present", "download_available", "marker_found",
                   "eligible", "deletion_block", "email_opt_in"):
            f[k] = _bool(v)
        elif k in ("account_status", "result", "record_type", "event_code", "retention_until", "event",
                   "connector_name", "last_delivery_status", "message_excerpt", "decision", "status_code",
                   "http_status", "rows", "total_hits", "direct_identifiers", "file", "file_name",
                   "object_key", "archive_key", "index", "index_name", "source", "event_name", "detector",
                   "source_resource_id", "shared_context", "note", "expires_at", "collection_name",
                   "attachment_name", "conversation_scope", "legal_hold", "hold_status", "linked_subjects",
                   "retained_until", "retention_reason", "storage_class", "ticket_id", "available", "reason", "access"):
            f[k] = clean(v) if not isinstance(v, (list, dict)) else v
    return f


def _bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return None


LOOKUP_STATES = {"FOUND": "FOUND", "NOT_FOUND": "NOT_FOUND", "IDENTIFIABLE": "FOUND", "ANONYMIZED": "FOUND",
                 "ACTIVE": "FOUND", "DISABLED": "FOUND", "BLOCKED": "FOUND", "REDACTED": "FOUND"}


def interpret_lookup(system: str, result: str, ident, fields: dict) -> str | None:
    """Record-lookup style evidence ("system | resource | TYPE | RESULT | identifier") reports a
    state word instead of raw fields. Translate that word into the same field vocabulary."""
    result = (result or "").upper()
    ident = clean(ident)
    fields["lookup_result"] = result
    if result == "IDENTIFIABLE" or (result in ("FOUND", "ACTIVE") and ident and "@" in str(ident)):
        if ident and "@" in str(ident):
            fields["email"] = ident
        else:
            fields["identifiable"] = True
    if result in ("ANONYMIZED", "REDACTED"):
        for k in ("email", "phone", "name", "email_hash"):
            fields[k] = None
    if ident and USER_RE.fullmatch(str(ident)):
        fields["customer_reference"] = ident
    if system == "integration_service" and result in ("ACTIVE", "DISABLED"):
        fields["enabled"] = result == "ACTIVE"
    if system == "backup_service" and result in ("ACTIVE", "BLOCKED"):
        fields["restore_allowed"] = result == "ACTIVE"
    return LOOKUP_STATES.get(result)


# --------------------------------------------------------------------------- SQL
SQL_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def parse_sql(path: Path, file_id, captured_at, origin):
    con = sqlite3.connect(":memory:")
    txt = path.read_text(encoding="utf-8")
    con.executescript(txt)
    m = re.search(r"Captured at:\s*(\S+)", txt) or re.search(r"snapshot_completed_at=(\S+)", txt)
    cap = m.group(1) if m else captured_at
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r[0]: r[1] for r in con.execute("select origin_system, captured_at from snapshot_scope")}
    obs = []
    table_info = {}
    for tb in tables:
        if tb == "snapshot_scope":
            continue
        cols = [r[1] for r in con.execute(f'pragma table_info("{tb}")')]
        rows = con.execute(f'select * from "{tb}"').fetchall()
        sys_default = None
        for prefix, s in SQL_TABLE_SYSTEM.items():
            if tb.startswith(prefix):
                sys_default = s
        role = "primary"
        low = tb.lower()
        if "replica" in low:
            role = "replica"
        table_info[tb] = {"role": role, "system": sys_default, "rows": len(rows)}
        for row in rows:
            rec = dict(zip(cols, row))
            if "record_json" in rec:
                inner = json.loads(rec["record_json"])
                rec = {**{k: v for k, v in rec.items() if k != "record_json"}, **inner}
            rid = rec.get("resource_id")
            system = rec.get("origin_system") or sys_default or system_for(rid)
            t = rec.get("updated_at") or rec.get("indexed_at") or cap
            rec = flatten(rec)
            fields = normalize_fields(rec)
            fields["_table"] = tb
            fields["_role"] = role
            keys = set()
            for k in ID_FIELDS:
                keys |= keys_from_value(rec.get(k))
            obs.append(Obs(file_id, captured_at, t if t <= cap else cap, system, rid, "FOUND", fields, keys,
                           f"sql:{tb}", origin))
    # coverage: which systems / tables this snapshot covers (absence by omission)
    coverage = {"tables": table_info, "scope": scope, "captured": cap}
    return obs, coverage


# --------------------------------------------------------------------------- JSON
def parse_json(path: Path, file_id, captured_at, origin):
    d = json.loads(path.read_text(encoding="utf-8"))
    obs = []
    coverage = {"systems": set()}
    file_time = d.get("exported_at") or d.get("captured_at") or captured_at

    def add(rec, system, t, status, source):
        rid = rec.get("resource_id")
        if not rid:
            return
        system = system or rec.get("system") or system_for(rid)
        flat = flatten(rec)
        fields = normalize_fields(flat)
        keys = set()
        for k in ID_FIELDS:
            keys |= keys_from_value(flat.get(k))
        if "query" in rec:
            keys |= keys_from_value(rec["query"])
        if source == "json:record_lookup":
            ident = rec.get("subject_reference") or rec.get("lookup_value")
            status = interpret_lookup(system, rec.get("result"), ident, fields) or status
        if fields.get("download_available") is False:
            status = "NOT_FOUND"
        obs.append(Obs(file_id, captured_at, t or file_time, system, rid, status, fields, keys, source, origin))

    if "service_exports" in d:
        for system, colls in d["service_exports"].items():
            coverage["systems"].add(system)
            for coll, recs in colls.items():
                for r in recs:
                    t = r.get("observed_at")
                    if "status_code" in r:
                        st = "FOUND" if r["status_code"] == 200 else "NOT_FOUND"
                    elif "hits" in r:
                        docs = r["hits"].get("documents", [])
                        if not docs:
                            add({**r}, system, t, "NOT_FOUND", f"json:{coll}")
                            continue
                        for doc in docs:
                            add({**doc, "lookup_key": r.get("query")}, system, t, "FOUND", f"json:{coll}")
                        continue
                    else:
                        st = "FOUND"
                    add(r, system, t, st, f"json:{coll}")
    elif "snapshots" in d:
        for snap in d["snapshots"]:
            system = snap.get("data_store")
            coverage["systems"].add(system)
            for r in snap.get("records", []):
                if "result" in r:
                    st = "NOT_FOUND" if r["result"] == "NOT_FOUND" else "FOUND"
                    add(r, r.get("system"), r.get("recorded_at"), st, "json:record_lookup")
                else:
                    add(r, system, snap.get("captured_at"), "FOUND", f"json:{snap.get('collection')}")
    elif "records" in d:
        for r in d["records"]:
            if "result" in r:
                st = {"FOUND": "FOUND", "NOT_FOUND": "NOT_FOUND"}.get(r["result"], "FOUND")
                add(r, r.get("system"), r.get("recorded_at"), st, "json:record_lookup")
            else:
                coll = d.get("collection", "records")
                add(r, None, d.get("captured_at"), "FOUND", f"json:{coll}")
                if r.get("resource_id"):
                    coverage["systems"].add(system_for(r["resource_id"]))
    coverage["systems"] = sorted(s for s in coverage["systems"] if s)
    return obs, coverage


# --------------------------------------------------------------------------- logs
KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")


def _unq(v: str):
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"')
    return v


def parse_log_line(line: str):
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    rec = {"_ts": ts}
    j = rest.find("{")
    if rest.startswith("{") or (j >= 0 and rest.rstrip().endswith("}") and '":' in rest):
        try:
            payload = json.loads(rest[j:])
            rec.update({k: str(v) if not isinstance(v, str) else v for k, v in payload.items()})
            rec.setdefault("service", payload.get("logger"))
            return rec
        except Exception:
            pass
    # [LEVEL] [service] [trace] event: kv
    mb = re.match(r"\[(\w+)\]\s+\[([\w.]+)\]\s+\[([\w-]+)\]\s+(\w+):\s*(.*)$", rest)
    if mb:
        rec.update(level=mb.group(1), service=mb.group(2), trace_id=mb.group(3), event=mb.group(4))
        rest = mb.group(5)
    # | LEVEL | service | trace | event | kv
    mp = re.match(r"\|\s*(\w+)\s*\|\s*([\w.]+)\s*\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|\s*(.*)$", rest)
    if mp:
        rec.update(level=mp.group(1), service=mp.group(2), trace_id=mp.group(3), event=mp.group(4))
        rest = mp.group(5)
    # <15>1 node service trace - - kv
    ms = re.match(r"<\d+>\d+\s+\S+\s+([\w.]+)\s+([\w-]+)\s+-\s+-\s+(.*)$", rest)
    if ms:
        rec.update(service=ms.group(1), trace_id=ms.group(2))
        rest = ms.group(3)
    for k, v in KV_RE.findall(rest):
        rec[k] = _unq(v)
    if "message" in rec and "=" in rec["message"]:
        for k, v in KV_RE.findall(rec["message"]):
            rec[k] = _unq(v)
    if "requestId" in rec:
        rec.setdefault("trace_id", rec["requestId"])
    if "function" in rec:
        rec.setdefault("service", rec["function"])
    return rec


def log_status(rec: dict):
    """Interpret one completed log record into FOUND/NOT_FOUND/None."""
    if str(rec.get("event", "")).endswith("_started"):
        return None
    if rec.get("result") in ("FOUND", "NOT_FOUND"):
        return rec["result"]
    hs = rec.get("http_status")
    if hs:
        return "FOUND" if hs.startswith("2") else "NOT_FOUND"
    if "rows" in rec:
        try:
            return "FOUND" if int(rec["rows"]) > 0 else "NOT_FOUND"
        except ValueError:
            pass
    if rec.get("result") in ("hit",):
        return "FOUND"
    if rec.get("result") in ("miss", "no_hit") and rec.get("event", "").startswith("index_query"):
        return "NOT_FOUND"
    if "total_hits" in rec:
        return "FOUND" if rec["total_hits"] not in ("0",) else "NOT_FOUND"
    return "FOUND" if rec.get("resource_id") else None


def parse_log(path: Path, file_id, captured_at, origin):
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = parse_log_line(line)
        if r:
            recs.append(r)
    # merge records sharing a trace id: the "started" line carries the lookup key,
    # the "completed" line carries the resource and its state
    trace_keys: dict[str, set] = {}
    for r in recs:
        tid = r.get("trace_id")
        if not tid:
            continue
        ks = set()
        for k in ID_FIELDS:
            ks |= keys_from_value(r.get(k))
        trace_keys.setdefault(tid, set()).update(ks)
    obs = []
    for r in recs:
        rid = r.get("resource_id")
        if not rid or not RID_RE.fullmatch(rid):
            continue
        if r.get("event") in ("thumbnail_failed",):
            continue
        system = r.get("system") or system_for(rid)
        keys = set()
        for k in ID_FIELDS:
            keys |= keys_from_value(r.get(k))
        if r.get("trace_id"):
            keys |= trace_keys.get(r["trace_id"], set())
        fields = normalize_fields(r)
        fields["_event"] = r.get("event")
        st = log_status(r)
        if r.get("event") == "record_lookup":
            st = interpret_lookup(system, r.get("result"), r.get("subject_reference"), fields) or st
        if str(r.get("available", "")).lower() == "false":
            st = "NOT_FOUND"
        if r.get("message_text") == "[REDACTED]":
            for k in ("email", "phone", "name"):
                fields.setdefault(k, None)
        obs.append(Obs(file_id, captured_at, r["_ts"], system, rid, st, fields, keys,
                       f"log:{r.get('event')}", origin))
    return obs, {"systems": []}


# --------------------------------------------------------------------------- text handover
def parse_txt(path: Path, file_id, captured_at, origin):
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.*)$", line.strip())
        if not m:
            continue
        ts, system, rid, rtype, status, ident = m.groups()
        fields = {"record_type": rtype}
        keys = keys_from_value(clean(ident))
        st = interpret_lookup(system, status, ident, fields)
        obs.append(Obs(file_id, captured_at, ts, system, rid, st, fields, keys,
                       "txt:handover", origin))
    return obs, {"systems": []}


# --------------------------------------------------------------------------- PDF documents
def pdf_text(path: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                              timeout=60).stdout
    except Exception:
        try:
            from pypdf import PdfReader  # optional fallback
            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except Exception:
            return ""


def parse_pdf(path: Path, file_id, captured_at, origin):
    txt = pdf_text(path)
    obs = []
    users = sorted(set(USER_RE.findall(txt)))
    emails = sorted(set(e.lower() for e in EMAIL_RE.findall(txt)))
    rids = sorted(set(RID_RE.findall(txt)))
    # register style documents (one row per subject): "ORD-x | user_08 | Card dispute | ... | Retain until ..."
    for line in txt.splitlines():
        if "|" in line and USER_RE.search(line) and "Retain until" in line:
            u = USER_RE.search(line).group(0)
            m = re.search(r"Retain until (\d{2} \w{3} \d{4})", line)
            order = re.search(r"ORD-\d+", line)
            obs.append(Obs(file_id, captured_at, captured_at, "legal_service", f"legal_register:{u}", "FOUND",
                           {"customer_reference": u, "retain_until": m.group(1) if m else None,
                            "order": order.group(0) if order else None, "legal_hold": "ACTIVE"},
                           {u}, "pdf:legal_register", origin))
    for rid in rids:
        f = {"name": None}
        if emails:
            f["email"] = emails[0]
        if users:
            f["customer_reference"] = users[0]
        if rid.startswith("audit_record_"):
            m = re.search(r"subject reference\s+(user_\d+)", txt)
            m2 = re.search(r"retain until\s+(\d{2} \w{3} \d{4})", txt)
            f = {"subject_reference": m.group(1) if m else None, "retention_until": m2.group(1) if m2 else None}
        obs.append(Obs(file_id, captured_at, captured_at, system_for(rid), rid, "FOUND", f,
                       set(users) | set(emails), "pdf:document", origin))
    # documents without an internal resource id (statements, attachments) are kept as text facts
    statement = re.search(r"Statement\s+(NST-[\d-]+)", txt)
    return obs, {"systems": [], "text": txt, "users": users, "emails": emails,
                 "statement": statement.group(1) if statement else None}


# --------------------------------------------------------------------------- images
def parse_image(rel: str, file_id, captured_at, origin, image_facts: dict):
    obs = []
    for r in image_facts.get(rel, []):
        rid = r["resource_id"]
        rec = {k: v for k, v in r.items() if k not in ("system", "resource_id", "status")}
        fields = normalize_fields(rec)
        if "shared_context" in r:
            fields["shared_context"] = r["shared_context"]
        keys = set()
        for k in ID_FIELDS:
            keys |= keys_from_value(rec.get(k))
        obs.append(Obs(file_id, captured_at, captured_at, r["system"], rid, r.get("status", "FOUND"), fields, keys,
                       "image:vision", origin))
    return obs, {"systems": []}


# --------------------------------------------------------------------------- driver
def load_evidence(dataset: Path, image_facts: dict):
    manifest = list(csv.DictReader(open(dataset / "evidence_files.csv", encoding="utf-8")))
    all_obs, files = [], {}
    for row in manifest:
        fid, rel, cap, origin = row["file_id"], row["file_path"], row["captured_at"], row["origin"]
        p = dataset / rel
        ext = p.suffix.lower()
        if not p.exists():
            continue
        if ext == ".sql":
            obs, cov = parse_sql(p, fid, cap, origin)
        elif ext == ".json":
            obs, cov = parse_json(p, fid, cap, origin)
        elif ext == ".log":
            obs, cov = parse_log(p, fid, cap, origin)
        elif ext == ".txt":
            obs, cov = parse_txt(p, fid, cap, origin)
        elif ext == ".pdf":
            obs, cov = parse_pdf(p, fid, cap, origin)
        elif ext in (".png", ".jpg", ".jpeg"):
            obs, cov = parse_image(rel, fid, cap, origin, image_facts)
        else:
            continue
        files[fid] = {"path": rel, "captured_at": cap, "origin": origin, "kind": ext[1:], "coverage": cov}
        all_obs.extend(obs)
    return all_obs, files
