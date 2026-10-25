"""Build code/evaluation/usage_report.md from code/llm_cache/usage_log.jsonl.

    python3 code/evaluation/usage_report.py

Every real model call made by the pipeline is logged once; cached re-runs make
no calls. The report therefore describes the cold run that produced the cached
LLM artifacts used for the final output.csv.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "code" / "llm_cache" / "usage_log.jsonl"
OUT = ROOT / "code" / "evaluation" / "usage_report.md"

# USD per million tokens: (input, output, cache write, cache read). Provider list prices;
# check the provider's pricing page before quoting - update here if they change.
PRICES = {
    "gpt-5-mini": (0.25, 2.00, 0.0, 0.025),
    "gpt-5": (1.25, 10.00, 0.0, 0.125),
    "gpt-5-nano": (0.05, 0.40, 0.0, 0.005),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}


def price_for(model: str) -> tuple:
    # dated snapshots (e.g. "gpt-5-mini-2025-08-07") use their family's price
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    return PRICES["claude-opus-5-5"]


def cost(model: str, r: dict) -> float:
    pin, pout, pcw, pcr = price_for(model)
    return (r["input_tokens"] * pin + r["output_tokens"] * pout
            + r["cache_creation_input_tokens"] * pcw + r["cache_read_input_tokens"] * pcr) / 1e6


def main() -> None:
    rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()] if LOG.exists() else []
    # ad-hoc calls classify custom evaluation requests, not the dataset run
    adhoc = [r for r in rows if r["purpose"].startswith("intent:adhoc")]
    rows = [r for r in rows if not r["purpose"].startswith("intent:adhoc")]
    n_requests = sum(1 for _ in csv.DictReader(open(ROOT / "dataset" / "deletion_requests.csv")))
    by_model: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_purpose: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        for agg, k in ((by_model, r["model"]), (by_purpose, r["purpose"].split(":")[0])):
            a = agg[k]
            a["calls"] += 1
            for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                a[f] += r[f]
            a["cost"] += cost(r["model"], r)

    def table(agg: dict, label: str) -> list[str]:
        lines = [f"| {label} | Calls | Input tok | Output tok | Cache write | Cache read | Total tok | Cost (USD) |",
                 "|---|---:|---:|---:|---:|---:|---:|---:|"]
        tot = defaultdict(float)
        for k, a in sorted(agg.items()):
            total = a["input_tokens"] + a["output_tokens"] + a["cache_creation_input_tokens"] + a["cache_read_input_tokens"]
            lines.append(f"| {k} | {int(a['calls'])} | {int(a['input_tokens']):,} | {int(a['output_tokens']):,} | "
                         f"{int(a['cache_creation_input_tokens']):,} | {int(a['cache_read_input_tokens']):,} | "
                         f"{int(total):,} | ${a['cost']:.4f} |")
            for f, v in a.items():
                tot[f] += v
            tot["total"] += total
        lines.append(f"| **Overall** | {int(tot['calls'])} | {int(tot['input_tokens']):,} | {int(tot['output_tokens']):,} | "
                     f"{int(tot['cache_creation_input_tokens']):,} | {int(tot['cache_read_input_tokens']):,} | "
                     f"{int(tot['total']):,} | ${tot['cost']:.4f} |")
        return lines, tot

    model_lines, tot = table(by_model, "Model")
    purpose_lines, _ = table(by_purpose, "Purpose")
    total_tokens = tot["total"]
    md = [
        "# Token Usage and Cost Report",
        "",
        "Final full-dataset run that produced `output.csv`.",
        "",
        "## How the system uses models",
        "",
        "- **Providers:** " + (", ".join(sorted({r.get('provider', 'anthropic') for r in rows})) or "none (offline run)")
        + ". The pipeline supports OpenAI (`openai` SDK) and Anthropic (`anthropic` SDK); it uses whichever key is configured.",
        "- **Where an LLM is used:** only for (1) reading screenshots and PDFs into structured records "
        "(`media`), and (2) deciding whether a multilingual request text authorises deletion (`intent`).",
        "- **Everything else is deterministic Python:** identity resolution, evidence linking, state "
        "reconstruction, policy application, step ordering. It costs 0 tokens.",
        "- **Token efficiency:** each media file is sent once. Request texts are deduplicated "
        f"(550 requests incl. samples, far fewer unique texts) and classified {40} per call. "
        "All responses are cached on disk, so re-running `python3 code/main.py` makes **0** calls.",
        "",
        "## Per-model totals",
        "",
        *model_lines,
        "",
        "## By purpose",
        "",
        *purpose_lines,
        "",
        "## Per-request averages",
        "",
        f"- Requests in `deletion_requests.csv`: **{n_requests}**",
        f"- Total tokens: **{int(total_tokens):,}**, average **{total_tokens / n_requests:,.1f} tokens per request**",
        f"- Estimated total cost: **${tot['cost']:.4f}**, average **${tot['cost'] / n_requests:.5f} per request**",
        f"- Cost covers all 550 requests (518 + 32 samples) and all {int(by_purpose.get('media', {}).get('calls', 0))} "
        "media files; the per-request figure divides by 518.",
        "",
        "Prices per million tokens (input / output / cached input): "
        + "; ".join(f"{m} ${p[0]} / ${p[1]} / ${p[3]}" for m, p in PRICES.items()
                    if any(price_for(r["model"]) == p for r in rows)) + ".",
        "",
        "## Notes",
        "",
        "- When no API credentials are present, the pipeline still runs end to end. It uses the rule-based "
        "intent classifier and skips media, and makes 0 calls.",
        "- Cached re-runs of the final pipeline: 0 model calls, 0 tokens, $0. A run **without any API key** "
        "reads the same cached answers and reproduces `output.csv` byte for byte.",
        f"- Excluded above: {len(adhoc)} extra call(s) made while scoring our custom evaluation cases "
        f"({sum(r['input_tokens'] + r['output_tokens'] for r in adhoc):,} tokens, "
        f"${sum(cost(r['model'], r) for r in adhoc):.4f}).",
    ]
    OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({int(tot['calls'])} calls, {int(total_tokens):,} tokens, ${tot['cost']:.4f})")


if __name__ == "__main__":
    main()
