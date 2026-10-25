"""The output contract from problem_statement.md: operations, ordering, validation."""
from __future__ import annotations

import json

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# operation -> (phase, state). Dict order is the in-phase sort order mandated
# by the spec ("follow the operation order in the table above").
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OP_RANK = {op: i for i, op in enumerate(OPERATIONS)}
STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def make_step(operation: str, system_id: str, resource_id: str, evidence_id: str = "") -> dict:
    phase, state = OPERATIONS[operation]
    return {"phase": phase, "system_id": system_id, "resource_id": resource_id,
            "operation": operation, "state": state, "evidence_id": evidence_id}


def finalize_steps(steps: list[dict]) -> list[dict]:
    """De-duplicate, sort by (phase, operation, system, resource) and number from 1."""
    seen, unique = set(), []
    for s in steps:
        k = (s["operation"], s["system_id"], s["resource_id"])
        if k not in seen:
            seen.add(k)
            unique.append(s)
    unique.sort(key=lambda s: (OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    return [{"step_no": i, **{k: s[k] for k in STEP_KEYS[1:]}} for i, s in enumerate(unique, 1)]


def dumps_steps(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def validate_row(row: dict) -> list[str]:
    """Return human-readable contract violations for one output row."""
    errs = []
    status = row.get("deletion_status")
    if status not in STATUSES:
        errs.append(f"bad status {status!r}")
    try:
        steps = json.loads(row.get("steps_json") or "")
    except json.JSONDecodeError as e:
        return errs + [f"steps_json is not JSON: {e}"]
    if not isinstance(steps, list):
        return errs + ["steps_json is not an array"]
    if status == "NOT_APPLICABLE" and steps:
        errs.append("NOT_APPLICABLE must have []")
    if status in ("COMPLETE", "INCOMPLETE") and not steps:
        errs.append(f"{status} with no steps")
    last_rank = -1
    for i, s in enumerate(steps, 1):
        if set(s) != set(STEP_KEYS):
            errs.append(f"step {i}: keys {sorted(s)}")
            continue
        if s["step_no"] != i:
            errs.append(f"step {i}: step_no={s['step_no']}")
        op = s["operation"]
        if op not in OPERATIONS:
            errs.append(f"step {i}: unknown operation {op}")
            continue
        phase, state = OPERATIONS[op]
        if s["phase"] != phase or s["state"] != state:
            errs.append(f"step {i}: {op} needs phase={phase} state={state}")
        if status == "COMPLETE" and phase != "VERIFICATION":
            errs.append(f"step {i}: COMPLETE may only contain verification")
        if status == "COMPLETE" and not s["evidence_id"]:
            errs.append(f"step {i}: COMPLETE verification needs evidence")
        if status == "INCOMPLETE" and phase == "FINDING" and not s["evidence_id"]:
            errs.append(f"step {i}: finding needs evidence")
        if status == "INCOMPLETE" and phase != "FINDING" and s["evidence_id"]:
            errs.append(f"step {i}: future {phase.lower()} must have empty evidence_id")
        rank = (OP_RANK[op], s["system_id"], s["resource_id"])
        if last_rank != -1 and rank < last_rank:
            errs.append(f"step {i}: out of order")
        last_rank = rank
    return errs
