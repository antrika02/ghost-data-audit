# Evaluation report — Ghost Data Still Exists

Reproduce: `python3 code/evaluation/main.py --custom --ablation --show` (from the repository root).

The public sample (`dataset/sample_deletion_requests.csv`, 32 cases) is the only labelled data.
It was audited with exactly the same code path as the full run (`code/main.py`). Deletion runs of
different people interleave in `system_activity.csv`, so the run planner always sees every known
request (sample + evaluation requests) — but only request inputs, never answers.

## 1. Headline metrics (public sample, final version)

| Metric | Value |
| --- | --- |
| Case-level status accuracy (`COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE`) | **32 / 32 = 100.0%** |
| Step precision (exact match on phase, system, resource, operation, state, evidence) | **100.0%** (189 / 189 predicted steps are correct) |
| Step recall | **97.9%** (189 / 193 gold steps found) |
| Step F1 | **0.990** |
| Step F1 ignoring `evidence_id` | 0.990 (every evidence citation we emit is the gold one) |
| Sequence accuracy — whole `steps_json` array identical, order included | **29 / 32 = 90.6%** |
| Sequence similarity (mean LCS / length) | 0.963 |
| Case exact (status and array identical) | 90.6% |

Field-level accuracy (gold step *i* vs predicted step *i*, so one missing step shifts the rest):

| phase | system_id | resource_id | operation | state | evidence_id | step_no |
| --- | --- | --- | --- | --- | --- | --- |
| 97.9% | 95.9% | 95.9% | 95.9% | 95.9% | 99.0% | 97.9% |

### Remaining sample failures (all are missing steps; no wrong steps)

| Case | Missing gold step | Why we cannot produce it from participant data |
| --- | --- | --- |
| case_04 | `account_004` absent, `analytics_901` anonymized | `account_004` never appears in any evidence or activity row, so no deletion run can be anchored and the identifier-less analytics record has nothing to attach to. |
| case_18 | `audit_record_6645` present | This resource id does not occur anywhere in `dataset/` (grep over every file). |
| case_20 | `audit_record_5569` present | Same — not present in any participant-facing file. |

## 2. Hand-built evaluation cases (`code/evaluation/custom_cases.py`)

Each case perturbs a real request or injects one observation and states the expected behaviour.
**Result: 6 / 6 pass.**

| # | Case | What it tests | Expected | Result |
| --- | --- | --- | --- | --- |
| 1 | `case_negated_deletion` | Text mentions "delete" only to forbid it ("Please don't delete anything… I only need a copy") | `NOT_APPLICABLE`, `[]` | PASS |
| 2 | `case_historical_email` | Sample case_29 resent from the *old* address in `user_audit_log.csv` | Same subject (`user_29`) and identical plan | PASS |
| 3 | `case_evidence_after_audit` | Sample case_24 with `audit_at` moved before the snapshots that prove the remaining records | No citation of evidence captured after `audit_at` | PASS |
| 4 | `case_replica_drift` | Inject a copy of a deleted profile into a replica table of a COMPLETE case | Becomes `INCOMPLETE` with `REPORT_REMAINING_RECORD` first, phases ordered | PASS |
| 5 | `case_restore_not_proven_blocked` | Backup that holds the subject's data with no proof that restore is blocked vs. one with `restore_allowed=false` | Restore risk vs. verified blocked | PASS |
| 6 | `case_multilingual_intent` | Korean / Portuguese / Japanese deletion requests and an English marketing opt-out | DELETE ×3, NON_DELETE | PASS |

## 3. Ablation — linking accounts without identifiers (`code/evaluation/ablation.py`)

About half of the deletion cases never show the subject's primary profile id next to an identifier;
the profile id is only visible as a deletion-worker activity. We attach it with the **run-cadence
planner** (see README). To measure it, all 252 (subject, account) pairs that *are* proven by
identifiers were hidden at once and the planner was re-solved:

**212 / 252 = 84.1% of proven accounts recovered from activity cadence alone.** In the real run
those 252 proven pairs stay as anchors, which makes the remaining assignments easier, so this is a
pessimistic estimate for the unproven half.

## 4. Failures found during development and what fixed them

Case-exact on the sample went 59% → 78% → 84% → 91% across these iterations.

| Failure found | Root cause | Improvement |
| --- | --- | --- |
| Search records stored as nested `document` objects were never linked (case_16, case_22) | Identifier fields hidden one level down in `record_json` | Generic flattening of identifier-bearing nested objects in the SQL/JSON parsers |
| Every readback log line counted twice and "started" lines looked like `FOUND` | Lookup lines carry the resource id before the result is known | `*_started` events only contribute lookup keys to their trace, never a state |
| Wrong account chosen when several profiles were deleted in the same batch (cases 21–29) | Picking the nearest deletion by time is ambiguous; batches interleave | Global assignment over all requests, scored by the learned run cadence (phase of downstream actions, seconds mod 30) with per-action-type likelihoods |
| Anonymized analytics profiles (no identifiers at all) missing from COMPLETE cases | Nothing ties them to a person except the deletion run | Claim identifier-less downstream actions for the run whose cadence they match; a run that already shows a *failure*-phase action does not claim a *success*-phase record |
| case_19: support message reported as compliant | PII scanner logs report the identifier as `matched_value`, not `email` | Map scanner matches (`matched_value`) and emails embedded in `message_text` to exposed direct identifiers |
| Record-lookup evidence ("… | BACKUP_ARCHIVE | BLOCKED | user_388") treated as plain `FOUND` | State words (`ACTIVE`, `BLOCKED`, `IDENTIFIABLE`, `ANONYMIZED`, `DISABLED`, `REDACTED`) were ignored outside the text handover | One interpreter for all record-lookup formats (JSON, logs, text) |
| case_05 sentence "remove any remaining **copy of my profile**" classified as a non-deletion | Non-deletion pattern "copy of …" too broad | Narrowed to "send me / need a copy"; ambiguous texts default to the deletion workflow, optional LLM adjudication |
| Screenshot case_34 produced an extra support record | A screenshot shows the whole customer page, not only the audited record | A record seen *only* on a screenshot is in scope when the data catalog links it to the subject or other evidence corroborates it |
| An expired June export was reported for a July request (case_33) | Identifier links were accepted from any time | Evidence used for linking must be within `[requested_at − 7 days, audit_at]` (sweep 0–45 days: sample unchanged, 2 full-set cases change) |

## 5. Known limits

* Resources that exist only in the hidden truth (case_18 / case_20 audit records) cannot be found.
* The cadence planner is statistical; the ablation above bounds its error.
* Screenshots are read by a vision model once and cached (`code/cache/image_facts.json`); the
  facts were checked by hand against the images.
