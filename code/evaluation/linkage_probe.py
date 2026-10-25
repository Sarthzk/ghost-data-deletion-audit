"""Developer probe: how is each gold resource connected to its sample subject?"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit.engine import Context
from audit.loaders import read_csv
ctx = Context()
gold = {r["case_id"]: json.loads(r["steps_json"]) for r in read_csv("sample_output.csv")}
acts = ctx.tables["activity"]
tot = {}
for req in ctx.tables["sample_requests"]:
    subj = ctx.resolve(req["email"]); ids = subj.identifiers()
    for rid in sorted({s["resource_id"] for s in gold[req["case_id"]]}):
        how = []
        if any(o.values() & ids for o in ctx.by_resource.get(rid, [])): how.append("evidence")
        if any(o.values() & {n.lower() for n in subj.names} for o in ctx.by_resource.get(rid, [])): how.append("name")
        if any(a["resource_id"] == rid and a["event_type"] == "LINK_RESOURCE_IDENTITY" for a in acts): how.append("catalog")
        w = [a for a in acts if a["resource_id"] == rid and a["source_system"] == "deletion_worker"]
        if w: how.append("worker@%+.1fh" % ((__import__("datetime").datetime.fromisoformat(w[0]["event_time"][:-1]) - __import__("datetime").datetime.fromisoformat(req["reported_at"][:-1])).total_seconds()/3600))
        if not ctx.by_resource.get(rid): how.append("NO-OBS")
        print(req["case_id"], rid.ljust(20), how)
