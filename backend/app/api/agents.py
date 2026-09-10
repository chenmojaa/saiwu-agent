"""Sub-agent routes.

POST /api/agents/run             -> SSE stream of one sub-agent run.
POST /api/agents/plan-suggest    -> one-shot call to the "plan" sub-agent
                                    which emits a structured JSON plan.

The frontend uses these from the chat header dropdown (when the user picks
"Use Explore sub-agent") and from the settings drawer (Plan Mode = on).

SSE event taxonomy:
  event: subagent
  data: {"phase": "started", "mode": "explore", ...}
        {"phase": "tool_call", "name": "hybrid_search", ...}
        {"phase": "text_delta", "text": "..."}
        {"phase": "done", "mode": "explore"}
        {"phase": "error", "detail": "..."}
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.subagents import (
    SUBAGENT_MODES, run_subagent_stream, parse_plan_payload,
)

_log = logging.getLogger(__name__)
router = APIRouter(tags=["agents"])


class RunRequest(BaseModel):
  mode: str = "general"
  query: str
  session_id: Optional[str] = None
  history: list[dict] | None = None
  api_key: Optional[str] = None
  base_url: Optional[str] = None


@router.post("/agents/run")
async def api_agents_run(body: RunRequest):
  """Stream a sub-agent run.

  The response is an SSE stream. Each event is named ``subagent`` with a JSON
  payload containing ``phase`` + mode-specific fields. The caller is expected
  to render the text-delta stream and react to ``done`` / ``error`` phases.
  """
  mode = (body.mode or "general").strip().lower()
  if mode not in SUBAGENT_MODES:
    mode = "general"
  query = (body.query or "").strip()
  if not query:
    return {"error": "query must be non-empty"}

  async def generate():
    yield "event: subagent\n"
    first = True
    async for ev in run_subagent_stream(
      mode=mode, query=query,
      api_key=body.api_key, base_url=body.base_url,
      history=body.history or [],
    ):
      # First event has phase=started; we surface that right away.
      yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
      if first:
        first = False
    yield "event: done\ndata: {}\n\n"
  return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/agents/plan-suggest")
async def api_agents_plan_suggest(body: RunRequest):
  """One-shot: ask the PLAN sub-agent to produce a JSON plan and return it.

  Returns:
      {"ok": bool, "plan": {"plan_summary", "steps": [...]}, "raw": str}

  On parse failure ``ok`` is false and ``raw`` contains the model's freeform
  text so the caller can decide whether to retry.
  """
  raw_chunks: list[str] = []
  async for ev in run_subagent_stream(
    mode="plan", query=body.query,
    api_key=body.api_key, base_url=body.base_url,
    history=body.history or [],
  ):
    if ev.get("phase") == "text_delta":
      raw_chunks.append(ev.get("text") or "")
  raw = "".join(raw_chunks).strip()
  plan = parse_plan_payload(raw)
  return {"ok": plan is not None, "plan": plan, "raw": raw[:1500]}
