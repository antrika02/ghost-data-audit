"""Request intent: does the request text authorize account/personal-data deletion?

Deterministic multilingual rules run first. An optional LLM check (see llm.py) can be
enabled for texts the rules mark as ambiguous; the final run did not need it.
"""
from __future__ import annotations

import re
import unicodedata

DELETE_TERMS = [
    # English
    r"\bdelet", r"\berase", r"\berasure", r"\bremove (?:my|the) (?:account|profile)", r"\bclose (?:my|this|the) account",
    r"\bclosed my account", r"\bwould like it closed", r"\bpermanently deleted", r"\bdeletion\b", r"\bremove records",
    r"\bremove any remaining", r"\bfinish the deletion", r"\baccount was (?:fully )?removed", r"\bremove my name",
    r"\bprofile is gone", r"\bclose the account completely", r"\bremove (?:that|the|any|my)\b", r"\bbeen removed",
    # Spanish / Portuguese / French
    r"\beliminar", r"\beliminaci[oó]n", r"\bexclus[aã]o", r"\bexcluir", r"\bsuppression", r"\bsupprimer",
    # Japanese / Korean
    r"削除", r"삭제",
]

# phrases that explicitly scope the request to something other than deletion
NON_DELETION = [
    r"not (?:a request|asking) (?:to erase|for deletion)", r"not asking for deletion", r"don'?t delete",
    r"do not (?:delete|close)", r"without removing", r"leave (?:the|my) (?:account|profile)", r"keep the account",
    r"marketing lists only", r"stop the promotional", r"information request", r"(?:send me|need) a copy",
    r"download my account information", r"continue using", r"still (?:use|need|want)", r"correct it",
    r"change the email", r"replace it with", r"resetting the login", r"leave my profile",
]


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t or "").lower()
    return t.replace("’", "'")


def classify(text: str) -> tuple[str, str]:
    """Return (label, reason) where label is DELETE, NON_DELETE or AMBIGUOUS."""
    t = _norm(text)
    neg = [p for p in NON_DELETION if re.search(p, t)]
    pos = [p for p in DELETE_TERMS if re.search(p, t)]
    if neg:
        return "NON_DELETE", f"explicit non-deletion scope: {neg[0]}"
    if pos:
        return "DELETE", f"deletion request: {pos[0]}"
    return "AMBIGUOUS", "no deletion or non-deletion signal"
