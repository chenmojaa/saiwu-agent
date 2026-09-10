"""Embedding factory - OpenAI-compatible /v1/embeddings via httpx.

No `openai` SDK dependency. Handles non-standard response envelopes (MiniMax `base_resp`)
and three common embedding response shapes.
"""
from __future__ import annotations

import httpx
from app.config import settings

OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"


def _resolve_url(base_url):
  if base_url:
    return base_url.rstrip("/") + "/embeddings"
  # Background jobs (Feishu auto-sync) pass no base_url; fall back to the
  # user-saved server-side value, then env vars.
  from app.storage import llm_config_store as _lcs
  fallback = (
    (settings.embedding_api_base or "").strip()
    or _lcs.get_base_url()
    or (settings.llm_api_base or "").strip()
    or OPENAI_DEFAULT_BASE
  )
  return fallback.rstrip("/") + "/embeddings"


def _resolve_key(api_key):
  # Request-provided key wins (masked echoes from /api/custom-models contain
  # '*' and are ignored); then env; then the server-side stored key so
  # background re-vectorization (no request context) still has credentials.
  from app.storage import llm_config_store as _lcs
  key = (api_key or "").strip()
  if key and "*" not in key:
    return key
  return (settings.embedding_api_key or settings.llm_api_key or _lcs.get_api_key() or "").strip()


def _resolve_model(model, base_url=None):
  m = (model or settings.embedding_model or "").strip()
  # MiniMax native endpoint speaks its own embedding model, not OpenAI's.
  # If the caller didn't pin one and we're pointed at minimax, fall back
  # to embo-01 instead of "text-embedding-3-small" (which 2013s upstream).
  if not model and base_url and "minimax" in base_url.lower() and m.startswith("text-embedding"):
    return "embo-01"
  return m


def _parse_embedding_response(j):
  """Parse an embedding response into a list[list[float]].

  Supports:
  - {"data": [{"embedding": [...]}]}    OpenAI / MiniMax
  - {"data": [[...]]}                   some providers
  - {"embeddings": [[...]]}             some providers

  Raises on upstream error envelope:
  - {"base_resp": {"status_code": 1004, "status_msg": "..."}}   MiniMax / Volcengine
  - {"error": {...}}                                            OpenAI error style
  """
  # If j is a bare list, that itself is the embeddings array.
  if isinstance(j, list):
    raw = j
  elif isinstance(j, dict):
    base = j.get("base_resp") if isinstance(j.get("base_resp"), dict) else None
    if base and base.get("status_code") not in (None, 0, "0"):
      _sc = base.get("status_code")
      _sm = base.get("status_msg") or j
      raise RuntimeError("Embedding upstream error (status_code=" + str(_sc) + "): " + str(_sm))
    if isinstance(j.get("error"), dict):
      raise RuntimeError("Embedding upstream error: " + str(j["error"]))
    raw = j.get("vectors") or j.get("embeddings") or j.get("data")
  else:
    raw = None
  if raw is None:
    raise RuntimeError("Embedding response missing vectors/embeddings/data: " + str(j))

  vecs = []
  order = []
  for i, item in enumerate(raw):
    if isinstance(item, dict):
      emb = (item.get("embedding") or item.get("vectors") or item.get("vector") or item.get("data"))
      if emb is None:
        raise RuntimeError(f"Embedding item {i} missing 'embedding': {item}")
      vecs.append(emb)
      order.append(item.get("index", i))
    elif isinstance(item, list):
      vecs.append(item)
      order.append(i)
    else:
      raise RuntimeError(f"Embedding item {i} unexpected type: {type(item).__name__}")

  pairs = sorted(zip(order, vecs), key=lambda p: p[0])
  return [v for _, v in pairs]



def _build_body(inputs, model, base_url, mode="db"):
  """Provider-aware /v1/embeddings request body.

  OpenAI / standard:    {"input": [...], "model": "..."}
  MiniMax native:      {"type": "<mode>", "texts": [...], "model": "..."}

  mode: "db" for ingestion (storing), "query" for retrieval.
  MiniMax uses different embedding for indexing vs querying.
  """
  b = (base_url or "").lower()
  if "minimax" in b:
    return {"model": model, "type": mode, "texts": list(inputs)}
  return {"model": model, "input": list(inputs)}

# Conservative batch size for MiniMax endpoints.
# Long empty / whitespace-only inputs can also trigger 2013 invalid_params upstream.
EMBED_BATCH = 32
# Per-item char cap to dodge upstream token-length limits.
MAX_CHARS_PER_ITEM = 1500


def _sanitize(texts):
  """Strip empties + clamp item length to MAX_CHARS_PER_ITEM.

  Truncation tries to land at a sentence/paragraph boundary to keep semantics.
  """
  if not texts:
    return []
  out = []
  for t in texts:
    if not isinstance(t, str):
      continue
    s = t.strip()
    if not s:
      continue
    if len(s) > MAX_CHARS_PER_ITEM:
      cut = s[:MAX_CHARS_PER_ITEM]
      for sep in ("\n\n", "。", "!", "?", " ", "\n"):
        idx = cut.rfind(sep)
        if idx > MAX_CHARS_PER_ITEM // 2:
          cut = cut[:idx]
          break
      s = cut.strip()
      if not s:
        continue
    out.append(s)
  return out


def embed_texts(texts, api_key=None, base_url=None, model=None, mode="db"):
  """Embed a list of strings via an OpenAI-compatible /v1/embeddings endpoint."""
  if not texts:
    return []

  cleaned = _sanitize(texts)
  if not cleaned:
    return []

  url = _resolve_url(base_url)
  key = _resolve_key(api_key)
  if not key:
    raise RuntimeError("No embedding API key configured (set EMBEDDING_API_KEY or pass api_key=)")

  m = _resolve_model(model, url)
  if not m:
    raise RuntimeError("No embedding model configured (set EMBEDDING_MODEL or pass model=)")

  headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json",
  }

  all_vecs = []
  with httpx.Client(timeout=60.0) as cli:
    for i in range(0, len(cleaned), EMBED_BATCH):
      chunk = cleaned[i:i + EMBED_BATCH]
      # Provider detection must use the RESOLVED url, not the raw base_url
      # param: when base_url is None the fallback chain in _resolve_url may
      # still land on MiniMax, which requires {"texts": ...} instead of
      # OpenAI-style {"input": ...} (upstream 2013 otherwise).
      body = _build_body(chunk, m, url, mode=mode)
      r = cli.post(url, headers=headers, json=body)
      if r.status_code != 200:
        # Surface upstream status_msg when present.
        try:
          j = r.json()
          base_resp = j.get("base_resp") or {}
          msg = base_resp.get("status_msg") or j.get("error") or j
        except Exception:
          msg = r.text
        raise RuntimeError(
          f"Embedding endpoint {url} returned {r.status_code} "
          f"(model={m}, batch={len(chunk)}, mode={mode}): {msg}"
        )
      try:
        j = r.json()
      except Exception as e:
        raise RuntimeError(f"Embedding response is not JSON: {e}; body={r.text[:200]}")
      all_vecs.extend(_parse_embedding_response(j))
  return all_vecs
