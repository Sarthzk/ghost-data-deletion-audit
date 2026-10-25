"""Developer tool: dump everything known about a sample case's gold resources.

    python3 code/evaluation/investigate.py case_05 case_22
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from audit.engine import Context  # noqa: E402

NOISE = {"instance", "pod", "region", "attempt", "duration_ms", "took_ms", "shard", "level", "ts",
         "trace", "trace_id", "_last", "_events", "requestId", "function", "logger", "severity"}

ctx = Context()
gold = {r["case_id"]: r for r in csv.DictReader(open(ROOT / "dataset/sample_output.csv"))}
reqs = {r["case_id"]: r for r in ctx.tables["sample_requests"]}
acts = ctx.tables["activity"]
for cid in sys.argv[1:]:
    req, g = reqs[cid], gold[cid]
    subj = ctx.resolve(req["email"])
    print(f"=========== {cid} {g['deletion_status']} reported={req['reported_status']} "
          f"req={req['requested_at']} rep={req['reported_at']} audit={req['audit_at']}")
    print("  text:", req["request_text"][:220])
    print("  subject:", subj)
    for a in ctx.tables["audit_log"]:
        if a["user_id"] == subj.user_id:
            print("  audit:", a)
    steps = json.loads(g["steps_json"])
    for s in steps:
        print("  GOLD", s["phase"][:4], s["operation"], s["system_id"], s["resource_id"], s["evidence_id"])
    rids = {s["resource_id"] for s in steps}
    for a in acts:
        if a["resource_id"] in rids:
            print("  ACT", a["activity_id"], a["event_time"], a["event_type"], a["source_system"], "->",
                  a["target_system"], a["resource_id"], a["reported_result"], "|", a["details"][:160])
    for r in sorted(rids):
        for o in ctx.by_resource.get(r, []):
            f = {k: v for k, v in o.fields.items() if v is not None and k not in NOISE}
            print("  OBS", o.evidence_id, o.captured_at[:16], o.observed_at[:16], o.system, r,
                  "present=", o.present, o.source, json.dumps(f, ensure_ascii=False)[:260])
