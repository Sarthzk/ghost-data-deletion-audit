"""Does the request text authorise deletion?

Primary: an LLM classifier (prompt in code/prompts/intent_classification.md).
Request texts are heavily templated, so unique texts are deduplicated and sent
in batches of BATCH per call; results are cached on disk.

Fallback / cross-check: a deterministic rule. Requests that *mention* deletion
only to rule it out ("not asking for deletion", "don't delete anything",
"leave the account active") are the trap, so an explicit keep signal wins over
deletion vocabulary. Disagreements between the two are written to
code/llm_cache/intent_disagreements.json for review.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import llm

BATCH = 40
PROMPT = (Path(__file__).resolve().parents[1] / "prompts" / "intent_classification.md").read_text(encoding="utf-8")
SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "authorizes_deletion": {"type": "boolean"},
            "request_type": {"type": "string", "enum": [
                "DELETION", "DELETION_FOLLOW_UP", "ACCESS", "PORTABILITY", "CORRECTION",
                "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "OTHER"]},
            "reason": {"type": "string"},
        },
        "required": ["id", "authorizes_deletion", "request_type", "reason"],
        "additionalProperties": False}}},
    "required": ["results"],
    "additionalProperties": False,
}

KEEP_SIGNALS = re.compile(
    r"not (asking|a request) (for|to) (deletion|delet|eras)|"
    r"(don'?t|do not|without) (delete|deleting|close|closing|remov|eras|chang)|"
    r"leave (the|my) (account|profile)|keep (the|my) account|continue using|"
    r"still (need|want|use)|marketing lists only|"
    r"(stop|unsubscribe|opt[- ]out).{0,40}(email|marketing|promotional)|"
    r"(correct|replace|change|update) (it|my|the)\b",
    re.I)

_llm_labels: dict[str, dict] | None = None


def rule_authorises(text: str) -> bool:
    # Requests arrive through the deletion workflow, so follow-ups that only ask
    # to "check the latest state" still authorise it; an explicit keep wins.
    return not KEEP_SIGNALS.search(text)


def _classify(texts: list[str], tag: str) -> None:
    unique = sorted(set(texts))
    for start in range(0, len(unique), BATCH):
        chunk = unique[start:start + BATCH]
        payload = json.dumps([{"id": i, "text": t} for i, t in enumerate(chunk)], ensure_ascii=False)
        key = llm.cache_key("intent-v1", PROMPT, SCHEMA, payload)
        out = llm.structured_call(f"intent:{tag}{start // BATCH}", PROMPT,
                                  [{"type": "text", "text": payload}], SCHEMA, key, max_tokens=16000)
        for r in out["results"]:
            if 0 <= r["id"] < len(chunk):
                _llm_labels[chunk[r["id"]]] = r


def classify_all(texts: list[str]) -> dict[str, dict]:
    """LLM labels for every unique text (cached); {} when no credentials."""
    global _llm_labels
    if _llm_labels is not None:
        return _llm_labels
    _llm_labels = {}
    try:
        _classify(texts, "batch")  # served from the committed cache when offline
    except LookupError:
        return _llm_labels  # no cache and no credentials: rule-based fallback
    disagreements = [{"text": t, "llm": v, "rule": rule_authorises(t)}
                     for t, v in _llm_labels.items() if v["authorizes_deletion"] != rule_authorises(t)]
    (llm.CACHE_DIR / "intent_disagreements.json").write_text(
        json.dumps(disagreements, indent=1, ensure_ascii=False), encoding="utf-8")
    return _llm_labels


def authorises_deletion(req: dict, ctx=None) -> bool:
    text = req["request_text"]
    if _llm_labels is not None and text not in _llm_labels:
        try:
            _classify([text], "adhoc")  # a request outside the pre-classified dataset files
        except LookupError:
            pass
    label = (_llm_labels or {}).get(text)
    if label is not None:
        return label["authorizes_deletion"]
    return rule_authorises(text)
