# Ghost Data Still Exists — deletion audit agent

HackerRank Orchestrate, October 2026. For every privacy request the agent decides whether the
person asked for deletion, reconstructs what really exists in Northstar Shop's systems at the
audit time, and returns `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with the ordered findings,
remediation and verification steps.

## Run it

Requirements: Python 3.10+ (standard library only) and `pdftotext` (poppler-utils) for the PDF
evidence. No API key is needed — the only model outputs (screenshot transcriptions) are cached.

```bash
# from the repository root
python3 code/main.py                       # -> ./output.csv (518 rows)
python3 code/main.py --trace traces.jsonl  # also write a per-case reasoning trace
python3 code/evaluation/main.py --custom --ablation --show   # evaluation workflow
```

Optional model paths (need `pip install anthropic` and `ANTHROPIC_API_KEY`):

```bash
python3 code/main.py --refresh-vision      # re-transcribe screenshots with Claude vision
```

## How it works

```
request ─► 1 intent ─► 2 identity ─► 3 discovery ─► 4 state at audit_at ─► 5 policy ─► 6 plan
           (rules,      (current +    (evidence     (latest citable         (deletion_   (contract
            LLM if      historical    links + the    observation; claims     rules.csv)   ordering)
            ambiguous)  ids, hashes)  deletion run)  are never state)
```

1. **Intent** (`ghostaudit/intent.py`) — multilingual rules (EN/ES/PT/FR/JA/KO). Explicit
   non-deletion scopes ("don't delete", "copy of my data", "marketing lists only", "correct my
   surname") win over deletion words. Anything ambiguous can be sent to Claude (`llm.py`).
2. **Identity** (`identity.py`) — the request email may be current or historical. We rebuild all
   emails, phones and `sha256(email)` hashes from `user_identities.csv` + `user_audit_log.csv`.
3. **Evidence ingestion** (`ingest.py`) — every file in `evidence_files.csv` becomes normalized
   observations `(file_id, time, system, resource_id, FOUND/NOT_FOUND, fields, identifier keys)`:
   SQL snapshots are executed in SQLite (primary *and* replica tables), JSON exports/bundles,
   six log dialects (key=value, JSON, bracket, pipe, syslog, cloud runtime) merged per trace id so
   the lookup key and the result line join, shift-handover text, PDFs (`pdftotext`) and
   screenshots (Claude vision, cached).
4. **Discovery** (`engine.py`) —
   * *identifier links*: any observation carrying one of the subject's identifiers (email, old
     email, phone, hash, `user_id`) within `[requested_at − 7d, audit_at]`;
   * *data-catalog links*: catalog activity rows map screenshot/document display keys
     (statement numbers, file names) to resource ids;
   * *deletion runs*: many primary profiles and already-anonymized analytics records carry no
     identifier at all. The deletion worker processes one subject's downstream records on a fixed
     cadence after its profile, so they share a phase (seconds mod 30). The phase distribution is
     **learned** from subjects whose account is proven by identifiers, then every request's
     profile deletion is chosen by a **global assignment** over all requests (runs of different
     people interleave), and identifier-less records are claimed by the run whose cadence they fit.
5. **State at `audit_at`** — latest observation from a file captured at or before `audit_at`.
   If a primary and a replica disagree at the same instant, any surviving copy counts as present
   (replication drift). `reported_status` and activity `reported_result` are treated as claims only.
6. **Policy** (`policy.py`) — category → `deletion_rules.csv` action:
   profile/search/export/marketing/attachment → DELETE; support conversation → REDACT;
   analytics → ANONYMIZE; tombstone → REDACT (keep `user_id`, drop name/email/phone/hash);
   security audit log / legal hold → RETAIN (verify present); backup → RETAIN but restore must be
   blocked (a backup with no proof of blocked restore is a restore risk); enabled connector → active data flow.
7. **Plan** — findings, remediation (`DISABLE_DATA_FLOW`, `BLOCK_RESTORE` before record changes),
   verification; within a phase by the operation table order, then `system_id`, `resource_id`.
   Findings and COMPLETE verifications cite the proving `file_id`; future work uses `""`.

## Results (public sample, 32 cases)

Status accuracy 100%, step precision 100%, step recall 97.9%, whole-sequence exact 90.6%.
Details, six custom cases, an ablation and the failure log: `code/evaluation/evaluation_report.md`.
Token/cost: `code/evaluation/usage_report.md` (final run: 0 model calls).

## Layout

```
code/main.py                 entry point
code/ghostaudit/             ingest, identity, intent, policy, engine, vision, llm, usage, runner
code/cache/image_facts.json  cached screenshot transcriptions (vision model output)
code/evaluation/             main.py (workflow), scoring.py, custom_cases.py, ablation.py, reports
```

## In plain words

Think of it like checking that someone really moved out of a building. The front desk says
"done", but we walk every floor: the main office (account), the phone book (search index), the
mailroom (support tickets), the photo albums (analytics), the storage room (backups), and the
pipes that keep refilling rooms (sync connectors). For each place we look at the *most recent
photo taken before the inspection time*. If something of theirs is still there we write what we
saw, what to do about it (turn off the pipes first, then clean up), and how to double-check it
afterwards. Some records must legally stay (security logs, legal holds) — we make sure they are
still there, just without the person's name on them.
