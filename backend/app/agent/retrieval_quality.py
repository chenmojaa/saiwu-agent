"""Retrieval quality helpers: query expansion + cross-encoder rerank."""
from __future__ import annotations

import json
import logging
import re

from app.config import settings

_log = logging.getLogger(__name__)

_EXPANSION_PROMPT = """你是查询扩展助手。把下面这个用户问题改写成 2-3 个语义等价但措辞不同的检索句,只输出 JSON 数组,不要任何其他文字:

用户问题: <<QUERY>>

输出形如: ["改写1", "改写2", "改写3"]
如果问题本身已经很明确,只输出 1 个原问题即可。
"""

_RERANK_PROMPT = """你是重排序助手。对每条候选文本,判断它对回答"<<QUERY>>">>这个问题的相关度(0=完全无关, 10=直接命中),只输出 JSON 数组,不要其他文字:

[
  {"id": "<id1>", "score": <0-10>},
  {"id": "<id2>", "score": <0-10>},
  ...
]

候选:
<<CANDIDATES>>
"""

_JSON_ARR_RE = re.compile(r"\[[\s\S]*\]")
_THINK_RE = re.compile(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", re.IGNORECASE)


def _strip_think(text):
    return _THINK_RE.sub("", text).strip()


def _cheap_model(api_key=None, base_url=None):
    """Build the router-quality model used for query expansion / rerank.

    Per-request ``api_key`` / ``base_url`` take precedence so callers can use
    their own provider creds for expansion + rerank instead of being silently
    pinned to ``settings.router_*`` (which the user may have set to a different
    provider entirely). Falls back to global settings when the caller did not
    supply overrides.
    """
    from app.llm.factory import _build_model
    return _build_model(
        provider=None,
        model=settings.router_model or None,
        api_key=api_key,
        base_url=base_url,
    )


def expand_query(query, max_variants=3, *, api_key=None, base_url=None):
    if not query or len(query.strip()) < 4:
        return [query]
    try:
        model = _cheap_model(api_key=api_key, base_url=base_url)
        prompt = _EXPANSION_PROMPT.replace("<<QUERY>>", query.strip())
        resp = model.invoke(prompt)
        content = getattr(resp, "content", "") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        text = _strip_think(str(content))
        m = _JSON_ARR_RE.search(text)
        if not m:
            return [query]
        arr = json.loads(m.group(0))
        if not isinstance(arr, list):
            return [query]
        out = [query]
        for v in arr:
            s = str(v).strip()
            if s and s not in out:
                out.append(s)
            if len(out) >= max_variants:
                break
        return out[:max_variants]
    except Exception as e:
        _log.warning("query expansion failed (ignored): %s", e)
        return [query]


def rerank(query, hits, top_n=None, *, api_key=None, base_url=None):
    if not hits:
        return hits
    try:
        if len(hits) <= 1:
            hits[0]["rerank_score"] = 10.0
            return hits
        capped = hits[: max(2, top_n or 10)]
        lines = []
        for i, h in enumerate(capped):
            snippet = (h.get("text") or "")[:300].replace(chr(10), " ")
            cid = "%s#%s" % (h.get('note_id','?'), h.get('chunk_index','?'))
            lines.append("[%d] id=%s text=%s" % (i, cid, snippet))
        prompt = _RERANK_PROMPT.replace("<<QUERY>>", query).replace("<<CANDIDATES>>", chr(10).join(lines))
        model = _cheap_model(api_key=api_key, base_url=base_url)
        resp = model.invoke(prompt)
        content = getattr(resp, "content", "") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        text = _strip_think(str(content))
        m = _JSON_ARR_RE.search(text)
        if not m:
            return hits
        arr = json.loads(m.group(0))
        if not isinstance(arr, list):
            return hits
        score_map = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            try:
                s = float(item.get("score") or 0)
            except Exception:
                continue
            score_map[str(item.get("id") or "").strip()] = max(0.0, min(10.0, s))
        for h in capped:
            cid = "%s#%s" % (h.get('note_id','?'), h.get('chunk_index','?'))
            h["rerank_score"] = score_map.get(cid, 5.0)
        capped.sort(key=lambda h: (h.get("rerank_score", 0), h.get("final_score", 0)), reverse=True)
        return capped
    except Exception as e:
        _log.warning("rerank failed (ignored): %s", e)
        return hits
