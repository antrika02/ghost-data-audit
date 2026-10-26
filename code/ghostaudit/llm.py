"""Optional LLM adjudication of request intent.

The deterministic classifier in intent.py labels every request DELETE, NON_DELETE or
AMBIGUOUS. Only AMBIGUOUS texts are sent to the model (when ANTHROPIC_API_KEY is set);
answers are cached by text hash so reruns are free and deterministic.
In the final run every request was decided by the rules, so no call was made.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import usage

INTENT_MODEL = os.environ.get("GHOST_INTENT_MODEL", "claude-haiku-5-5")
CACHE = Path(__file__).resolve().parent.parent / "cache" / "intent_llm.json"

INTENT_PROMPT = """You review privacy requests sent to an online shop.
Decide whether the request below AUTHORIZES deleting the customer's account / personal data.
Requests for access, a copy/export, correction, marketing opt-out, login help or anything that
explicitly keeps the account are NOT deletion requests, even if they mention deletion.
Answer with exactly one word: DELETE or NON_DELETE.

Request:
<<<{text}>>>"""


def adjudicate(text: str) -> str | None:
    key = hashlib.sha256(text.encode()).hexdigest()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if key in cache:
        usage.record(INTENT_MODEL, 0, 0, cached=True)
        return cache[key]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    msg = anthropic.Anthropic().messages.create(
        model=INTENT_MODEL, max_tokens=5,
        messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}])
    usage.record(INTENT_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
    ans = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip().upper()
    ans = "NON_DELETE" if "NON" in ans else "DELETE"
    cache[key] = ans
    CACHE.write_text(json.dumps(cache, indent=2))
    return ans
