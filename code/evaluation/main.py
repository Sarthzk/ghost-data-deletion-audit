"""Score predictions against labelled answers.

Usage (from the repo root):
    python3 code/evaluation/main.py                 # run the agent on the 32 samples and score
    python3 code/evaluation/main.py --pred X.csv    # score an existing prediction file
    python3 code/evaluation/main.py --show case_05  # print a step-by-step diff for one case
    python3 code/evaluation/main.py --failures      # print diffs for every imperfect case
    python3 code/evaluation/main.py --gold code/evaluation/custom_cases/gold.csv \
        --requests code/evaluation/custom_cases/requests.csv   # custom evaluation cases

Metrics:
  * status accuracy     - deletion_status exact match, plus confusion matrix
  * step P/R/F1         - each step as a full tuple (phase, system, resource, op, state, evidence)
  * resource P/R/F1     - (phase, system_id, resource_id, operation) ignoring state/evidence
  * field accuracy      - for steps aligned on (phase, system, resource, operation): state, evidence_id
  * sequence accuracy   - steps_json identical in order (exact) and mean normalised LCS
  * case exact match    - status and steps_json both identical
  * contract violations - schema / ordering / evidence-rule problems in predictions
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "code"))

from audit.contract import validate_row  # noqa: E402

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def read_answers(path: Path) -> dict[str, tuple[str, list[dict]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
                for r in csv.DictReader(fh)}


def step_tuple(s: dict) -> tuple:
    return tuple(str(s.get(f, "")) for f in FIELDS)


def key_tuple(s: dict) -> tuple:
    return tuple(str(s.get(f, "")) for f in FIELDS[:4])


def prf(tp: int, n_pred: int, n_gold: int) -> tuple[float, float, float]:
    p = tp / n_pred if n_pred else 1.0
    r = tp / n_gold if n_gold else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def multiset_tp(a: list, b: list) -> int:
    return sum((Counter(a) & Counter(b)).values())


def lcs(a: list, b: list) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            dp[i + 1][j + 1] = dp[i][j] + 1 if a[i] == b[j] else max(dp[i][j + 1], dp[i + 1][j])
    return dp[-1][-1]


def score(pred: dict, gold: dict) -> dict:
    statuses = Counter()
    confusion = Counter()
    step_tp = step_np = step_ng = 0
    key_tp = key_np = key_ng = 0
    field_hits = Counter()
    field_total = 0
    seq_exact = 0
    lcs_sum = 0.0
    case_exact = 0
    missing = [c for c in gold if c not in pred]
    imperfect = []
    for cid, (g_status, g_steps) in gold.items():
        p_status, p_steps = pred.get(cid, ("<missing>", []))
        confusion[(g_status, p_status)] += 1
        statuses["correct" if g_status == p_status else "wrong"] += 1

        gt, pt = [step_tuple(s) for s in g_steps], [step_tuple(s) for s in p_steps]
        step_tp += multiset_tp(pt, gt)
        step_np += len(pt)
        step_ng += len(gt)

        gk, pk = [key_tuple(s) for s in g_steps], [key_tuple(s) for s in p_steps]
        key_tp += multiset_tp(pk, gk)
        key_np += len(pk)
        key_ng += len(gk)

        # field accuracy on steps aligned by key
        p_by_key = {}
        for s in p_steps:
            p_by_key.setdefault(key_tuple(s), []).append(s)
        for s in g_steps:
            cands = p_by_key.get(key_tuple(s))
            if not cands:
                continue
            m = cands.pop(0)
            field_total += 1
            for f in ("state", "evidence_id"):
                field_hits[f] += str(m.get(f, "")) == str(s.get(f, ""))

        if pt == gt:
            seq_exact += 1
        lcs_sum += (lcs(pt, gt) / max(len(pt), len(gt))) if (pt or gt) else 1.0
        if pt == gt and p_status == g_status:
            case_exact += 1
        else:
            imperfect.append(cid)

    n = len(gold)
    return {
        "cases": n,
        "missing_predictions": missing,
        "status_accuracy": statuses["correct"] / n,
        "confusion": confusion,
        "step_prf": prf(step_tp, step_np, step_ng),
        "resource_prf": prf(key_tp, key_np, key_ng),
        "field_accuracy": {f: (field_hits[f] / field_total if field_total else 0.0)
                           for f in ("state", "evidence_id")},
        "aligned_steps": field_total,
        "sequence_exact": seq_exact / n,
        "sequence_lcs": lcs_sum / n,
        "case_exact": case_exact / n,
        "imperfect": imperfect,
    }


def diff_case(cid: str, pred: dict, gold: dict) -> str:
    g_status, g_steps = gold[cid]
    p_status, p_steps = pred.get(cid, ("<missing>", []))
    lines = [f"--- {cid}: gold={g_status} pred={p_status}"]
    gt, pt = [step_tuple(s) for s in g_steps], [step_tuple(s) for s in p_steps]
    common = Counter(gt) & Counter(pt)
    for t in gt:
        mark = "  " if common[t] > 0 else "- "
        if common[t] > 0:
            common[t] -= 1
        lines.append(f"  {mark}{' | '.join(t)}")
    extra = Counter(pt) - Counter(gt)
    for t in pt:
        if extra[t] > 0:
            extra[t] -= 1
            lines.append(f"  + {' | '.join(t)}")
    if gt != pt and sorted(gt) == sorted(pt):
        lines.append("  (same steps, different order)")
    return "\n".join(lines)


def format_report(m: dict, violations: list[str]) -> str:
    out = [
        f"cases scored         : {m['cases']}  (missing predictions: {len(m['missing_predictions'])})",
        f"status accuracy      : {m['status_accuracy']:.3f}",
        "step P/R/F1 (full)   : %.3f / %.3f / %.3f" % m["step_prf"],
        "resource P/R/F1      : %.3f / %.3f / %.3f" % m["resource_prf"],
        f"field acc state      : {m['field_accuracy']['state']:.3f}  (on {m['aligned_steps']} aligned steps)",
        f"field acc evidence_id: {m['field_accuracy']['evidence_id']:.3f}",
        f"sequence exact       : {m['sequence_exact']:.3f}",
        f"sequence LCS         : {m['sequence_lcs']:.3f}",
        f"case exact match     : {m['case_exact']:.3f}",
        f"contract violations  : {len(violations)}",
        "confusion (gold -> pred): " + ", ".join(
            f"{g}->{p}: {c}" for (g, p), c in sorted(m["confusion"].items())),
    ]
    if m["imperfect"]:
        out.append("imperfect cases      : " + " ".join(m["imperfect"]))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(REPO_ROOT / "dataset" / "sample_output.csv"))
    ap.add_argument("--requests", default=str(REPO_ROOT / "dataset" / "sample_deletion_requests.csv"),
                    help="requests to run the agent on when --pred is not given")
    ap.add_argument("--pred", help="score an existing prediction CSV instead of running the agent")
    ap.add_argument("--show", nargs="*", default=[], help="case ids to diff")
    ap.add_argument("--failures", action="store_true", help="diff every imperfect case")
    ap.add_argument("--json", help="also write metrics as JSON to this path")
    args = ap.parse_args()

    gold = read_answers(Path(args.gold))
    if args.pred:
        pred_path = Path(args.pred)
    else:
        import main as agent  # code/main.py

        pred_path = REPO_ROOT / "code" / "evaluation" / "last_sample_predictions.csv"
        agent.run(Path(args.requests), pred_path)
    pred = read_answers(pred_path)

    violations = []
    with open(pred_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            violations += [f"{row['case_id']}: {v}" for v in validate_row(row)]

    m = score(pred, gold)
    print(format_report(m, violations))
    for v in violations[:20]:
        print("  violation:", v)
    for cid in args.show or (m["imperfect"] if args.failures else []):
        print(diff_case(cid, pred, gold))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {k: v for k, v in m.items() if k != "confusion"}
            | {"confusion": {f"{g}->{p}": c for (g, p), c in m["confusion"].items()},
               "violations": violations}, indent=2))


if __name__ == "__main__":
    main()
