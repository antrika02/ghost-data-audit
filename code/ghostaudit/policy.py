"""Policy: map an observed resource to a data category, its required treatment and its
current compliance state, using deletion_rules.csv as the source of truth for actions."""
from __future__ import annotations

import csv
from pathlib import Path

DIRECT_ID_FIELDS = ("email", "phone", "name", "email_hash")

# operation tables in the order defined by the problem statement
FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}


class Rules:
    def __init__(self, dataset: Path):
        self.rows = {r["data_category"]: r for r in csv.DictReader(open(dataset / "deletion_rules.csv", encoding="utf-8"))}

    def action(self, category: str) -> str | None:
        r = self.rows.get(category)
        return r["required_action"] if r else None


def has_direct_ids(fields: dict) -> bool:
    if fields.get("identifiable"):
        return True
    try:
        if int(fields.get("direct_identifiers") or 0) > 0:
            return True
    except ValueError:
        pass
    return any(fields.get(k) for k in DIRECT_ID_FIELDS)


def categorize(system: str, resource_id: str, fields: dict, source: str) -> str:
    """Decide which deletion_rules category a record belongs to."""
    rt = (fields.get("record_type") or "").upper()
    if rt in ("SUPPORT_ATTACHMENT", "SUPPORT_MESSAGE", "SEARCH_INDEX_ENTRY", "USER_PROFILE", "ANALYTICS_PROFILE",
              "USER_EXPORT", "BACKUP_ARCHIVE", "SECURITY_AUDIT_LOG", "MARKETING_PROFILE", "ACCOUNT_TOMBSTONE",
              "SECONDARY_IDENTIFIER", "CACHE_ENTRY", "EMAIL_DELIVERY_LOG", "PRODUCT_TELEMETRY", "LEGAL_HOLD_RECORD"):
        return rt
    if system == "account_service":
        return "USER_PROFILE"
    if system == "search_service":
        return "SEARCH_INDEX_ENTRY"
    if system == "analytics_platform":
        ev = str(fields.get("_event") or "")
        # a contact/opt-in record keyed by a hashed identifier is a secondary identifier, not a profile
        if "contact" in ev or fields.get("email_opt_in") is not None:
            return "SECONDARY_IDENTIFIER"
        return "ANALYTICS_PROFILE"
    if system == "support_platform":
        src = source.lower()
        ev = str(fields.get("_event") or "")
        if "attachment" in src or "attachment" in ev or fields.get("file") or fields.get("attachment_name") \
                or str(fields.get("file_name") or "").endswith(".pdf") or source.startswith("pdf:"):
            return "SUPPORT_ATTACHMENT"
        if "contact_query" in ev or "legacy" in str(fields.get("source") or ""):
            return "SECONDARY_IDENTIFIER"
        return "SUPPORT_MESSAGE"
    if system == "export_service":
        return "USER_EXPORT"
    if system == "integration_service":
        return "DATA_FLOW"
    if system == "backup_service":
        return "BACKUP_ARCHIVE"
    if system == "security_service":
        return "SECURITY_AUDIT_LOG"
    if system == "identity_service":
        return "ACCOUNT_TOMBSTONE"
    if system == "object_storage":
        # catalogued support-attachment objects that belong only to the subject
        return "SUPPORT_ATTACHMENT"
    if system == "marketing_platform":
        return "MARKETING_PROFILE"
    if system == "cache_service":
        return "CACHE_ENTRY"
    if system == "email_delivery_service":
        return "EMAIL_DELIVERY_LOG"
    if system == "legal_service":
        return "LEGAL_HOLD_RECORD"
    return "UNKNOWN"


def assess(category: str, present: bool, fields: dict, rules: Rules):
    """Return (finding_op, remediation_op, verification_op) for one record's state.

    finding_op/remediation_op are None when the record already complies.
    """
    action = rules.action(category) if category not in ("DATA_FLOW",) else "DISABLE"
    if category == "DATA_FLOW":
        if present and fields.get("enabled"):
            return "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"
        return None, None, "VERIFY_DATA_FLOW_DISABLED"
    if category == "BACKUP_ARCHIVE":
        blocked = fields.get("restore_allowed") is False or fields.get("deletion_block") is True \
            or str(fields.get("decision") or "").lower() == "deny"
        if present and not blocked:
            return "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"
        return None, None, "VERIFY_RESTORE_BLOCKED"
    if action == "RETAIN":
        return None, None, "VERIFY_RECORD_PRESENT"
    if action == "DELETE":
        if present:
            return "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"
        return None, None, "VERIFY_RECORD_ABSENT"
    if action == "REDACT":
        if present and has_direct_ids(fields):
            return "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"
        if not present:
            return None, None, "VERIFY_RECORD_ABSENT"
        return None, None, "VERIFY_RECORD_REDACTED"
    if action == "ANONYMIZE":
        if present and (has_direct_ids(fields) or fields.get("customer_reference")):
            return "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"
        if not present:
            return None, None, "VERIFY_RECORD_ABSENT"
        return None, None, "VERIFY_RECORD_ANONYMIZED"
    return None, None, None
