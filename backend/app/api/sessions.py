"""Chat session REST API."""
from __future__ import annotations

from typing import Optional
from fastapi import Query, APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.storage.db import (
  list_sessions, get_session_with_messages, create_session,
  delete_session, rename_session,
  fork_session as _fork_session,
)

router = APIRouter(tags=["sessions"])


class CreateSessionRequest(BaseModel):
  title: Optional[str] = Field(None, description="可选标题，默认 新对话")


class RenameRequest(BaseModel):
  title: str = Field(..., min_length=1, max_length=100)


class ForkRequest(BaseModel):
  title: Optional[str] = None


@router.get("/sessions")
async def api_list_sessions(limit: int = Query(50, ge=1, le=200, description="1..200")):
  return {"items": list_sessions(limit=limit)}


@router.post("/sessions")
async def api_create_session(body: CreateSessionRequest = CreateSessionRequest()):
  s = create_session(body.title or "新对话")
  return {"id": s.id, "title": s.title, "created_at": s.created_at.isoformat()}


@router.get("/sessions/{session_id}")
async def api_get_session(session_id: str):
  data = get_session_with_messages(session_id)
  if not data:
    raise HTTPException(404, "Session not found")
  return data


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str):
  ok = delete_session(session_id)
  if not ok:
    raise HTTPException(404, "Session not found")
  return {"deleted": session_id}


@router.patch("/sessions/{session_id}")
async def api_rename_session(session_id: str, body: RenameRequest):
  ok = rename_session(session_id, body.title)
  if not ok:
    raise HTTPException(404, "Session not found")
  return {"renamed": session_id, "title": body.title}


@router.post("/sessions/{session_id}/fork")
async def api_fork_session(session_id: str, body: ForkRequest | None = None):
  """Clone an existing session + all of its messages into a new session.

  Returns the new session descriptor. The caller should switch the active
  session to the new id to start the branch.
  """
  payload = body.title if body else None
  result = _fork_session(session_id, payload)
  if not result:
    raise HTTPException(404, "Session not found")
  return result
