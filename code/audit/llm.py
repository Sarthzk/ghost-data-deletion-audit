"""Thin, cached, usage-tracked LLM wrapper with two providers.

* Provider: OpenAI (OPENAI_API_KEY) or Anthropic (ANTHROPIC_API_KEY), chosen by
  AUDIT_PROVIDER or by whichever key is present (OpenAI first). Model override:
  OPENAI_MODEL / ANTHROPIC_MODEL.
* Callers build content in one neutral shape (Anthropic-style text / image /
  document blocks); it is translated per provider here.
* Every response is cached on disk keyed by a hash of (provider, model, prompt,
  inputs), so a re-run of the pipeline makes zero model calls and is deterministic.
* Every real call appends one line to code/llm_cache/usage_log.jsonl
  (provider, model, purpose, tokens) - the source of usage_report.md.
* Credentials come from the environment or an uncommitted repo-root .env file.
  Nothing about the key itself is ever logged or written.
* When no credentials are available, `available()` is False and callers fall
  back to their deterministic rule-based path.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "code" / "llm_cache"
USAGE_LOG = CACHE_DIR / "usage_log.jsonl"
DEFAULT_MODELS = {"openai": "gpt-5-mini", "anthropic": "claude-opus-5-5"}
EFFORT = os.environ.get("AUDIT_EFFORT", "low")  # extraction/classification, not deep reasoning

_clients: dict = {}


def _load_dotenv() -> None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


def provider() -> str | None:
    _load_dotenv()
    if os.environ.get("AUDIT_OFFLINE") == "1":
        return None
    forced = os.environ.get("AUDIT_PROVIDER")
    has = {"openai": bool(os.environ.get("OPENAI_API_KEY")),
           "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))}
    for name in ([forced] if forced else ["openai", "anthropic"]):
        if has.get(name):
            return name
    return None


LAST_USED = CACHE_DIR / "last_used_model.json"


def _key_identity() -> tuple[str, str]:
    """(provider, model) for cache keys. Without credentials, reuse the pair that
    produced the committed cache so a key-less re-run reads the same answers."""
    p = provider()
    if p is None and LAST_USED.exists():
        last = json.loads(LAST_USED.read_text(encoding="utf-8"))
        return last["provider"], last["model"]
    return p or "offline", model()


def model() -> str:
    p = provider()
    if p is None and LAST_USED.exists():
        return json.loads(LAST_USED.read_text(encoding="utf-8"))["model"]
    p = p or "openai"
    return os.environ.get(f"{p.upper()}_MODEL", DEFAULT_MODELS[p])


def available() -> bool:
    p = provider()
    if p is None:
        return False
    try:
        __import__("openai" if p == "openai" else "anthropic")
    except ImportError:
        return False
    return True


def _client(p: str):
    if p not in _clients:
        if p == "openai":
            import openai

            _clients[p] = openai.OpenAI(max_retries=4)
        else:
            import anthropic

            _clients[p] = anthropic.Anthropic(max_retries=4)
    return _clients[p]


def cache_key(*parts) -> str:
    h = hashlib.sha256()
    for part in _key_identity() + parts:
        h.update(part if isinstance(part, bytes) else json.dumps(part, sort_keys=True, ensure_ascii=False).encode())
        h.update(b"\x00")
    return h.hexdigest()[:24]


def cached(key: str):
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _record_usage(purpose: str, p: str, model_name: str, tin: int, tout: int, cw: int, cr: int) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "purpose": purpose,
           "provider": p, "model": model_name, "input_tokens": tin, "output_tokens": tout,
           "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------- providers

def _openai_content(content: list) -> list:
    out = []
    for i, block in enumerate(content):
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            src = block["source"]
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}", "detail": "high"}})
        elif block["type"] == "document":
            src = block["source"]
            out.append({"type": "file", "file": {"filename": f"document_{i}.pdf",
                                                 "file_data": f"data:{src['media_type']};base64,{src['data']}"}})
    return out


def _call_openai(purpose, system, content, schema, max_tokens) -> str:
    m = model()
    params = dict(
        model=m,
        max_completion_tokens=max_tokens,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": _openai_content(content)}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "audit_output", "schema": schema, "strict": True}},
    )
    if m.startswith(("gpt-5", "o")):
        params["reasoning_effort"] = EFFORT
    resp = _client("openai").chat.completions.create(**params)
    u = resp.usage
    cached_in = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    _record_usage(purpose, "openai", resp.model, u.prompt_tokens - cached_in, u.completion_tokens, 0, cached_in)
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError(f"{purpose}: model declined: {choice.message.refusal}")
    if choice.finish_reason == "length":
        raise RuntimeError(f"{purpose}: output truncated at max_completion_tokens")
    return choice.message.content


def _call_anthropic(purpose, system, content, schema, max_tokens) -> str:
    import anthropic

    params = dict(model=model(), max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": content}],
                  output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}})
    try:
        # Server-side refusal fallback (routes a policy decline to another model).
        resp = _client("anthropic").beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    except anthropic.BadRequestError:
        resp = _client("anthropic").messages.create(**params)
    u = resp.usage
    _record_usage(purpose, "anthropic", resp.model, u.input_tokens, u.output_tokens,
                  getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"{purpose}: model declined (request id {resp._request_id})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"{purpose}: output truncated at max_tokens")
    return next(b.text for b in resp.content if b.type == "text")


def structured_call(purpose: str, system: str, content: list, schema: dict, key: str,
                    max_tokens: int = 16000) -> dict:
    """One JSON-schema-constrained call, cached under `key`."""
    hit = cached(key)
    if hit is not None:
        return hit
    p = provider()
    if p is None or not available():
        raise LookupError(f"{purpose}: not cached and no LLM credentials")
    text = (_call_openai if p == "openai" else _call_anthropic)(purpose, system, content, schema, max_tokens)
    LAST_USED.write_text(json.dumps({"provider": p, "model": model()}), encoding="utf-8")
    data = json.loads(text)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data
