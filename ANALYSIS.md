# Project Analysis: Ghost Data Still Exists

A complete, honest account of this project: what the problem was, what was built, how it was built, what works, what doesn't, and what was learned. It's written to be re-read later for interview prep, so it explains *why* as well as *what*.

---

## 1. TL;DR

- **Event:** HackerRank Orchestrate, October 2026, my first hackathon. Built with AI-assisted development (Claude Code as the coding agent) on 25 Oct 2026.
- **Task:** for 518 "delete my data" requests, decide whether the deletion is really `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE` (not a deletion request). Then output the exact ordered steps: findings, remediation, verification, each citing evidence.
- **What I built:** a hybrid agent.
  - A deterministic Python engine makes every decision.
  - An LLM (OpenAI `gpt-5-mini`) is used only for reading screenshots and PDFs, and for classifying multilingual request intent.
- **Measured result on the 32 public sample cases:**
  - 100% status accuracy;
  - step F1 0.987;
  - 100% correct evidence citations;
  - 28 of 32 cases exactly right.

  Total LLM cost: about $0.08.
- **What happened:** the solution was finished and packaged at about **1:14 PM IST**. Submissions closed at about **7:00 PM IST**. The files were **never uploaded**, so the entry wasn't counted. The work itself is complete and reproducible.
- **Biggest honest caveat:** every score was measured on the 32 public samples only. The hidden answers for the 518 real cases were never seen. Some engine rules were reverse-engineered from patterns in the sample data and may be specific to how this dataset was generated (section 8).

---

## 2. The problem in plain English

A company ("Northstar Shop", fictional) lost $2.4M because a "deleted" customer still existed. Their dashboards said *Complete*, but copies survived in many places:
- database replicas;
- search indexes;
- support tickets and attachments;
- analytics profiles;
- data exports;
- logs;
- backups.

Worse, a **sync job** recreated data that had been deleted.

The agent must act like an auditor:
1. **Is this even a deletion request?** Some customers only want a copy of their data, a correction, or a marketing opt-out. Those are `NOT_APPLICABLE` with no steps.
2. **Who is this person?** Use their current *and* old emails, phones and email hashes, because old systems still hold old identifiers.
3. **What is true at the audit time?** Don't trust the workflow's claimed status. Use the evidence.
4. **What does policy require for each record?** Delete, redact, anonymize, retain, or keep a "tombstone".
5. **Output** the ordered steps in a strict JSON contract.

### The policy (`dataset/deletion_rules.csv`)

| Data category | Required action |
|---|---|
| User profile, secondary identifier, search entry, cache, export, marketing profile | **DELETE** |
| Support message (shared context), email delivery log | **REDACT** (keep the record, remove identifiers) |
| Support attachment owned only by the subject | **DELETE** |
| Analytics profile, product telemetry | **ANONYMIZE** |
| Security audit log (within retention), legal hold | **RETAIN**: must still exist |
| Backup archive | **RETAIN**, but restore must be **blocked** |
| Account tombstone | **REDACT**: keep only the stable `user_id` |
| Active sync / data flow | must be **disabled** first |

### The output contract

There are 15 allowed operations across three phases:
- **FINDING:** `REPORT_REMAINING_RECORD`, `REPORT_EXPOSED_PII`, `REPORT_ACTIVE_DATA_FLOW`, `REPORT_RESTORE_RISK`.
- **REMEDIATION:** `DISABLE_DATA_FLOW`, `BLOCK_RESTORE`, `DELETE_RECORD`, `REDACT_RECORD`, `ANONYMIZE_RECORD`.
- **VERIFICATION:** `VERIFY_RECORD_ABSENT`, `VERIFY_RECORD_PRESENT`, `VERIFY_RECORD_REDACTED`, `VERIFY_RECORD_ANONYMIZED`, `VERIFY_DATA_FLOW_DISABLED`, `VERIFY_RESTORE_BLOCKED`.

Ordering and evidence rules:
- Steps are ordered by phase, then by operation order, then by `system_id`, then by `resource_id`.
- A `COMPLETE` case contains only verification steps, each citing evidence.
- An `INCOMPLETE` case contains findings that cite evidence, plus future remediation and verification steps with an empty `evidence_id`.

### The data

| File | Size | What it holds |
|---|---|---|
| `deletion_requests.csv` | 518 rows | The cases to answer |
| `sample_deletion_requests.csv` / `sample_output.csv` | 32 rows | Public cases with correct answers |
| `user_identities.csv` | 850 users | Current name, email, phone, email hash |
| `user_audit_log.csv` | 306 rows | Historical email, phone and hash changes |
| `system_activity.csv` | 4,011 rows | Global event stream: deletes, syncs, exports, backups. Includes unrelated people and decoys |
| `evidence_files.csv` + `evidence/` | 94 files | SQL snapshots, JSON exports, logs, text, 11 PNG screenshots, 4 PDFs |

---

## 3. Architecture

```
request ─► 1. intent ───────────────► NOT_APPLICABLE → []
              ▼
        2. identity resolution      current + historical emails / phones / hashes / user_id
              ▼
        3. evidence index           every file format → one "Observation" record
              ▼
        4. linking                  which records belong to this person
              ▼
        5. state at audit_at        present? exposed? active? restorable?
              ▼
        6. policy                   deletion_rules.csv → required treatment
              ▼
        7. step planner + validator contract-ordered JSON steps
```

| File | Role |
|---|---|
| `code/main.py` | Entry point. Reads `dataset/`, writes `output.csv` |
| `code/audit/loaders.py` | Parses every evidence format into `Observation(evidence_id, captured_at, observed_at, system, resource_id, fields, present)`; records snapshot "scopes" |
| `code/audit/engine.py` | Identity, linking, state reconstruction, policy, step planning |
| `code/audit/batching.py` | Attributes identifier-free deletion-worker actions to requests |
| `code/audit/contract.py` | Allowed operations, ordering, validator |
| `code/audit/intent.py` | LLM intent classifier + rule-based fallback |
| `code/audit/media_extract.py` | Vision extraction of screenshots/PDFs + deterministic id clean-up |
| `code/audit/llm.py` | Provider-agnostic (OpenAI / Anthropic) cached client with usage logging |
| `code/prompts/*.md` | The two LLM prompts |
| `code/evaluation/main.py` | Evaluation harness |
| `code/evaluation/evaluation_report.md`, `usage_report.md` | Required reports |

About 1,900 lines of Python.

### Why a hybrid, rules-first design?

- **Grading is exact.** It compares resource ids, evidence ids and step order. That needs precise joins across thousands of rows. An LLM guessing those would be slow, costly and non-reproducible.
- **Rules are auditable.** Every verdict can be traced to a specific evidence line.
- **The LLM handles what rules can't:**
  - screenshots and PDFs;
  - multilingual requests that use deletion words to say *don't* delete ("I am not asking for deletion", "Bitte löschen Sie nichts").
- **Proof:** the ablation in section 6.

---

## 4. How each stage really works

### 4.1 Evidence normalisation (`loaders.py`)

Every source becomes the same `Observation` shape:
- **SQL snapshots:** executed in in-memory SQLite. Some tables hold JSON inside a `record_json` column, which is unpacked.
- **JSON:** four different layouts, handled by one recursive walker that finds every object with a `resource_id`.
- **Logs:** five formats (key=value, bracketed, pipe-separated, JSON-lines, and cloud logs with nested escaped messages). Lines are merged by trace id, because the *start* line holds the lookup key (the email) and the *completed* line holds the result (`rows=0`, `http_status=404`).
- **Text handovers:** parsed with one pipe-separated row format.
- **Screenshots and PDFs:** read once by the vision model into structured records and cached in `code/media_cache/`.

### 4.2 Identity resolution

1. Find the `user_id` by current email, or by an old email from the audit log.
2. Collect all of that user's current and historical emails, phones and hashes.
3. Lower-case everything.

Exact matching only: the data contains near-miss decoys like `g.silva76@` vs `g.silva76.jr@`.

### 4.3 Linking records to the person

A record belongs to the subject when:
- **(a)** any observation of it contains one of the subject's identifiers, including query keys like `lookup_key=email` in readback logs; or
- **(b)** a data-catalog `LINK_RESOURCE_IDENTITY` activity names the subject; or
- **(c)** batch attribution assigns it (4.5).

Records that the evidence ties only to a different person are dropped.

### 4.4 State at audit time

- **Which observation decides:** the latest observation whose own `observed_at` is at or before `audit_at`. A bundle exported *later* can still describe the state *at* audit time.
- **What gets cited:** the latest observation showing that same state, preferring files captured by `audit_at`. The rules say never to cite evidence created after the audit.
- **A record never seen at all** is absent only if a non-empty full capture of its system doesn't list it. An empty capture file proves nothing.
- **Signals read:**
  - presence: `rows=0`, `404`, `NOT_FOUND`, `result=miss`, `available=true`;
  - exposure: any non-null email, phone, name or hash field, or `IDENTIFIABLE`;
  - data flows: `enabled`;
  - backups: `restore_allowed`, `deletion_block`.

### 4.5 Batch attribution (`batching.py`): the hardest part

**The problem:** some records have *no identifiers anywhere*. A profile deleted before any readback, or an analytics record already anonymised, never shows an email. The only trace is a `deletion_worker` row in `system_activity.csv`, shortly before the request's `reported_at`. Several requests are often reported in the same batch, so whose record is it?

Signals discovered from the samples, strongest first:
1. **Status agreement.** The worker's `reported_result` matches the request's `reported_status`: COMPLETE with SUCCESS, NOT_STARTED with FAILED, PARTIALLY_COMPLETE with PARTIAL (sometimes SUCCESS). This held on all 31 sample accounts.
2. **Same readback file.** The record appears in an evidence file that also holds the subject's other records.
3. **Timing.** Account deletes happen about 0.75–1.25 h before `reported_at`; analytics anonymisation about 30 minutes before.
4. **Id order.** Within one batch, follow-up actions line up with account deletions when both are sorted by resource id.

Assignment is **global**, so no two requests claim the same record. A second, relaxed pass gives leftover requests the nearest unused account.

### 4.6 Policy → steps

| Situation | Steps |
|---|---|
| Record to delete is present | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |
| Record to delete is absent | VERIFY_RECORD_ABSENT (with evidence) |
| Record to redact or anonymize still has identifiers | REPORT_EXPOSED_PII → REDACT/ANONYMIZE → VERIFY_REDACTED/ANONYMIZED |
| Record to retain | VERIFY_RECORD_PRESENT |
| Data flow is active | REPORT_ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW → VERIFY_DATA_FLOW_DISABLED |
| Backup is restorable | REPORT_RESTORE_RISK → BLOCK_RESTORE → VERIFY_RESTORE_BLOCKED |

Any finding makes the case `INCOMPLETE`, and its verification evidence is blanked because it describes future checks. Steps are then de-duplicated, sorted by contract order and numbered.

### 4.7 The LLM parts

- **Intent:** 550 requests (518 + 32 samples) contain only **398 unique texts**, because many are templated. They're classified 40 per call, about 10 calls, with a strict JSON schema. A rule-based classifier (negation-aware regex) runs as a cross-check. **Both agreed on all 398 texts** and flagged exactly the 10 non-deletion requests (cases 533–542).
- **Vision:** each of the 15 images/PDFs is sent once.

  Deterministic clean-up then fixes the model's mistakes:
  - Only valid resource-id patterns are kept.
  - Printed aliases (statement numbers, attachment file names) are resolved through catalog `LINK` activity, with word-boundary matching.
  - An attachment inherits its ticket's id through a shared reference such as `CS-4827`.
  - The record type follows the resource-id prefix.
- **Caching:** every response is cached on disk. A run **with no API key and no SDK installed** reads the cache and reproduces `output.csv` byte for byte. This was verified, including from a fresh unzip of `code.zip`.

---

## 5. How it was built (timeline, IST, 25 Oct 2026)

| Time | Step | Sample "case exact" |
|---|---|---|
| 12:22 | Read spec, data; plan | n/a |
| 12:32 | Loaders, contract validator, evaluation harness, baseline | 0.031 |
| 12:46 | Real engine; reverse-engineered rules with probe scripts | 0.281 → 0.844 |
| 12:50 | LLM layer (Anthropic first), media extraction, intent classifier | n/a |
| 12:59 | No Anthropic key available, so OpenAI provider added | n/a |
| 13:09 | Ran gpt-5-mini on media + intent | **0.875** |
| 13:14 | Evaluation report, README, code.zip, pre-submission checks | **ready to submit** |
| ~19:00 | Submission window closed; files never uploaded | n/a |

**Development method.** The method was harness-first and evidence-driven:
1. Build the scorer before the solution.
2. Write probe scripts (`investigate.py`, `linkage_probe.py`) that dump every observation and activity behind each gold answer.
3. Form a hypothesis, implement it, and re-score.
4. Keep a change only if the score improves.

**The AI's role.** Claude Code (Claude Opus 5.5) read the data, proposed hypotheses, wrote the code, ran it and iterated. My role was setting direction and making the decisions: start building, which provider to use, when to package.

---

## 6. Results (measured on the 32 public samples only)

| Metric | Score |
|---|---|
| Status accuracy | **1.000** |
| Step precision / recall / F1 | 0.995 / 0.979 / **0.987** |
| `state` field accuracy | 1.000 |
| `evidence_id` accuracy | **1.000** |
| Exact sequence / case match | **0.875** (28/32) |
| Contract violations | 0 |

**Progression of "case exact":** 0.031 → 0.281 → 0.562 → 0.562 → 0.656 → 0.812 → 0.844 → 0.875.

**Ablation**

| Configuration | Status | Step F1 | Case exact | Custom cases |
|---|---|---|---|---|
| Rules only (no LLM, no media) | 0.969 | 0.974 | 0.844 | 2/4 |
| Final (with LLM) | 1.000 | 0.987 | 0.875 | 4/4 |

The deterministic core does most of the work; the LLM closes the last gap.

**Custom test cases (our own)**

| Case | What it tests |
|---|---|
| custom_01 | A request from a user's **old** email |
| custom_02 | An **upper-cased** email |
| custom_03 | A **German** "don't delete, just send me a copy" request |
| custom_04 | A **Japanese** "don't delete" request |

All 4 pass. They also **found a real bug**: request texts outside the dataset files never reached the LLM. That's fixed now.

**Cost:** 25 calls, 99,434 tokens, about **$0.077** total, $0.00015 per request, using `gpt-5-mini`. These are list prices as I knew them; verify them on OpenAI's pricing page.

**Full output:** 518 rows: 348 INCOMPLETE, 160 COMPLETE, 10 NOT_APPLICABLE.

---

## 7. What is still wrong (known errors)

- **case_04, case_18, case_20:** the gold answers contain `account_004`, `audit_record_6645` and `audit_record_5569`. A full-text search shows these ids exist **nowhere** in the participant data. No evidence-based system can produce them, and I chose not to guess.
- **case_23:** three NOT_STARTED requests and three FAILED account deletes share one batch, with nothing else to tell them apart. It's a 1-in-3 guess.
- **case_417** (real set): its batch has more requests than account deletions, so nothing could be attributed. It's emitted as COMPLETE with `[]`, which breaks the contract rule that COMPLETE must have evidence-backed steps.
- **case_126** (real set): one finding cites a file exported 2 days after `audit_at`. The snapshot inside it was taken at audit time, and it's the only evidence for that finding.

---

## 8. The honest caveats

1. **Scores are on 32 samples, not on the 518 graded cases.** The hidden answers were never seen, so real accuracy is unknown and probably lower.
2. **Some rules are dataset-specific heuristics, not universal truths.**
   - The status-agreement rule, the timing windows (−1.5 h to +0.3 h; −0.75 h for follow-ups) and the resource-id ordering were **reverse-engineered from how this synthetic dataset was generated**.
   - They held on every sample, but a real company would solve the same problem with a **lineage table** (request id → resource → action), not timing guesses.
   - In an interview, call this what it is: inferring a hidden join key from data patterns.
3. **Risk of overfitting.** Iterating against 32 samples can overfit them. Mitigations: the rules are generic (no case ids or answers are hard-coded), and the 4 custom cases test generalisation. A held-out split would have been better.
4. **The LLM path tested live is OpenAI only.** The Anthropic code path (including its refusal fallback) was written but never run against the real API, because no key was available.
5. **The model's output needed fixing.** gpt-5-mini put order ids and file names in the resource-id field and invented a wrong type for a tombstone. Deterministic post-processing fixed this. Lesson: never trust LLM output as structured truth without validation.
6. **AI wrote most of the code.** The value I added was direction, decisions and judgment. Know the code well enough to explain it: the pipeline, the batching signals, and why rules beat an LLM here.
7. **The miss was process, not capability.** The solution was ready about 5h45m before the deadline, but the upload never happened.

---

## 9. Lessons for next time

- **Submit a first valid version early, then improve.** Most platforms accept re-submissions. A missed deadline scores zero no matter how good the work is.
- **Keep the countdown visible** and set an alarm about 1 hour before close.
- **Build the evaluation harness first.** Every later decision was data-driven because of it.
- **Read the gold answers like a detective.** Probe scripts showing *why* each answer is what it is were worth more than any amount of guessing.
- **Treat workflow claims as claims.** The theme of the challenge is also a good engineering principle.
- **Use LLMs narrowly, validate their output, cache everything.**

---

## 10. Glossary (for interview vocabulary)

| Term | Meaning here |
|---|---|
| **Tombstone** | A minimal "this account was deleted" record that keeps only the stable `user_id`, so retained records (audit logs, invoices) can still reference it |
| **Replication drift** | A replica (failover/read/regional) still holds data the primary deleted |
| **Data flow / sync** | A connector that copies data between systems; active ones can recreate deleted data |
| **Restore risk** | A backup that can still be restored, bringing deleted data back |
| **Readback** | A verification query run after deletion (e.g. a profile lookup returning 404) |
| **Evidence citation** | The `file_id`, `activity_id` or `audit_id` that proves a step |
| **Ablation** | Removing a component (here, the LLM) to measure its contribution |
| **Structured outputs** | Forcing an LLM to return JSON that matches a schema |
| **Precision / recall / F1** | Share of predicted steps that are right / share of gold steps found / their harmonic mean |
| **LCS** | Longest common subsequence: gives partial credit when steps are right but out of order |

---

## 11. Reproduce it

```bash
git clone <this repo> && cd <repo>
python3 code/main.py                  # writes output.csv (no API key needed, uses cache)
python3 code/evaluation/main.py       # scores on the 32 samples
python3 code/evaluation/main.py --failures   # per-case diffs
```

See `README.md` for re-running the LLM steps, (the interview-prep notes are kept privately).
