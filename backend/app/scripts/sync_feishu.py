"""One-shot Feishu sync trigger.

The backend already runs a 15-minute background loop. This script lets the
user fire a single on-demand cycle from the UI without waiting for the next
tick.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("sync_feishu")


def _emit(phase, **kw):
    print(json.dumps({"phase": phase, **kw}, ensure_ascii=False), flush=True)


def main():
    _emit("started", script="sync_feishu")
    from app.config import settings
    if not settings.feishu_enabled:
        _emit("done", processed=0, skipped=0, reason="feishu disabled")
        return 0
    try:
        from app.feishu_sync import sync_all
    except Exception as e:
        _emit("error", detail="feishu_sync import failed: " + str(e)[:200])
        return 1
    try:
        result = sync_all()
        _emit("done", **result)
    except Exception as e:
        _emit("error", detail=str(e)[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
