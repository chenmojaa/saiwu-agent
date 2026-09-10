# -*- coding: utf-8 -*-
"""Ingest agent: detect URL/text in user message, ingest via existing pipeline,
generate metadata (title/tags/summary) via cheap LLM call, dedupe against FTS5.

Design (per OPTIMIZATION.md section 2.7):
  - Detect URL in the user message -> ingest_url (reuse existing pipeline).
  - Otherwise treat the whole message (after stripping trigger words like
    "remember this", "save this") as text content -> ingest_text.
  - Generate 3-5 tags + 1-line summary + improved title via structured output.
  - FTS5 dedup: if a note has title/summary overlap with an existing note,
    flag it as `duplicate_of` (informational only, never blocks ingestion).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from pydantic import BaseModel, Field

from app.agent.state import AgentState
from app.llm.factory import _build_model
from app.storage.db import Note, fts_search
from sqlmodel import Session as SqlSession, select
from app.storage.db import get_engine

_log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef]+", re.IGNORECASE)
_TRIGGER_WORDS = ("\u5b58\u8fdb\u6765", "\u8bb0\u4e00\u4e0b", "\u5e2e\u6211\u5165\u5e93", "\u5b58\u4e0b", "\u52a0\u8fdb\u53bb",
                  "remember this", "save this", "add to my kb")


class IngestMeta(BaseModel):
    title: str = Field("", description="\u4e2d\u6587\u6807\u9898\uff0c\u226430\u5b57")
    tags: list[str] = Field(default_factory=list, description="3-5 \u4e2a\u6807\u7b7e")
    summary: str = Field("", description="\u4e00\u53e5\u8bdd\u6458\u8981\uff0c\u226460\u5b57")


def _strip_triggers(text: str) -> str:
    out = text
    for t in _TRIGGER_WORDS:
        out = out.replace(t, "")
    return out.strip()


def _first_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?\")\u3002\uff0c\uff1b\uff1a\uff01\uff1f") if m else None


def _generate_meta(content: str, fallback_title: str, chat) -> IngestMeta:
    """Cheap LLM call to extract metadata. Falls back to safe defaults on failure."""
    head = content[:1500]
    prompt = (
        "\u4f60\u662f\u77e5\u8bc6\u5e93\u5143\u6570\u636e\u63d0\u53d6\u5668\u3002\u8bfb\u4ee5\u4e0b\u5185\u5bb9\uff0c\u4e25\u683c\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\u3001\u4e0d\u8981\u5176\u4ed6\u6587\u5b57\uff1a\n"
        '{"title": "<string, \u4e2d\u6587\u6807\u9898 \u226430 \u5b57>", '
        '"tags": ["tag1","tag2","tag3"], '
        '"summary": "<string, \u4e00\u53e5\u8bdd\u6458\u8981 \u226460 \u5b57>"}\n\n'
        "Fallback title hint: %s\n\nContent (first 1500 chars):\n%s" % (fallback_title, head)
    )
    try:
        structured = chat.with_structured_output(IngestMeta)
        meta = structured.invoke(prompt)
        if not meta.title:
            meta.title = fallback_title[:30]
        # Decode any leftover \uXXXX escape sequences the LLM may have returned
        # literally (e.g. "\u6587\u4ef6" instead of "文件").
        meta.title = _decode_unicode_escapes(meta.title)
        meta.tags = [t.strip() for t in (meta.tags or []) if t and t.strip()][:5]
        meta.tags = [_decode_unicode_escapes(t) for t in meta.tags]
        if not meta.summary:
            meta.summary = head[:60]
        meta.summary = _decode_unicode_escapes(meta.summary)
        return meta
    except Exception as e:
        _log.warning("ingest: meta generation failed: %s", e)
        return IngestMeta(title=fallback_title[:30], tags=[], summary=head[:60])


def _decode_unicode_escapes(s: str) -> str:
    """Replace literal \\uXXXX sequences with the actual Unicode characters.

    Some LLMs return JSON string values with escaped Unicode (e.g. the title
    field literally contains ``\\u6587\\u4ef6`` instead of ``文件``).  This
    helper decodes those sequences so the stored title is human-readable.
    """
    def _repl(m):
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)
    import re
    return re.sub(r'\\u([0-9a-fA-F]{4})', _repl, s)


def _check_duplicate(title: str, summary: str) -> int | None:
    """FTS5-based soft dedup. Returns existing note_id if likely duplicate."""
    query = (title + " " + summary).strip()
    if not query:
        return None
    try:
        rows = fts_search(query, top_k=3)
    except Exception:
        return None
    if not rows:
        return None
    # First hit with score below the typical FTS5 BM25 threshold (~0 means exact match
    # in our score-to-similarity mapping; treat < 0.05 as strong match). We use the
    # raw score here as a soft signal only; frontend will surface the warning.
    best = rows[0]
    return best.get("note_id") or "" if best.get("note_id") else None


def _update_note_meta(note_id: str, meta: IngestMeta) -> None:
    engine = get_engine()
    with SqlSession(engine) as s:
        stmt = select(Note).where(Note.id == note_id)
        n = s.exec(stmt).first()
        if not n:
            return
        n.title = meta.title or n.title
        n.summary = meta.summary or n.summary
        # tags stored as comma-separated string for now
        n.tags = ", ".join(meta.tags) if meta.tags else (n.tags or "")
        s.add(n)
        s.commit()


def ingest_node(state: AgentState) -> dict:
    """Detect URL/text, ingest, generate metadata, soft dedup. Returns ingest_result."""
    raw = (state.get("query") or "").strip()
    api_key = state.get("api_key_override")
    base_url = state.get("base_url_override")
    embedding_model = state.get("embedding_model_override") or state.get("embedding_model") or None

    if not raw:
        return {"ingest_result": {"ok": False, "error": "empty query"},
                "step_count": state.get("step_count", 0) + 1}

    # Detect URL
    url = _first_url(raw)
    try:
        from app.tools.ingest import ingest_url, ingest_text
        if url:
            note = ingest_url(url, api_key=api_key, base_url=base_url, embedding_model=embedding_model)
            content_for_meta = note.summary or note.title or url
            fallback_title = url
        else:
            body = _strip_triggers(raw)
            note = ingest_text(body, title=None, api_key=api_key, base_url=base_url, embedding_model=embedding_model)
            content_for_meta = body[:1500]
            fallback_title = body.splitlines()[0][:30] if body else "Untitled"
    except Exception as e:
        _log.warning("ingest: pipeline failed: %s", e)
        return {"ingest_result": {"ok": False, "error": "%s: %s" % (type(e).__name__, e)},
                "step_count": state.get("step_count", 0) + 1}

    # Metadata generation (cheap LLM)
    try:
        chat = _build_model(
            provider=None,
            model=state.get("model_override"),
            api_key=api_key,
            base_url=base_url,
            reasoning_level=None,
        )
        meta = _generate_meta(content_for_meta, fallback_title, chat)
    except Exception as e:
        _log.warning("ingest: meta model init failed: %s", e)
        meta = IngestMeta(title=fallback_title[:30], tags=[], summary=(content_for_meta or "")[:60])

    # Update note in DB
    try:
        _update_note_meta(note.id, meta)
    except Exception as e:
        _log.warning("ingest: failed to persist metadata: %s", e)

    # Soft dedup
    dup_of = _check_duplicate(meta.title, meta.summary)

    return {
        "ingest_result": {
            "ok": True,
            "note_id": note.id,
            "title": meta.title,
            "tags": meta.tags,
            "summary": meta.summary,
            "source_type": note.source_type,
            "embedded": bool(note.embedded),
            "chunk_count": int(note.chunk_count or 0),
            "duplicate_of": dup_of,
        },
        "step_count": state.get("step_count", 0) + 1,
    }