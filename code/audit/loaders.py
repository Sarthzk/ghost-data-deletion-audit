"""Load every participant-facing input into plain Python structures.

All evidence formats (SQL snapshots, JSON exports, logs, text handovers, PDFs,
and pre-extracted image facts) are normalised into a single `Observation`
shape so the downstream engine never needs to know which file format a fact
came from.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "dataset"
MEDIA_CACHE = REPO_ROOT / "code" / "media_cache"

# Resource-id prefix -> owning system. Used when the evidence itself does not
# name the system (e.g. a bare SQL row or a log line from a proxy service).
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "marketing": "marketing_platform",
}

ABSENT_RESULTS = {"NOT_FOUND", "MISS", "ABSENT", "DELETED"}


def resource_prefix(resource_id: str) -> str:
    return re.sub(r"_?\d+$", "", resource_id or "")


def system_for(resource_id: str) -> str | None:
    return PREFIX_SYSTEM.get(resource_prefix(resource_id))


@dataclass
class Scope:
    """A snapshot that fully enumerated one system/collection at a point in time."""

    evidence_id: str
    captured_at: str
    observed_at: str
    system: str
    source: str
    records: int = 0   # records the whole file held; empty captures prove nothing


@dataclass
class Observation:
    """One fact about one resource, as seen in one evidence item."""

    evidence_id: str          # file_id from evidence_files.csv
    captured_at: str          # manifest capture time (citation cut-off)
    observed_at: str          # when the state was observed (ordering)
    system: str | None
    resource_id: str
    fields: dict = field(default_factory=dict)
    present: bool | None = None   # None = existence not stated
    source: str = ""              # table / collection / log event

    def values(self) -> frozenset[str]:
        """Lower-cased string values, for identifier matching (cached)."""
        cached = self.__dict__.get("_values")
        if cached is not None:
            return cached
        out = set()
        stack = [self.fields]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, str) and cur and cur.lower() not in ("null", "none"):
                out.add(cur.strip().lower())
        self.__dict__["_values"] = frozenset(out)
        return self.__dict__["_values"]


# --------------------------------------------------------------------------- CSV

def read_csv(name: str) -> list[dict]:
    with open(DATASET / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_tables() -> dict[str, list[dict]]:
    return {
        "requests": read_csv("deletion_requests.csv"),
        "sample_requests": read_csv("sample_deletion_requests.csv"),
        "identities": read_csv("user_identities.csv"),
        "audit_log": read_csv("user_audit_log.csv"),
        "activity": read_csv("system_activity.csv"),
        "rules": read_csv("deletion_rules.csv"),
        "manifest": read_csv("evidence_files.csv"),
    }


# --------------------------------------------------------------------------- helpers

def _clean(v):
    if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
        return None
    return v


def _presence(fields: dict) -> bool | None:
    """Infer whether a record exists from common readback signals."""
    res = str(fields.get("result") or "").upper()
    if res in ABSENT_RESULTS:
        return False
    if res in {"FOUND", "HIT", "IDENTIFIABLE", "ACTIVE", "ANONYMIZED", "PRESENT"}:
        return True
    for key in ("http_status", "status_code"):
        if str(fields.get(key) or "") == "404":
            return False
        if str(fields.get(key) or "") == "200":
            return True
    for key in ("rows", "total_hits", "total"):
        if key in fields and fields[key] is not None:
            try:
                return int(fields[key]) > 0
            except (TypeError, ValueError):
                pass
    if "profile" in fields and fields["profile"] is None:
        return False
    return None


# --------------------------------------------------------------------------- SQL

def _load_sql(path: Path, fid: str, captured: str, scopes: list) -> list[Observation]:
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    out = []
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = dict(con.execute("select origin_system, captured_at from snapshot_scope"))
        scopes += [Scope(fid, captured, ts, sysname, path.stem) for sysname, ts in scope.items()]
    for table in tables:
        if table == "snapshot_scope":
            continue
        cur = con.execute(f'select * from "{table}"')  # noqa: S608
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            rec = {c: _clean(v) for c, v in zip(cols, row)}
            if "record_json" in rec and rec["record_json"]:
                inner = json.loads(rec.pop("record_json"))
                rec.update({k: _clean(v) for k, v in inner.items()})
            rid = rec.get("resource_id")
            if not rid:
                continue
            system = rec.get("origin_system") or system_for(rid)
            observed = scope.get(system) or captured
            out.append(Observation(fid, captured, observed, system, rid, rec, True, table))
    con.close()
    if not scope:
        # replica/primary captures without a scope table enumerate whole systems
        for sysname in sorted({o.system for o in out if o.system}):
            scopes.append(Scope(fid, captured, captured, sysname, path.stem))
    for sc in scopes:
        if sc.evidence_id == fid:
            sc.records = len(out)
    return out


# --------------------------------------------------------------------------- JSON

_TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "exported_at")
_SYSTEM_KEYS = ("system", "data_store")


def _walk_json(node, ctx: dict, fid: str, captured: str, out: list, path: str, scopes: list):
    if isinstance(node, dict):
        ctx = dict(ctx)
        if isinstance(node.get("data_store"), str) and isinstance(node.get("records"), list):
            scopes.append(Scope(fid, captured, node.get("captured_at", captured),
                                node["data_store"], node.get("collection", "")))
        for k in _TIME_KEYS:
            if isinstance(node.get(k), str):
                ctx["time"] = node[k]
                break
        for k in _SYSTEM_KEYS:
            if isinstance(node.get(k), str):
                ctx["system"] = node[k]
        if "resource_id" in node and isinstance(node["resource_id"], str):
            rec = {k: _clean(v) for k, v in node.items()}
            # flatten nested profile / document dicts into the record
            for nk in ("profile", "document"):
                if isinstance(node.get(nk), dict):
                    rec.update({k: _clean(v) for k, v in node[nk].items()})
            rid = node["resource_id"]
            system = ctx.get("system") if (ctx.get("system") or "").endswith(
                ("_service", "_platform")) else None
            system = system or system_for(rid)
            out.append(Observation(fid, captured, ctx.get("time", captured), system,
                                   rid, rec, _presence(rec), path))
            # search hits nest documents that are themselves resources
            hits = node.get("hits")
            if isinstance(hits, dict):
                _walk_json(hits, ctx, fid, captured, out, path + ".hits", scopes)
            return
        for k, v in node.items():
            sub = dict(ctx)
            if k in PREFIX_SYSTEM.values() or k.endswith(("_service", "_platform")):
                sub["system"] = k
            _walk_json(v, sub, fid, captured, out, f"{path}.{k}" if path else k, scopes)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, ctx, fid, captured, out, path, scopes)


def _load_json(path: Path, fid: str, captured: str, scopes: list) -> list[Observation]:
    out: list[Observation] = []
    _walk_json(json.loads(path.read_text(encoding="utf-8")), {}, fid, captured, out, "", scopes)
    for sc in scopes:
        if sc.evidence_id == fid:
            sc.records = len(out)
    return out


# --------------------------------------------------------------------------- logs

_KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_BRACKET = re.compile(r"^\S+ \[(\w+)\] \[(\w+)\] \[([\w-]+)\] (\w+):")
_PIPE = re.compile(r"^\S+ \| (\w+) \| (\w+) \| ([\w-]+) \| (\w+) \|")


_NOISE_EVENTS = {"cache_lookup", "connection_acquired", "dependency_retry", "dependency_recovered",
                 "lease_extended", "pool_sample"}


def _parse_kv(text: str) -> dict:
    out = {}
    for k, v in _KV.findall(text):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    if "message" in out and "=" in out["message"]:
        inner = _parse_kv(out.pop("message"))
        out.update(inner)
    return out


def parse_log_line(line: str) -> dict | None:
    m = _TS.match(line)
    if not m:
        return None
    rec = {"ts": m.group(1)}
    rest = line[m.end():].strip()
    if rest.startswith("{"):
        rec.update(json.loads(rest))
        rec.setdefault("level", rec.get("severity"))
        rec["trace"] = rec.get("trace_id")
        return {k: _clean(v) for k, v in rec.items()}
    b = _BRACKET.match(line) or _PIPE.match(line)
    if b:
        rec.update(level=b.group(1), service=b.group(2), trace=b.group(3), event=b.group(4))
        rec.update(_parse_kv(line[b.end():]))
    else:
        rec.update(_parse_kv(line[m.end():]))
        rec.setdefault("service", rec.get("function"))
        rec["trace"] = rec.get("trace_id") or rec.get("requestId")
    return {k: _clean(v) for k, v in rec.items()}


def _load_log(path: Path, fid: str, captured: str) -> list[Observation]:
    lines = [r for r in map(parse_log_line, path.read_text(encoding="utf-8").splitlines()) if r]
    # Merge "*_started" lines (which carry lookup_key) into the matching
    # completed line through the trace id.
    by_trace: dict[str, dict] = defaultdict(dict)
    singles = []
    for r in lines:
        if r.get("trace"):
            merged = by_trace[r["trace"]]
            for k, v in r.items():
                if v is not None or k not in merged:
                    merged[k] = v
            merged.setdefault("_events", []).append(r.get("event"))
            if r.get("event") not in _NOISE_EVENTS:
                p = _presence(r)
                if p is not None:
                    merged["_presence"] = p
            if r.get("resource_id"):
                merged["_last"] = dict(r)
        else:
            singles.append(r)
    out = []
    for rec in list(by_trace.values()) + singles:
        rid = rec.get("resource_id")
        if not rid:
            continue
        system = rec.get("system") or rec.get("service")
        if not system or not system.endswith(("_service", "_platform")) or system_for(rid) and system != system_for(rid) and not rec.get("system"):
            system = system_for(rid) or system
        presence = rec.pop("_presence", None) if "_events" in rec else _presence(rec)
        out.append(Observation(fid, captured, rec["ts"], system, rid, rec, presence, rec.get("event") or ""))
    return out


# --------------------------------------------------------------------------- text / pdf / images

_HANDOVER = re.compile(r"^- (\S+) \| (\w+) \| (\w+) \| (\w+) \| (\w+) \| (.+)$")


def _load_text(text: str, fid: str, captured: str, source: str) -> list[Observation]:
    out = []
    for line in text.splitlines():
        m = _HANDOVER.match(line.strip())
        if m:
            ts, system, rid, rtype, result, subject = m.groups()
            rec = {"record_type": rtype, "result": result, "subject_reference": subject.strip()}
            out.append(Observation(fid, captured, ts, system, rid, rec, _presence(rec), source))
    return out


def pdf_text(path: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def _load_media_cache(fid: str, captured: str) -> list[Observation]:
    """Image/PDF facts extracted once by a vision model (see media_extract.py)."""
    path = MEDIA_CACHE / f"{fid}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for rec in data.get("records", []):
        rid = rec.get("resource_id")
        if not rid:
            continue
        rec = {k: _clean(v) for k, v in rec.items()}
        out.append(Observation(fid, captured, rec.get("observed_at") or captured,
                               rec.get("system") or system_for(rid), rid, rec,
                               rec.get("present", _presence(rec)), "media"))
    return out


# --------------------------------------------------------------------------- entry point

def load_observations(manifest: list[dict]) -> tuple[list[Observation], list[Scope]]:
    obs: list[Observation] = []
    scopes: list[Scope] = []
    for row in manifest:
        fid, captured = row["file_id"], row["captured_at"]
        path = DATASET / row["file_path"]
        suffix = path.suffix.lower()
        if suffix == ".sql":
            obs += _load_sql(path, fid, captured, scopes)
        elif suffix == ".json":
            obs += _load_json(path, fid, captured, scopes)
        elif suffix == ".log":
            obs += _load_log(path, fid, captured)
        elif suffix == ".txt":
            obs += _load_text(path.read_text(encoding="utf-8"), fid, captured, "text")
        elif suffix == ".pdf":
            obs += _load_media_cache(fid, captured)
        else:
            obs += _load_media_cache(fid, captured)
    return obs, scopes


if __name__ == "__main__":
    from collections import Counter

    t = load_tables()
    o, sc = load_observations(t["manifest"])
    print("scopes:", len(sc), Counter(x.system for x in sc))
    print({k: len(v) for k, v in t.items()})
    print("observations:", len(o))
    print(Counter(x.system for x in o))
    print(Counter(x.present for x in o))
    covered = {x.evidence_id for x in o}
    print("files without observations:", [r["file_id"] + " " + r["file_path"]
                                           for r in t["manifest"] if r["file_id"] not in covered])
