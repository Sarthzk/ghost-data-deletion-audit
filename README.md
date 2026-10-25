# Ghost Data Still Exists: Deletion Audit Agent

HackerRank Orchestrate (October 2026). This agent decides, for every deletion request, whether the person's data is **actually** gone. It answers `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`, and emits the ordered findings, remediation and verification steps with evidence citations.

Challenge: https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

## Results

| Set | Status acc. | Step F1 | Evidence acc. | Case exact |
|---|---|---|---|---|
| 32 public samples | **1.000** | **0.987** | **1.000** | **28/32** |
| 4 custom cases (ours) | 1.000 | 1.000 | 1.000 | 4/4 |

Full details, the ablation, and the failure analysis are in [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md). Token usage and cost are in [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md): the whole LLM run was 25 calls and about $0.08.

## Quick start

The repo layout puts `dataset/` beside `code/`. The prediction run needs **only Python 3.10+ and the standard library**. All LLM outputs are cached in `code/llm_cache/` and `code/media_cache/`, so no API key is needed to reproduce `output.csv`.

```bash
python3 code/main.py                 # reads dataset/, writes ./output.csv (518 rows)
python3 code/evaluation/main.py      # scores the agent on the 32 sample cases
```

Optional: re-run the LLM steps from scratch (cold cache):

```bash
python3 -m venv .venv && .venv/bin/pip install openai anthropic pydantic
echo "OPENAI_API_KEY=..." > .env          # or ANTHROPIC_API_KEY=...; .env is git-ignored
.venv/bin/python code/audit/media_extract.py   # images/PDFs -> code/media_cache/
.venv/bin/python code/main.py                  # intent classification + full run
.venv/bin/python code/evaluation/usage_report.py
```

Environment knobs: `AUDIT_PROVIDER` (`openai`|`anthropic`), `OPENAI_MODEL` (default `gpt-5-mini`), `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `AUDIT_EFFORT` (default `low`), and `AUDIT_OFFLINE=1` (rules only). Keys are read from the environment or `.env` only, and are never logged.

## Architecture

```
request ─► intent ──────────────► NOT_APPLICABLE → []
              │ (LLM, multilingual, negation-aware; rule cross-check)
              ▼
        identity resolution     current + historical emails / phones / hashes / user_id
              ▼
        evidence index          SQL replicas · JSON exports · 5 log formats · text handovers
              │                 · screenshots & PDFs (vision LLM → structured records, cached)
              ▼
        linking                 a. evidence containing the subject's identifiers
                                b. data-catalog LINK activity
                                c. identifier-free deletion-worker actions → batch attribution
              ▼
        state at audit_at       latest observation by observed time; files captured by
                                audit_at cited first; absence from full snapshots
              ▼
        policy                  deletion_rules.csv: DELETE / REDACT / ANONYMIZE / RETAIN,
                                plus data flows (disable) and backups (block restore)
              ▼
        step planner            FINDING → REMEDIATION → VERIFICATION, contract order,
                                evidence only where the contract allows it
              ▼
        validator               schema, ordering and evidence rules on every row
```

**Design principle:** the LLM does only what rules can't do reliably, which is reading images and PDFs and understanding multilingual intent. Everything that decides the answer is deterministic Python, so the output is reproducible, auditable, cheap and fast. The whole run takes about 1 second once caches are warm.

### Key findings encoded in the engine

- **Workflow claims are not proof.** `reported_status` and activity `reported_result` are used only to *attribute* records, never to decide their state.
- **Absence needs evidence.** A record never seen is proven absent only by a non-empty full capture of its system; an empty capture proves nothing.
- **Some records carry no identifiers at all.** A profile deleted before any readback, or an analytics record already anonymised, links to its request only through the deletion worker's batch. Attribution is done globally:
  - the worker result must agree with `reported_status`;
  - records that share an evidence file with the subject's other records are preferred;
  - follow-up actions are matched in resource-id order.
- **Replication drift.** A profile deleted from the primary but alive in a failover replica remains a finding.
- **Tombstones.** These keep the stable `user_id` and must hold no name, email, phone or hash.

## Code map

| Path | Purpose |
|---|---|
| `code/main.py` | Entry point: `python3 code/main.py [--requests X.csv] [--out Y.csv]` |
| `code/audit/loaders.py` | All evidence formats → one `Observation` model; snapshot scopes |
| `code/audit/engine.py` | Identity, linking, state reconstruction, policy → steps |
| `code/audit/batching.py` | Attribution of identifier-free deletion-worker actions |
| `code/audit/contract.py` | Output contract: operations, ordering, validation |
| `code/audit/intent.py` | LLM intent classifier (batched, deduplicated) + rule fallback |
| `code/audit/media_extract.py` | Vision extraction of screenshots/PDFs + deterministic id resolution |
| `code/audit/llm.py` | Provider-agnostic (OpenAI/Anthropic), cached, usage-logged LLM client |
| `code/prompts/*.md` | The two prompts (media extraction, intent classification) |
| `code/evaluation/main.py` | Evaluation harness (status, step, field, sequence metrics, diffs) |
| `code/evaluation/custom_cases/` | Our 4 extra evaluation cases |
| `code/evaluation/usage_report.py` | Generates `usage_report.md` from the usage log |
| `code/evaluation/investigate.py`, `linkage_probe.py` | Developer probes used to reverse-engineer the evidence |

## How AI was used to build this

The solution was developed with **Claude Code** as the coding agent. Every turn is logged in `log.txt`. The workflow:
1. read the spec and data;
2. build the evaluation harness first;
3. reverse-engineer the evidence rules from the samples with probe scripts;
4. iterate engine changes and re-score after each one (see the progression table in the evaluation report);
5. add LLM components only where rules fell short.

At run time, the pipeline calls OpenAI `gpt-5-mini` for vision and intent. It also supports the Anthropic Claude API.
