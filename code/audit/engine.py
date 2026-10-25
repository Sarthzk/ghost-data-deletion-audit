"""Deletion-audit engine.

Pipeline for one request:
  1. intent      - does the request text authorise deletion?          (intent.py)
  2. identity    - current + historical emails, phones, hashes, user_id
  3. linking     - which resources belong to the subject
                     a. evidence whose contents contain a subject identifier
                     b. data-catalog LINK_RESOURCE_IDENTITY activity
                     c. deletion-worker actions that carry no identifiers, attributed
                        to the request whose workflow batch produced them (batching.py)
  4. state       - per resource, the latest observation at or before audit_at, or
                   absence from a later full snapshot of the owning system
  5. policy      - deletion_rules.csv category -> DELETE / REDACT / ANONYMIZE / RETAIN
  6. steps       - findings, remediation and verification in contract order
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .batching import attribute_worker_resources
from .contract import finalize_steps, make_step
from .intent import authorises_deletion, classify_all
from .loaders import Observation, Scope, load_observations, load_tables, resource_prefix, system_for

# --------------------------------------------------------------------------- identity


@dataclass
class Subject:
    user_id: str | None
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def identifiers(self) -> set[str]:
        ids = {e.lower() for e in self.emails} | {p.lower() for p in self.phones} | self.hashes
        if self.user_id:
            ids.add(self.user_id.lower())
        return ids


# --------------------------------------------------------------------------- record categories

PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "cache": "CACHE_ENTRY",
    "support": "SUPPORT_MESSAGE",
    "analytics": "ANALYTICS_PROFILE",
    "telemetry": "PRODUCT_TELEMETRY",
    "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "backup": "BACKUP_ARCHIVE",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "data_flow": "DATA_FLOW",
    "object": "SUPPORT_ATTACHMENT",
}

# Fields that hold record *content*. Query keys (lookup_key, correlation_key,
# subject_key, ...) are deliberately excluded: they say what the reader searched
# for, not what the record still contains.
IDENTIFIER_FIELDS = {
    "email", "phone", "full_name", "name", "display_name", "email_hash",
    "requester_email", "requester_name", "requester_phone",
    "owner_email", "owner_phone", "customer_email", "matched_value",
}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]+", re.I)


def _truthy(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
    return None


def exposes_identifiers(o: Observation) -> bool:
    f = o.fields
    if str(f.get("result") or "").upper() == "IDENTIFIABLE":
        return True
    if str(f.get("record_type") or "").upper() in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE",
                                                  "ANALYTICS_PROFILE", "EMAIL_DELIVERY_LOG"):
        # handover / ledger rows: FOUND means the identifiable record is there
        if str(f.get("result") or "").upper() in ("FOUND",):
            return True
    try:
        if int(f.get("direct_identifiers") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if f.get("exposed_identifiers"):
        return True
    for k in IDENTIFIER_FIELDS:
        if f.get(k) not in (None, "", [], {}):
            return True
    return False


def category_of(rid: str, obs: list[Observation]) -> str:
    stated = [rt for o in obs if (rt := o.fields.get("record_type") or o.fields.get("data_category"))
              and rt != "DATA_FLOW"]
    if "SUPPORT_ATTACHMENT" in stated:  # a ticket carrying a subject-only attachment
        return "SUPPORT_ATTACHMENT"
    if stated:
        return stated[0]
    cat = PREFIX_CATEGORY.get(resource_prefix(rid), "UNKNOWN")
    for o in obs:
        f, ev = o.fields, str(o.fields.get("event") or o.source)
        if cat == "SUPPORT_MESSAGE":
            if ev.startswith("attachment_") or f.get("file") or f.get("attachment_name"):
                return "SUPPORT_ATTACHMENT"
            if f.get("source") == "legacy_contact_index" or ev == "deletion_marker_check":
                return "SECONDARY_IDENTIFIER"
        if cat == "ANALYTICS_PROFILE" and ("email_opt_in" in f or ev.startswith("contact_")):
            return "MARKETING_PROFILE"
    return cat


# --------------------------------------------------------------------------- state


@dataclass
class ResourceState:
    resource_id: str
    system: str
    category: str
    present: bool | None = None
    exposed: bool = False
    active: bool | None = None        # data flows
    restorable: bool | None = None    # backups
    evidence: str = ""
    observed_at: str = ""


def _flow_active(o: Observation) -> bool | None:
    f = o.fields
    for k in ("enabled", "scheduler_registered"):
        t = _truthy(f.get(k))
        if t is not None:
            return t
    res = str(f.get("result") or "").upper()
    if res in ("ACTIVE", "ENABLED"):
        return True
    if res in ("DISABLED", "STOPPED", "INACTIVE"):
        return False
    status = str(f.get("last_delivery_status") or "").lower()
    if status == "stopped":
        return False
    return None


def _backup_restorable(o: Observation) -> bool | None:
    f = o.fields
    t = _truthy(f.get("restore_allowed"))
    if t is not None:
        return t
    t = _truthy(f.get("deletion_block"))
    if t is not None:
        return not t
    t = _truthy(f.get("eligible"))
    if t is not None:
        return t
    res = str(f.get("result") or "").upper()
    if res in ("ACTIVE", "RESTORABLE"):
        return True
    if res in ("BLOCKED",):
        return False
    return None


def _present(o: Observation) -> bool | None:
    if o.present is not None:
        return o.present
    f = o.fields
    for k in ("available", "marker_found"):
        t = _truthy(f.get(k))
        if t is not None:
            # marker_found=true means a deletion marker exists -> record is gone
            return t if k == "available" else not t
    return True  # a record-shaped observation without a negative signal


def _signal(category: str, o: Observation):
    """The state an observation asserts, or None if it says nothing decisive."""
    if category == "DATA_FLOW":
        a = _flow_active(o)
        return None if a is None else ("active", a)
    if category == "BACKUP_ARCHIVE":
        r = _backup_restorable(o)
        return None if r is None else ("restorable", r)
    p = _present(o)
    return ("present", p, exposes_identifiers(o) if p else False)


def resource_state(rid: str, obs: list[Observation], scopes: list[Scope], audit_at: str) -> ResourceState:
    """State at audit_at.

    The state comes from the latest observation whose *observed* time is at or
    before audit_at (a bundle exported later can still describe audit time).
    The cited evidence is the latest observation asserting that same state,
    preferring files that were already captured by audit_at.
    """
    # Files captured by audit_at take precedence; a later export that describes
    # audit time is only used when nothing captured in time speaks to the record.
    on_time = [o for o in obs if o.captured_at <= audit_at and o.observed_at <= audit_at]
    late = [o for o in obs if o.observed_at <= audit_at < o.captured_at]
    cat = category_of(rid, on_time or late or obs)
    if not any(_signal(cat, o) is not None for o in on_time):
        on_time = on_time + late
    seen = sorted(on_time, key=lambda o: (o.observed_at, o.captured_at))
    system = (next((o.system for o in seen if o.system), None)
              or next((o.system for o in obs if o.system), None) or system_for(rid) or "")
    st = ResourceState(rid, system, cat)
    decisive = [(o, sig) for o in seen if (sig := _signal(st.category, o)) is not None]
    if decisive:
        final = decisive[-1][1]
        same = [o for o, sig in decisive if sig == final]
        cited = [o for o in same if o.captured_at <= audit_at] or same
        o = cited[-1]
        st.evidence, st.observed_at = o.evidence_id, o.observed_at
        if o.captured_at > audit_at and final[0] == "present" and final[1] is False:
            # never cite a file exported after audit_at: an on-time full capture
            # that does not list the record proves the same absence
            caps = [s for s in scopes if s.system == system and s.captured_at <= audit_at and s.records > 0
                    and s.evidence_id not in {x.evidence_id for x in obs}]
            if caps:
                st.evidence = max(caps, key=lambda s: (s.observed_at, s.captured_at)).evidence_id
        if final[0] == "active":
            st.present, st.active = True, final[1]
        elif final[0] == "restorable":
            st.present, st.restorable = True, final[1]
        else:
            st.present, st.exposed = final[1], final[2]
        return st
    # Never sighted: absence from the latest non-empty full capture of its system.
    caps = [s for s in scopes if s.system == system and s.captured_at <= audit_at and s.records > 0]
    if caps:
        last = max(caps, key=lambda s: (s.observed_at, s.captured_at))
        st.present, st.evidence, st.observed_at = False, last.evidence_id, last.observed_at
    return st


# --------------------------------------------------------------------------- policy -> steps

ACTION_FOR = {"DATA_FLOW": "DISABLE", "BACKUP_ARCHIVE": "BLOCK"}


def required_action(category: str, rules: dict[str, str]) -> str:
    if category in ACTION_FOR:
        return ACTION_FOR[category]
    return rules.get(category, "DELETE")


def plan_steps(states: list[ResourceState], rules: dict[str, str], complete_mode: bool | None = None):
    findings, remediation, verification = [], [], []
    for st in states:
        action = required_action(st.category, rules)
        sysid, rid, ev = st.system, st.resource_id, st.evidence
        if action == "DISABLE":
            if st.active:
                findings.append(make_step("REPORT_ACTIVE_DATA_FLOW", sysid, rid, ev))
                remediation.append(make_step("DISABLE_DATA_FLOW", sysid, rid))
                verification.append(make_step("VERIFY_DATA_FLOW_DISABLED", sysid, rid))
            else:
                verification.append(make_step("VERIFY_DATA_FLOW_DISABLED", sysid, rid, ev))
        elif action == "BLOCK" or (action == "RETAIN" and st.category == "BACKUP_ARCHIVE"):
            if st.restorable:
                findings.append(make_step("REPORT_RESTORE_RISK", sysid, rid, ev))
                remediation.append(make_step("BLOCK_RESTORE", sysid, rid))
                verification.append(make_step("VERIFY_RESTORE_BLOCKED", sysid, rid))
            else:
                verification.append(make_step("VERIFY_RESTORE_BLOCKED", sysid, rid, ev))
        elif action == "DELETE":
            if st.present:
                findings.append(make_step("REPORT_REMAINING_RECORD", sysid, rid, ev))
                remediation.append(make_step("DELETE_RECORD", sysid, rid))
                verification.append(make_step("VERIFY_RECORD_ABSENT", sysid, rid))
            else:
                verification.append(make_step("VERIFY_RECORD_ABSENT", sysid, rid, ev))
        elif action in ("REDACT", "ANONYMIZE"):
            fin_op, rem_op, ver_op = (("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
                                      if action == "REDACT" else
                                      ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"))
            if st.present is False:
                verification.append(make_step("VERIFY_RECORD_ABSENT", sysid, rid, ev))
            elif st.exposed:
                findings.append(make_step(fin_op, sysid, rid, ev))
                remediation.append(make_step(rem_op, sysid, rid))
                verification.append(make_step(ver_op, sysid, rid))
            else:
                verification.append(make_step(ver_op, sysid, rid, ev))
        elif action == "RETAIN":
            verification.append(make_step("VERIFY_RECORD_PRESENT", sysid, rid, ev if st.present else ""))
    if findings:
        for s in verification:  # future checks carry no evidence
            s["evidence_id"] = ""
        return "INCOMPLETE", finalize_steps(findings + remediation + verification)
    return "COMPLETE", finalize_steps(verification)


# --------------------------------------------------------------------------- context


class Context:
    """Everything loaded once and shared across cases."""

    def __init__(self):
        self.tables = load_tables()
        self.observations, self.scopes = load_observations(self.tables["manifest"])
        self.identities = {r["user_id"]: r for r in self.tables["identities"]}
        self.rules = {r["data_category"]: r["required_action"] for r in self.tables["rules"]}
        self.by_resource: dict[str, list[Observation]] = {}
        for o in self.observations:
            self.by_resource.setdefault(o.resource_id, []).append(o)
        # identifier -> user_id, for spotting resources that belong to someone else
        self.owner_of: dict[str, set[str]] = {}
        for uid in self.identities:
            for ident in self.resolve_user(uid).identifiers():
                self.owner_of.setdefault(ident, set()).add(uid)
        self.resource_users: dict[str, set[str]] = {}
        for o in self.observations:
            users = set().union(*(self.owner_of.get(v, set()) for v in o.values())) if o.values() else set()
            if users:
                self.resource_users.setdefault(o.resource_id, set()).update(users)
        for a in self.tables["activity"]:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                for tok in re.findall(r"user_\d+", a["details"]):
                    self.resource_users.setdefault(a["resource_id"], set()).add(tok)
        all_requests = self.tables["sample_requests"] + self.tables["requests"]
        classify_all([r["request_text"] for r in all_requests])  # LLM intent labels (cached)
        self.requests_by_user: dict[str, list[dict]] = {}
        for r in all_requests:
            self.requests_by_user.setdefault(self.resolve(r["email"]).user_id, []).append(r)
        self.worker_time = {}
        for a in self.tables["activity"]:
            if a["source_system"] == "deletion_worker":
                self.worker_time.setdefault(a["resource_id"], a["event_time"])
        self.worker_attribution = attribute_worker_resources(self, all_requests)

    # identity -----------------------------------------------------------------
    def resolve_user(self, uid: str) -> Subject:
        subj = Subject(uid)
        cur = self.identities.get(uid)
        if cur:
            subj.emails.add(cur["email"].lower())
            subj.phones.add(cur["phone"])
            subj.hashes.add(cur["email_hash"].lower())
            subj.names.add(cur["name"])
        for a in self.tables["audit_log"]:
            if a["user_id"] == uid:
                for v in (a["old_value"], a["new_value"]):
                    if a["field_name"] == "email":
                        subj.emails.add(v.lower())
                    elif a["field_name"] == "phone":
                        subj.phones.add(v)
                    elif a["field_name"] == "email_hash":
                        subj.hashes.add(v.lower())
        return subj

    def resolve(self, email: str) -> Subject:
        email = email.strip().lower()
        uid = next((r["user_id"] for r in self.tables["identities"] if r["email"].strip().lower() == email), None)
        if uid is None:
            uid = next((a["user_id"] for a in self.tables["audit_log"] if a["field_name"] == "email"
                        and email in (a["old_value"].lower(), a["new_value"].lower())), None)
        if uid is None:
            return Subject(None, {email})
        subj = self.resolve_user(uid)
        subj.emails.add(email)
        return subj

    # linking ------------------------------------------------------------------
    def evidence_linked(self, subj: Subject, audit_at: str) -> set[str]:
        ids = subj.identifiers()
        out = {o.resource_id for o in self.observations if o.observed_at <= audit_at and o.values() & ids}
        for a in self.tables["activity"]:
            if (a["event_type"] == "LINK_RESOURCE_IDENTITY" and a["event_time"] <= audit_at
                    and any(i in a["details"].lower() for i in ids if "@" in i or i.startswith("user_"))):
                if subj.user_id and re.search(rf"\b{re.escape(subj.user_id)}\b", a["details"]):
                    out.add(a["resource_id"])
        return out

    def linked_resources(self, req: dict, subj: Subject) -> set[str]:
        out = self.evidence_linked(subj, req["audit_at"])
        out |= self.worker_attribution.get(req["case_id"], set())
        out = self._this_request_only(req, subj, out)
        # drop resources that the evidence ties to a different person only
        if subj.user_id:
            out = {r for r in out if not self.resource_users.get(r) or subj.user_id in self.resource_users[r]}
        return out


    def _this_request_only(self, req: dict, subj: Subject, rids: set[str]) -> set[str]:
        """Drop records that belong to the same person's *other* deletion request."""
        worker_time = self.worker_time
        lo, hi = window(req)
        # one profile per request: prefer the account the workflow touched for this request
        accts = [r for r in rids if resource_prefix(r) == "account" and r in worker_time]
        mine = [r for r in accts if lo <= worker_time[r] <= hi]
        if mine:
            rids = rids - (set(accts) - set(mine))
        others = [o for o in self.requests_by_user.get(subj.user_id, []) if o["case_id"] != req["case_id"]]
        for other in others:
            if other["audit_at"] >= req["requested_at"]:
                continue
            # evidence that predates this request and fell inside the earlier audit
            for r in list(rids):
                seen = [o.observed_at for o in self.by_resource.get(r, []) if o.values() & subj.identifiers()]
                acts = [a["event_time"] for a in self.tables["activity"] if a["resource_id"] == r
                        and a["event_type"] == "LINK_RESOURCE_IDENTITY"]
                stamps = seen + acts
                if stamps and max(stamps) <= other["audit_at"]:
                    rids = rids - {r}
        return rids


def window(req: dict) -> tuple[str, str]:
    """ISO bounds of the deletion workflow run for a request."""
    from datetime import datetime, timedelta
    rep = datetime.fromisoformat(req["reported_at"].replace("Z", "+00:00"))
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return fmt(rep - timedelta(hours=1.5)), fmt(rep + timedelta(hours=0.3))


def audit_case(req: dict, ctx: Context) -> tuple[str, list[dict]]:
    if not authorises_deletion(req, ctx):
        return "NOT_APPLICABLE", []
    subj = ctx.resolve(req["email"])
    rids = ctx.linked_resources(req, subj)
    states = [resource_state(r, ctx.by_resource.get(r, []), ctx.scopes, req["audit_at"]) for r in sorted(rids)]
    states = [s for s in states if s.system]
    return plan_steps(states, ctx.rules)
