"""Recompute embeddings for every note in the knowledge base (CLI entry).

Lightweight offline-equivalent of the agent's online ingest path: walk all
notes, re-vectorise them into the shared Chroma collection. Designed to be
spawned as a subprocess by /api/background/reindex so the chat UI does not
block.

Usage::

    python -m app.scripts.reindex_all [--scope all|notes|remote] [--root PATH]

Stdout is line-per-step so the SSE consumer can render a progress bar.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("reindex")


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def reindex_notes(scope: str = "all", root: str | None = None) -> None:
    from app.storage import db
    started = time.monotonic()
    notes = (db.list_all_notes() if hasattr(db, "list_all_notes") else []) or []
    total = len(notes)
    _emit(f"[start] scope={scope} total={total} root={root or '-'}")
    # Lazy imports so the script can be --help-ed without LLM deps installed.
    try:
        from app.config import settings
    except Exception as e:
        _emit(f"[fatal] cannot load settings: {e}")
        return
    if total == 0:
        _emit(f"[config] embedding_model={settings.embedding_model}")
        _emit("[done] elapsed_s=0 processed=0")
        return
    # Use the online ingest path: chunk + embed + add to chroma. This keeps
    # the offline CLI behaviour identical to what /api/chat would do.
    try:
        from app.tools.ingest import ingest_text_into_chroma
        from app.tools.chunk import chunk_text
    except Exception as e:
        _emit(f"[fatal] ingest pipeline unavailable: {e}")
        return
    ok = failed = 0
    for i, n in enumerate(notes, 1):
        nid = getattr(n, "id", None)
        body = (getattr(n, "text", None) or getattr(n, "content", None) or "")
        if not nid or not body:
            failed += 1
            _emit(f"[warn] {i}/{total} {nid}: empty body")
            continue
        try:
            chunks = chunk_text(body)
            ingest_text_into_chroma(
                note_id=nid,
                chunks=chunks,
                title=getattr(n, "title", None) or "",
                source_url=getattr(n, "source_url", None),
            )
            ok += 1
        except Exception as e:
            failed += 1
            _emit(f"[warn] {nid}: {type(e).__name__}: {e}")
        if i % 5 == 0 or i == total:
            _emit(f"[progress] {i}/{total} ok={ok} failed={failed}")
    elapsed = int(time.monotonic() - started)
    _emit(f"[done] elapsed_s={elapsed} processed={ok} failed={failed}")


def main() -> int:
    p = argparse.ArgumentParser(description="Re-vector every note in the KB.")
    p.add_argument("--scope", default="all", choices=["all", "notes", "remote"])
    p.add_argument("--root", default=None)
    args = p.parse_args()
    try:
        reindex_notes(args.scope, args.root)
    except Exception as e:
        _emit(f"[fatal] {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
