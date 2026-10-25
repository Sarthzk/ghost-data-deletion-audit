"""Extract structured facts from screenshots and PDFs with a vision model.

Run once (results are cached under code/media_cache/<file_id>.json and picked up
by loaders.py on every later run):

    .venv/bin/python code/audit/media_extract.py

Each image/PDF is sent once with the prompt in code/prompts/media_extraction.md
and a strict JSON schema. Resource ids that the artifact only shows as an alias
(e.g. a statement number) are resolved deterministically afterwards through the
data catalog's LINK_RESOURCE_IDENTITY activity.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit import llm  # noqa: E402
from audit.loaders import DATASET, MEDIA_CACHE, PREFIX_SYSTEM, read_csv  # noqa: E402

PROMPT = (Path(__file__).resolve().parents[1] / "prompts" / "media_extraction.md").read_text(encoding="utf-8")
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}

_S = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_B = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
RECORD_FIELDS = {
    "resource_id": _S, "system": _S, "record_type": _S,
    "references": {"type": "array", "items": {"type": "string"}},
    "subject_reference": _S, "full_name": _S, "email": _S, "phone": _S, "email_hash": _S,
    "present": _B, "enabled": _B, "restore_allowed": _B,
    "retention_until": _S, "observed_at": _S, "notes": _S,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "artifact_kind": {"type": "string"},
        "records": {"type": "array", "items": {
            "type": "object", "properties": RECORD_FIELDS,
            "required": list(RECORD_FIELDS), "additionalProperties": False}},
    },
    "required": ["artifact_kind", "records"],
    "additionalProperties": False,
}


def alias_map() -> dict[str, str]:
    """'NST-2026-0718-1842' -> 'export_1842' from 'linked export_1842 (NST-...) to ...'."""
    out = {}
    for a in read_csv("system_activity.csv"):
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = re.search(r"\b(\w+_\d+) \(([^)]+)\)", a["details"])
            if m and m.group(1) != m.group(2):
                out[m.group(2).lower()] = m.group(1)
    return out


RESOURCE_ID = re.compile(r"\b(" + "|".join(sorted(map(re.escape, PREFIX_SYSTEM), key=len, reverse=True))
                         + r")_\d+\b")


def normalise_ids(rec: dict, aliases: dict[str, str]) -> None:
    """Keep only real resource ids in resource_id; resolve printed aliases.

    The model sometimes puts an order id, file name or statement label in
    resource_id. Those move to references, and any reference the data catalog
    maps to a resource (statement number, attachment file name) resolves it.
    """
    rid = rec.get("resource_id") or ""
    m = RESOURCE_ID.fullmatch(rid.strip())
    if not m:
        if rid:
            rec["references"] = [rid] + list(rec.get("references") or [])
        rec["resource_id"] = None
        for ref in rec.get("references") or []:
            low = ref.lower()
            hit = aliases.get(low) or next((v for k, v in aliases.items()
                                            if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", low)), None)
            # a bare user id is only an alias for that user's account tombstone
            if hit and hit.startswith("tombstone_") and not (
                    rec.get("record_type") == "ACCOUNT_TOMBSTONE" or rec.get("system") == "identity_service"):
                hit = None
            if hit:
                rec["resource_id"], rec["resolved_from_alias"] = hit, ref
                break
            inline = RESOURCE_ID.search(low)
            if inline:
                rec["resource_id"] = inline.group(0)
                break


def _ref_tokens(rec: dict) -> set[str]:
    toks = set()
    for ref in rec.get("references") or []:
        toks |= {t for t in re.findall(r"[A-Z]{2,4}-\d{3,}", ref)}
    return toks


def inherit_ids(records: list[dict]) -> None:
    """An id-less record (e.g. a ticket's attachment) inherits the resource id of
    the one record in the same artifact that shares its ticket/case reference."""
    for rec in records:
        if rec.get("resource_id"):
            continue
        mine = _ref_tokens(rec)
        owners = {r["resource_id"] for r in records if r.get("resource_id") and mine & _ref_tokens(r)}
        if len(owners) == 1:
            rec["resource_id"] = owners.pop()
            rec["resolved_from_alias"] = "shared reference " + ", ".join(sorted(mine))


# Prefixes whose data category is fixed; the resource id outranks the model's label.
FIXED_TYPE = {"account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "export": "USER_EXPORT",
              "audit_record": "SECURITY_AUDIT_LOG", "legal_hold": "LEGAL_HOLD_RECORD",
              "backup": "BACKUP_ARCHIVE", "tombstone": "ACCOUNT_TOMBSTONE", "data_flow": "DATA_FLOW",
              "marketing": "MARKETING_PROFILE"}


def fix_type(rec: dict) -> None:
    rid = rec.get("resource_id") or ""
    prefix = re.sub(r"_\d+$", "", rid)
    if prefix in FIXED_TYPE:
        rec["record_type"] = FIXED_TYPE[prefix]
        rec["system"] = PREFIX_SYSTEM.get(prefix, rec.get("system"))


def extract(row: dict) -> dict:
    path = DATASET / row["file_path"]
    media_type = MEDIA_TYPES[path.suffix.lower()]
    data = path.read_bytes()
    block_type = "document" if media_type == "application/pdf" else "image"
    content = [
        {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": f"Artifact origin: {row['origin']}. Captured at: {row['captured_at']}. "
                                 "Extract the records."},
    ]
    key = llm.cache_key("media-v1", PROMPT, SCHEMA, data, row["origin"], row["captured_at"])
    return llm.structured_call(f"media:{row['file_id']}", PROMPT, content, SCHEMA, key)


def main() -> None:
    if not llm.available():
        sys.exit("No LLM credentials found: add OPENAI_API_KEY (or ANTHROPIC_API_KEY) to the uncommitted repo-root .env.")
    aliases = alias_map()
    MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
    for row in read_csv("evidence_files.csv"):
        if Path(row["file_path"]).suffix.lower() not in MEDIA_TYPES:
            continue
        result = extract(row)
        for rec in result["records"]:
            normalise_ids(rec, aliases)
        inherit_ids(result["records"])
        for rec in result["records"]:
            fix_type(rec)
            if rec.get("present") is None and rec.get("record_type"):
                rec.pop("present")  # let the loader infer from the other fields
        out = {"file_id": row["file_id"], "file_path": row["file_path"], **result}
        (MEDIA_CACHE / f"{row['file_id']}.json").write_text(
            json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        ids = [r.get("resource_id") for r in result["records"]]
        print(f"{row['file_id']} {row['file_path']}: {len(ids)} records {ids}")


if __name__ == "__main__":
    main()
