"""Notes REST API."""
from __future__ import annotations

from typing import Optional
from fastapi import Query,  APIRouter, HTTPException, UploadFile, File, Header, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlalchemy import text

from app.storage.db import Note, get_session
from app.tools.ingest import ingest_url, ingest_text, ingest_pdf, ingest_image, ingest_file, _ingest
from app.storage.vector import add_chunks, collection_stats, delete_note_chunks
from app.tools.chunk import chunk_text
from app.embeddings.factory import embed_texts

router = APIRouter(tags=["notes"])


class IngestURLRequest(BaseModel):
  url: str
  base_url: Optional[str] = Field(None, description="可选 embedding 接口 URL")
  embedding_model: Optional[str] = Field(None, description="可选 embedding 模型名 例如 embo-01")


class IngestTextRequest(BaseModel):
  text: str
  title: Optional[str] = None
  base_url: Optional[str] = Field(None, description="可选 embedding 接口 URL")
  embedding_model: Optional[str] = Field(None, description="可选 embedding 模型名 例如 embo-01")


def _to_dict(note: Note) -> dict:
  import re
  from app.storage.feishu_config_store import get_web_url

  view_url = None
  if note.source_type.startswith("feishu_") and note.source_url:
    web_base = get_web_url()
    if web_base:
      m = re.match(r"feishu://(\w+)/(\w+)/(\w+)", note.source_url)
      if m:
        scheme, space_id, token = m.group(1), m.group(2), m.group(3)
        web_base = web_base.rstrip("/")
        if scheme == "wiki":
          view_url = f"{web_base}/wiki/{token}"
        elif scheme == "bitable":
          view_url = f"{web_base}/base/{space_id}?table={token}"

  return {
    "id": note.id,
    "title": note.title,
    "source_type": note.source_type,
    "source_url": note.source_url,
    "content_path": note.content_path,
    "summary": note.summary,
    "tags": note.tags,
    "word_count": note.word_count,
    "chunk_count": note.chunk_count,
    "embedded": note.embedded,
    "created_at": note.created_at.isoformat() if note.created_at else None,
    "view_url": view_url,
  }


def _resolve_base_url(body_url: Optional[str], header_url: Optional[str]) -> Optional[str]:
  return (body_url or header_url or "").strip() or None


def _resolve_embedding_model(body_model: Optional[str], header_model: Optional[str]) -> Optional[str]:
  return (body_model or header_model or "").strip() or None


_ALLOWED_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".html", ".htm", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}

async def _safe_read_upload(file, max_bytes: int) -> bytes:
    """Read an UploadFile in chunks, refusing payloads > max_bytes."""
    total = 0
    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="file too large (>%d bytes)" % max_bytes)
        buf.extend(chunk)
    return bytes(buf)


def _check_ext(filename: str) -> None:
    from pathlib import Path
    ext = Path(filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail="unsupported file type: " + (ext or "<none>"))


@router.post("/notes/url")
async def api_ingest_url(
  body: IngestURLRequest,
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  try:
    note = ingest_url(
      body.url,
      api_key=x_api_key,
      base_url=_resolve_base_url(body.base_url, x_embedding_base_url),
      embedding_model=_resolve_embedding_model(body.embedding_model, x_embedding_model),
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))
  return _to_dict(note)


@router.post("/notes/text")
async def api_ingest_text(
  body: IngestTextRequest,
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  try:
    note = ingest_text(
      body.text,
      body.title,
      api_key=x_api_key,
      base_url=_resolve_base_url(body.base_url, x_embedding_base_url),
      embedding_model=_resolve_embedding_model(body.embedding_model, x_embedding_model),
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))
  return _to_dict(note)


@router.post("/notes/pdf")
async def api_ingest_pdf(
  file: UploadFile = File(...),
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  import os, tempfile
  from app.config import settings
  suffix = os.path.splitext(file.filename or "")[1] or ".pdf"
  name = file.filename or "upload.pdf"
  fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=settings.data_dir)
  os.close(fd)
  try:
    content = await file.read()
    with open(tmp_path, "wb") as f:
      f.write(content)
    note = ingest_pdf(
      tmp_path,
      api_key=x_api_key,
      base_url=_resolve_base_url(None, x_embedding_base_url),
      embedding_model=_resolve_embedding_model(None, x_embedding_model),
      original_name=name,
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))
  finally:
    try: os.unlink(tmp_path)
    except Exception: pass
  return _to_dict(note)




@router.post("/notes/image")
async def api_ingest_image(
  file: UploadFile = File(...),
  lang: str = "chi_sim+eng",
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  """Upload an image png/jpg/webp/bmp/tif. OCR via Tesseract."""
  import os, tempfile
  from app.config import settings
  suffix = os.path.splitext(file.filename or "")[1] or ".png"
  name = file.filename or "upload.png"
  fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=settings.data_dir)
  os.close(fd)
  try:
    content = await file.read()
    with open(tmp_path, "wb") as f:
      f.write(content)
    note = ingest_image(
      tmp_path,
      api_key=x_api_key,
      base_url=_resolve_base_url(None, x_embedding_base_url),
      lang=lang,
      embedding_model=_resolve_embedding_model(None, x_embedding_model),
      original_name=name,
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))
  finally:
    try: os.unlink(tmp_path)
    except Exception: pass
  return _to_dict(note)


@router.post("/notes/file")
async def api_ingest_file(
  file: UploadFile = File(...),
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  """Generic file upload: dispatches by extension pdf/docx/txt/md/image."""
  import os, tempfile
  from app.config import settings
  name = file.filename or "upload.bin"
  suffix = os.path.splitext(name)[1] or ""
  fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=settings.data_dir)
  os.close(fd)
  try:
    content = await file.read()
    with open(tmp_path, "wb") as f:
      f.write(content)
    note = ingest_file(
      tmp_path,
      original_name=name,
      api_key=x_api_key,
      base_url=_resolve_base_url(None, x_embedding_base_url),
      embedding_model=_resolve_embedding_model(None, x_embedding_model),
    )
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))
  finally:
    try: os.unlink(tmp_path)
    except Exception: pass
  return _to_dict(note)

@router.get("/notes")
async def api_list_notes(limit: int = Query(50, ge=1, le=500, description="1..500"), offset: int = 0):
  with get_session() as s:
    stmt = select(Note).order_by(Note.created_at.desc()).offset(offset).limit(limit)
    notes = s.exec(stmt).all()
    total = s.exec(text("SELECT COUNT(*) FROM notes")).scalar()
  return {"items": [_to_dict(n) for n in notes], "total": total, "limit": limit, "offset": offset}


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
  with get_session() as s:
    note = s.get(Note, note_id)
    if not note:
      raise HTTPException(status_code=404, detail="Note not found")
  return _to_dict(note)


@router.get("/notes/{note_id}/download")
async def api_download_note(note_id: str):
  import os
  from urllib.parse import quote
  with get_session() as s:
    note = s.get(Note, note_id)
    if not note:
      raise HTTPException(status_code=404, detail="Note not found")
    title = note.title or "note"
    content_path = note.content_path
  ascii_name = "".join(c for c in title if c.isalnum() or c in (" ", ".", "_", "-")).strip() or "note"
  quoted_name = quote(ascii_name, safe="-_.")
  if content_path and os.path.isfile(content_path):
    return FileResponse(
      path=content_path,
      filename=ascii_name + ".md",
      media_type="text/markdown; charset=utf-8",
      headers={"Content-Disposition": "attachment; filename=" + quoted_name + ".md"},
    )
  summary = (note.summary or "").encode("utf-8")
  data = (b"# " + title.encode("utf-8") + b"\n\n" + summary + b"\n")
  return Response(
    content=data,
    media_type="text/markdown; charset=utf-8",
    headers={"Content-Disposition": "attachment; filename=" + quoted_name + ".md"},
  )
@router.post("/notes/{note_id}/reembed")
async def api_reembed_note(
  note_id: str,
  x_api_key: str | None = Header(None, alias="X-API-Key"),
  x_embedding_base_url: str | None = Header(None, alias="X-Embedding-Base-URL"),
  x_embedding_model: str | None = Header(None, alias="X-Embedding-Model"),
):
  """Re-run embedding for an existing note."""
  import os
  with get_session() as s:
    note = s.get(Note, note_id)
    if not note:
      raise HTTPException(status_code=404, detail="Note not found")
    content_path = note.content_path
    title = note.title
    note_id_local = note.id

  if not content_path or not os.path.isfile(content_path):
    raise HTTPException(status_code=400, detail="Note content missing on disk")

  with open(content_path, "r", encoding="utf-8", errors="ignore") as f:
    content = f.read()

  # Re-embed ordering: produce the new chunks first, then evict the old ones.
  # Old code deleted first, which left a note with no retrievable body if
  # the embed request failed partway through.
  chunks = chunk_text(content)
  try:
    embeddings = embed_texts(
      chunks,
      api_key=x_api_key,
      base_url=_resolve_base_url(None, x_embedding_base_url),
      model=_resolve_embedding_model(None, x_embedding_model),
    )
    n = add_chunks(note_id_local, chunks, embeddings)
    # New vectors are in place; only now do we evict the old ones. The
    # add_chunks path uses the same note id, so old + new vectors coexist
    # briefly inside Chroma; we delete by id right after to keep tidy.
    # If this delete fails, worst case is duplicated retrieval, which is
    # recoverable by another reembed.
    delete_note_chunks(note_id_local)
    with get_session() as s:
      note = s.get(Note, note_id_local)
      if note:
        note.chunk_count = n
        note.embedded = True
        if chunks:
          note.summary = chunks[0][:200]
        s.add(note)
        s.commit()
        s.refresh(note)
        return _to_dict(note)
  except Exception as e:
    with get_session() as s:
      note = s.get(Note, note_id_local)
      if note:
        note.summary = f"[embedding failed] {type(e).__name__}: {e}"
        s.add(note)
        s.commit()
    raise HTTPException(status_code=400, detail=str(e))


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
  import os
  content_path = None
  with get_session() as s:
    note = s.get(Note, note_id)
    if not note:
      raise HTTPException(status_code=404, detail="Note not found")
    content_path = note.content_path
    s.delete(note)
    s.commit()
  chunks_deleted = delete_note_chunks(note_id)
  if content_path and os.path.isfile(content_path):
    try:
      os.remove(content_path)
    except OSError:
      pass
  gone = bool(content_path and not os.path.exists(content_path))
  return {"deleted": note_id, "chunks_deleted": chunks_deleted, "file_removed": gone}
@router.get("/notes-stats")
async def api_stats():
  with get_session() as s:
    total = s.exec(text("SELECT COUNT(*) FROM notes")).scalar()
    embedded = len(s.exec(select(Note).where(Note.embedded == True)).all())  # noqa: E712
  return {
    "sqlite": {"total_notes": total, "embedded_notes": embedded},
    "chroma": collection_stats(),
  }