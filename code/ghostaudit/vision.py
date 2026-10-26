"""Screenshot understanding with a vision-language model, cached on disk.

Screenshots are ordinary business UIs (admin consoles, search inspectors, connector
dashboards). The VLM is asked to transcribe the visible records into a fixed JSON schema;
it is never asked for a deletion verdict. Results are cached in code/cache/image_facts.json
so the audit run is deterministic and costs nothing once the cache exists.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from . import usage

VISION_MODEL = os.environ.get("GHOST_VISION_MODEL", "claude-opus-5-5")

VISION_PROMPT = """You are transcribing an internal admin screenshot for a privacy audit.
Return ONLY a JSON array. One object per data record that is visibly shown (table rows and the
detail panel), with these keys when visible (omit keys that are not shown):
  system            one of account_service, search_service, analytics_platform, support_platform,
                    export_service, integration_service, backup_service, security_service,
                    identity_service, marketing_platform, object_storage
  resource_id       the internal id exactly as printed (e.g. account_5762, search_428, data_flow_1121);
                    if none is printed use the display key (file name, statement number, user_N)
  status            FOUND (the record is shown as existing)
  customer_reference / subject_reference   user_N references
  full_name, email, phone, display_name, owner_email, customer_email, requester_name, requester_email
  enabled           true/false for connectors (continuous sync / healthy = true)
  restore_allowed   true/false for backups only if the screen states it
  shared_context    true if a support conversation includes agents or other parties
  note              short factual description of what the screen shows for that record
Do not infer anything that is not printed on the screen."""


def _call_vlm(path: Path) -> list:
    import anthropic  # optional dependency, only needed with --refresh-vision
    client = anthropic.Anthropic()
    data = base64.standard_b64encode(path.read_bytes()).decode()
    msg = client.messages.create(
        model=VISION_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text": VISION_PROMPT}]}])
    usage.record(VISION_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    start, end = text.find("["), text.rfind("]")
    return json.loads(text[start:end + 1]) if start >= 0 else []


def load_image_facts(dataset: Path, cache_path: Path, refresh: bool = False) -> dict:
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    images = sorted((dataset / "evidence" / "images").glob("*.png"))
    can_call = refresh and os.environ.get("ANTHROPIC_API_KEY")
    changed = False
    for p in images:
        rel = f"evidence/images/{p.name}"
        if rel in cache and not refresh:
            usage.record(VISION_MODEL, 0, 0, cached=True)
            continue
        if can_call:
            cache[rel] = _call_vlm(p)
            changed = True
        elif rel in cache:
            usage.record(VISION_MODEL, 0, 0, cached=True)
    if changed:
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache
