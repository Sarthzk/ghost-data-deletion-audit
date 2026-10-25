"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repo root):
    python3 code/main.py                       # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --out /tmp/x.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "code"))

from audit.contract import dumps_steps  # noqa: E402
from audit.engine import Context, audit_case  # noqa: E402

_CTX: Context | None = None


def context() -> Context:
    global _CTX
    if _CTX is None:
        _CTX = Context()
    return _CTX


def case_sort_key(case_id: str):
    head, _, num = case_id.rpartition("_")
    return (head, int(num)) if num.isdigit() else (case_id, 0)


def run(requests_path: Path, out_path: Path) -> None:
    ctx = context()
    with open(requests_path, newline="", encoding="utf-8") as fh:
        requests = list(csv.DictReader(fh))
    rows = []
    for req in sorted(requests, key=lambda r: case_sort_key(r["case_id"])):
        status, steps = audit_case(req, ctx)
        rows.append({"case_id": req["case_id"], "deletion_status": status,
                     "steps_json": dumps_steps(steps)})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", default=str(REPO_ROOT / "dataset" / "deletion_requests.csv"))
    ap.add_argument("--out", default=str(REPO_ROOT / "output.csv"))
    args = ap.parse_args()
    run(Path(args.requests), Path(args.out))


if __name__ == "__main__":
    main()
