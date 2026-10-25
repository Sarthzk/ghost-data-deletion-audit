# Evaluation Report: Ghost Data Still Exists

The harness is `code/evaluation/main.py`. It runs the agent on a request file and scores the result against labelled answers. Every number below can be reproduced with the commands in [Reproducing](#reproducing).

## 1. Metrics and how they are computed

| Metric | Definition |
|---|---|
| **Case-level status accuracy** | `deletion_status` exact match, plus a gold→predicted confusion matrix |
| **Step-level P / R / F1 (full)** | Each step is the tuple `(phase, system_id, resource_id, operation, state, evidence_id)`, compared as a multiset per case |
| **Resource-level P / R / F1** | `(phase, system_id, resource_id, operation)`: did we find the right records and choose the right action? |
| **Field-level accuracy** | On steps aligned by `(phase, system, resource, operation)`: accuracy of `state` and of `evidence_id` |
| **Sequence accuracy (exact)** | Share of cases whose `steps_json` array matches the gold one exactly, including order |
| **Sequence accuracy (LCS)** | Mean longest-common-subsequence length / max length, which gives partial credit for order |
| **Case exact match** | Status and full ordered `steps_json` both identical |
| **Contract violations** | Schema, phase/operation/state pairing, ordering, `step_no` continuity, evidence rules (COMPLETE cites evidence; INCOMPLETE remediation/verification uses `""`) |

The contract validator (`code/audit/contract.py`) passes on all 32 official gold rows, which confirms the ordering rules were implemented correctly.

## 2. Results on the 32 public sample cases (final system)

| Metric | Score |
|---|---|
| Status accuracy | **1.000** (32/32) |
| Confusion | COMPLETE→COMPLETE 10, INCOMPLETE→INCOMPLETE 22 |
| Step P / R / F1 (full tuple) | **0.995 / 0.979 / 0.987** |
| Resource P / R / F1 | 0.995 / 0.979 / 0.987 |
| Field accuracy: `state` | **1.000** (189 aligned steps) |
| Field accuracy: `evidence_id` | **1.000** |
| Sequence exact | **0.875** (28/32) |
| Sequence LCS | 0.974 |
| Case exact match | **0.875** (28/32) |
| Contract violations | **0** |

### Progression (each row is one development iteration, scored on the same 32 cases)

| # | Change | Status | Step F1 | Case exact |
|---|---|---|---|---|
| 0 | Baseline: identity lookup + account record only | 0.406 | 0.095 | 0.031 |
| 1 | Full engine: evidence linking, per-record state, policy → steps | 0.938 | 0.839 | 0.281 |
| 2 | State from *observed* time; snapshot-absence only for never-sighted records | 0.969 | 0.913 | 0.562 |
| 3 | Batch attribution: `reported_status` ↔ worker result + evidence-file affinity | 0.969 | 0.926 | 0.562 |
| 4 | Follow-up actions matched in resource-id order within a batch | 0.969 | 0.944 | 0.656 |
| 5 | Prefer on-time captures for state; per-request ownership for repeat users | 0.969 | 0.968 | 0.812 |
| 6 | Exclude non-deletion requests from batch matching | 0.969 | 0.971 | 0.844 |
| 7 | LLM vision extraction of images/PDFs + LLM intent classifier | **1.000** | **0.987** | **0.875** |

### Ablation: the contribution of the LLM components

| Configuration | Status | Step F1 | Evidence acc. | Case exact | Custom cases |
|---|---|---|---|---|---|
| Fully offline (`AUDIT_OFFLINE=1`, no media cache): rules only | 0.969 | 0.974 | 0.995 | 0.844 | 2/4 |
| Final (gpt-5-mini media extraction + intent) | **1.000** | **0.987** | **1.000** | **0.875** | **4/4** |

The deterministic core does most of the work. The LLM adds what rules can't do reliably: reading screenshots and PDFs (case_07's support attachment is visible only in a PDF), and multilingual intent with negation (custom_03/04).

## 3. Custom evaluation cases (created by our team)

These live in `code/evaluation/custom_cases/` (`requests.csv`, `gold.csv`). Each one isolates a single risk. Expected answers were derived by hand from the dataset evidence and the official sample answers.

| ID | What it tests | Construction | Expected | Result |
|---|---|---|---|---|
| **custom_01** | **Historical-identifier resolution** | case_12's request resubmitted from the user's *old* address `minjun.kim90@gmail.com`, which appears only in `user_audit_log.csv` | Same as case_12: INCOMPLETE, 5 steps (remaining analytics contact record → delete; account absent; audit record retained) | ✅ exact |
| **custom_02** | **Identifier normalisation** | case_11's request with the email upper-cased (`NOVAK.TOMASZ@ICLOUD.COM`); the evidence itself contains upper-case decoys | Same as case_11: INCOMPLETE, 14 steps across search, analytics, support, data flow, account and audit | ✅ exact |
| **custom_03** | **Multilingual negated intent (German)** | case_19's subject, text "*Bitte löschen Sie nichts … nur eine Kopie der Daten … Konto weiter nutzen*" | NOT_APPLICABLE, `[]`. No destructive steps for a data-access request | ✅ exact |
| **custom_04** | **Multilingual negated intent (Japanese)** | case_13's subject, text "*アカウントは削除しないでください … 写しを送ってください*" | NOT_APPLICABLE, `[]` | ✅ exact |

Scores on the custom set: status 1.000, case exact 1.000, 0 contract violations. The offline rules alone score 0.500, because they fail custom_03/04.

## 4. Failures found and improvements made

| Failure found | Root cause | Improvement |
|---|---|---|
| Baseline found almost nothing (step F1 0.10) | Only the account record was considered | Normalised all evidence formats (SQL replicas, 4 JSON layouts, 5 log formats, text handovers, media) into one `Observation` model; linked by current and historical email, phone, hash and `user_id` |
| Retained audit records were marked absent | Every snapshot that omitted a record was treated as proof of absence; snapshot bundles are partial | Absence-by-omission is used only for records never sighted, and only from non-empty full captures (an empty inventory file proves nothing) |
| Gold included records whose only evidence was exported after `audit_at` | Filtering by file capture time discarded exports that *describe* audit time | Link and reconstruct by the observation's own time; cite files captured by `audit_at` when one shows the same state |
| Deleted profiles and anonymised analytics with no identifiers anywhere were missed or assigned to the wrong case | The only link is the deletion worker's batch activity, shared by several requests | Global attribution: worker result must agree with `reported_status` (COMPLETE~SUCCESS, NOT_STARTED~FAILED, PARTIALLY~PARTIAL); prefer resources in the same readback file; follow-ups matched in resource-id order |
| A repeat user's earlier deletion leaked into a later case (case_17) | Same person, two requests months apart | Records whose identifying evidence predates the request and fell inside the earlier request's audit belong to that request; one profile per request window |
| Non-deletion requests were never detected (0 NOT_APPLICABLE) | Requests use deletion words only to rule deletion out ("not asking for deletion") | Negation-aware rule plus an LLM classifier; both agree on all 398 unique texts and flag exactly the 10 non-deletion requests |
| Vision model put order ids, file names and statement labels in `resource_id` | Free-form artifacts | Deterministic post-processing: keep only real resource-id patterns; resolve aliases (statement no., attachment file) via the data catalog with word boundaries; attachments inherit their ticket's id via a shared reference; record type follows the id prefix |
| A legal-register row for `user_17` became `tombstone_17` | `user_17` is also the tombstone's catalog alias | User-id aliases resolve only for identity-service / tombstone records |
| Support attachment treated as a message to redact (case_07) | Ticket and attachment are separate records on one PDF | Any evidence showing `SUPPORT_ATTACHMENT` makes the record follow the attachment rule (DELETE) |
| Custom German/Japanese requests misclassified | Texts outside the two dataset files never reached the LLM | Unseen texts are classified on demand (cached) |

### Known remaining errors

- **case_04, case_18, case_20:** the gold answers include `account_004`, `audit_record_6645` and `audit_record_5569`. A full-text search shows these ids appear **nowhere** in participant-facing data, so no evidence-grounded system can produce them. We chose not to guess.
- **case_23:** three NOT_STARTED requests share one batch with three FAILED account deletions that carry no identifiers. With no further signal, the account is ambiguous (1-in-3).
- **Full dataset:** 1 of 518 rows (`case_417`) has no attributable record: its batch has more requests than account deletions. It is emitted as COMPLETE with `[]`, the only contract warning in `output.csv`.
- **Citation timing:** the pre-submission check found 12 citations of files exported after `audit_at`, even though the records they describe were observed before it. For absent records we now cite the latest on-time full capture that does not list the record, which removed 11 of them. One remains (`case_126`): a finding for a profile still present, whose only evidence is a snapshot taken at `audit_at` but exported two days later. We keep it, because a finding must cite the evidence that proves it.

## 5. Full-dataset run

`python3 code/main.py` → `output.csv`: **518 rows**, sorted by case number, 348 INCOMPLETE / 160 COMPLETE / 10 NOT_APPLICABLE. One contract warning (case_417, above). See `usage_report.md` for tokens and cost.

## Reproducing

```bash
python3 code/evaluation/main.py                       # 32 samples
python3 code/evaluation/main.py --failures            # + per-case diffs
python3 code/evaluation/main.py --requests code/evaluation/custom_cases/requests.csv \
                                --gold code/evaluation/custom_cases/gold.csv   # custom cases
AUDIT_OFFLINE=1 python3 code/evaluation/main.py       # rules-only ablation (keeps media cache)
```

Developer probes used during the investigation: `code/evaluation/investigate.py` (dumps every activity and observation behind a case's gold resources) and `code/evaluation/linkage_probe.py` (shows how each gold resource is linkable).
