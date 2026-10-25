"""Attribute identifier-free deletion-worker actions to the request that caused them.

Some resources never show a personal identifier in any evidence: a profile that
was deleted before any readback captured it, or an analytics record that was
already anonymised. The only participant-visible link is the deletion workflow
itself: `system_activity.csv` rows emitted by `deletion_worker` while it
processed a request, shortly before that request's `reported_at`.

Several requests are often reported in the same batch, so attribution is done
globally:
  1. account profiles: each request without an evidence-linked profile gets the
     closest unowned account DELETE in its workflow window (greedy by time gap);
  2. other unowned worker actions shortly before reported_at are matched, in
     resource-id order, to the batch's requests in account-id order.
Resources that any evidence ties to a known person are never re-attributed.

Matching signals, strongest first:
  * the worker's reported_result must agree with the request's reported_status
    (COMPLETE~SUCCESS, NOT_STARTED~FAILED, PARTIALLY_COMPLETE~PARTIAL/SUCCESS);
  * the resource appears in an evidence file that also holds the subject's
    evidence-linked records (the readback that audited this request);
  * the time gap to reported_at.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .loaders import resource_prefix

ACCOUNT_WINDOW = (timedelta(hours=-1.5), timedelta(hours=0.3))
# reported_status -> {worker result: penalty}
COMPATIBLE = {
    "COMPLETE": {"SUCCESS": 0},
    "NOT_STARTED": {"FAILED": 0},
    "PARTIALLY_COMPLETE": {"PARTIAL": 0, "SUCCESS": 1},
}
FOLLOWUP_WINDOW = (timedelta(hours=-0.75), timedelta(hours=0.05))


# Record patterns that never come with an identifier-free follow-up action.
EXCLUSIVE_PATTERNS = {"search", "export", "tombstone", "analytics"}


def _num(rid: str) -> int:
    tail = rid.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def attribute_worker_resources(ctx, requests: list[dict]) -> dict[str, set[str]]:
    worker = [a for a in ctx.tables["activity"] if a["source_system"] == "deletion_worker"]
    unowned = [a for a in worker if not ctx.resource_users.get(a["resource_id"])]
    first_action = {}
    for a in worker:
        first_action.setdefault(a["resource_id"], a)

    from .intent import authorises_deletion

    out: dict[str, set[str]] = {r["case_id"]: set() for r in requests}
    # privacy intake explicitly classified these accounts' requests as non-deletion
    non_deletion_users = set()
    for a in ctx.tables["activity"]:
        if a["event_type"] == "CLASSIFY_REQUEST" and "non-deletion" in a["details"]:
            non_deletion_users |= ctx.resource_users.get(a["resource_id"], set())
    requests = [r for r in requests if authorises_deletion(r, ctx)
                and ctx.resolve(r["email"]).user_id not in non_deletion_users]
    account_time: dict[str, datetime] = {}
    account_id: dict[str, str] = {}
    linked_prefixes: dict[str, set[str]] = {}
    reported = {r["case_id"]: _t(r["reported_at"]) for r in requests}

    files_of = {}
    for o in ctx.observations:
        files_of.setdefault(o.resource_id, set()).add(o.evidence_id)

    # requests that already have an evidence-linked account profile
    need_account = []
    case_files: dict[str, set[str]] = {}
    for r in requests:
        subj = ctx.resolve(r["email"])
        linked = ctx.evidence_linked(subj, r["audit_at"])
        case_files[r["case_id"]] = set().union(*(files_of.get(x, set()) for x in linked)) if linked else set()
        rep = reported[r["case_id"]]
        accts = [x for x in linked if resource_prefix(x) == "account" and x in first_action
                 and (not ctx.resource_users.get(x) or subj.user_id in ctx.resource_users[x])
                 and ACCOUNT_WINDOW[0] <= _t(first_action[x]["event_time"]) - rep <= ACCOUNT_WINDOW[1]]
        if not accts and subj.user_id:
            # profile tied to this person only by evidence captured after the audit
            owned = [a["resource_id"] for a in worker if resource_prefix(a["resource_id"]) == "account"
                     and subj.user_id in ctx.resource_users.get(a["resource_id"], set())
                     and ACCOUNT_WINDOW[0] <= _t(a["event_time"]) - rep <= ACCOUNT_WINDOW[1]]
            if owned:
                accts = owned[:1]
                out[r["case_id"]].add(owned[0])
        linked_prefixes[r["case_id"]] = {resource_prefix(x) for x in linked
                                         if not ctx.resource_users.get(x) or subj.user_id in ctx.resource_users[x]}
        if accts:
            account_time[r["case_id"]] = min(_t(first_action[x]["event_time"]) for x in accts)
            account_id[r["case_id"]] = min(accts, key=lambda x: first_action[x]["event_time"])
        else:
            need_account.append(r)

    # 1. account profiles
    pairs = []
    for r in need_account:
        rep = reported[r["case_id"]]
        for a in unowned:
            if resource_prefix(a["resource_id"]) != "account" or a["event_type"] != "DELETE_RECORD":
                continue
            dt = _t(a["event_time"]) - rep
            penalty = COMPATIBLE.get(r["reported_status"], {}).get(a["reported_result"])
            if penalty is None or not (ACCOUNT_WINDOW[0] <= dt <= ACCOUNT_WINDOW[1]):
                continue
            affinity = 0 if files_of.get(a["resource_id"], set()) & case_files[r["case_id"]] else 1
            cost = (affinity, penalty, abs(dt.total_seconds()))
            pairs.append((cost, r["case_id"], a["resource_id"], _t(a["event_time"])))
    used_cases, used_res = set(), set()
    for _, cid, rid, when in sorted(pairs):
        if cid in used_cases or rid in used_res:
            continue
        used_cases.add(cid)
        used_res.add(rid)
        out[cid].add(rid)
        account_time[cid] = when
        account_id[cid] = rid

    # 1b. requests still without a profile take the nearest unused one in their
    #     window even when the reported status disagrees (lower confidence, but an
    #     unattributed request otherwise yields no verifiable steps at all)
    pairs = []
    for r in need_account:
        if r["case_id"] in used_cases:
            continue
        rep = reported[r["case_id"]]
        for a in unowned:
            if (resource_prefix(a["resource_id"]) != "account" or a["event_type"] != "DELETE_RECORD"
                    or a["resource_id"] in used_res):
                continue
            dt = _t(a["event_time"]) - rep
            if ACCOUNT_WINDOW[0] <= dt <= ACCOUNT_WINDOW[1]:
                pairs.append((abs(dt.total_seconds()), r["case_id"], a["resource_id"], _t(a["event_time"])))
    for _, cid, rid, when in sorted(pairs):
        if cid in used_cases or rid in used_res:
            continue
        used_cases.add(cid)
        used_res.add(rid)
        out[cid].add(rid)
        account_time[cid] = when
        account_id[cid] = rid

    # 2. follow-up actions with no identifiers (typically analytics anonymised
    #    ~30 min before reported_at). Within one batch the worker emits them in
    #    the same resource-id order as the batch's account deletions, so they are
    #    matched in order to the requests whose record pattern can include them.
    by_batch: dict[datetime, list[str]] = {}
    for r in requests:  # requests whose profile was never found still take part
        by_batch.setdefault(reported[r["case_id"]], []).append(r["case_id"])
    for rep, cids in by_batch.items():
        items_by_prefix: dict[str, list[str]] = {}
        for a in unowned:
            rid = a["resource_id"]
            if resource_prefix(rid) == "account" or rid in used_res:
                continue
            dt = _t(a["event_time"]) - rep
            if FOLLOWUP_WINDOW[0] <= dt <= FOLLOWUP_WINDOW[1]:
                items_by_prefix.setdefault(resource_prefix(rid), []).append(rid)
        for prefix, items in items_by_prefix.items():
            items = sorted(set(items), key=_num)
            free = [c for c in cids if prefix not in linked_prefixes[c]]
            eligible = [c for c in free if not (linked_prefixes[c] & EXCLUSIVE_PATTERNS)]
            for pool in (eligible, free):
                if len(pool) == len(items):
                    for cid, rid in zip(sorted(pool, key=lambda c: _num(account_id.get(c, ""))), items):
                        out[cid].add(rid)
                        used_res.add(rid)
                    break
    return out
