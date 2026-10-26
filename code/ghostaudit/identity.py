"""Identity resolution: map a request email to a subject and all of its identifiers."""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


def norm_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")


def email_hash(e: str) -> str:
    return hashlib.sha256(e.strip().lower().encode()).hexdigest()


@dataclass
class Subject:
    user_id: str
    name: str
    emails: list = field(default_factory=list)       # current first, then historical
    phones: list = field(default_factory=list)
    hashes: list = field(default_factory=list)
    names: list = field(default_factory=list)
    history: list = field(default_factory=list)      # audit rows that touched identifiers

    def keys(self) -> set:
        ks = {self.user_id}
        ks |= {e.lower() for e in self.emails}
        ks |= set(self.hashes)
        return ks

    def matches_value(self, field_name: str, value) -> bool:
        if value is None:
            return False
        v = str(value).strip()
        if field_name in ("email",):
            return v.lower() in {e.lower() for e in self.emails}
        if field_name in ("phone",):
            return norm_phone(v) in {norm_phone(p) for p in self.phones} and len(norm_phone(v)) > 5
        if field_name == "email_hash":
            return v in self.hashes
        if field_name == "name":
            return v.lower() in {n.lower() for n in self.names}
        if field_name in ("customer_reference", "subject_reference"):
            return v == self.user_id
        return False


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.users = {r["user_id"]: r for r in csv.DictReader(open(dataset / "user_identities.csv", encoding="utf-8"))}
        self.audit = list(csv.DictReader(open(dataset / "user_audit_log.csv", encoding="utf-8")))
        self.by_email: dict[str, set] = {}
        for u in self.users.values():
            self.by_email.setdefault(u["email"].lower(), set()).add(u["user_id"])
        for a in self.audit:
            if a["field_name"] == "email":
                for v in (a["old_value"], a["new_value"]):
                    if v:
                        self.by_email.setdefault(v.lower(), set()).add(a["user_id"])

    def resolve(self, email: str, as_of: str | None = None) -> Subject | None:
        uids = self.by_email.get(email.strip().lower(), set())
        if not uids:
            return None
        # deterministic: prefer the user whose current email matches, then lowest id
        uid = sorted(uids, key=lambda u: (self.users.get(u, {}).get("email", "").lower() != email.lower(),
                                          int(u.split("_")[1])))[0]
        u = self.users.get(uid)
        if u is None:
            return None
        s = Subject(uid, u["name"], [u["email"]], [u["phone"]], [u["email_hash"]], [u["name"]])
        for a in sorted(self.audit, key=lambda r: r["event_time"]):
            if a["user_id"] != uid:
                continue
            s.history.append(a)
            for v in (a["old_value"], a["new_value"]):
                if not v:
                    continue
                if a["field_name"] == "email" and v not in s.emails:
                    s.emails.append(v)
                elif a["field_name"] == "phone" and v not in s.phones:
                    s.phones.append(v)
                elif a["field_name"] == "email_hash" and v not in s.hashes:
                    s.hashes.append(v)
                elif a["field_name"] in ("name", "full_name") and v not in s.names:
                    s.names.append(v)
        for e in s.emails:
            h = email_hash(e)
            if h not in s.hashes:
                s.hashes.append(h)
        return s
