"""Settings API: list available providers, auto-detect custom model lists."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import httpx

from app.tools.fetch_url import check_url

from app.llm.factory import list_providers
from app.storage import llm_config_store as _lcs

router = APIRouter(tags=["settings"])


class LlmConfigRequest(BaseModel):
  api_key: str | None = None
  base_url: str | None = None


@router.get("/settings/llm-config")
def get_llm_config():
  """Return the server-side stored LLM config (key masked)."""
  return _lcs.get_config()


@router.post("/settings/llm-config")
def set_llm_config(body: LlmConfigRequest):
  """Persist the user's API key/base_url server-side.

  Needed so background jobs (Feishu auto-sync re-vectorization) can embed
  without a live request carrying the X-API-Key header.
  """
  patch = {}
  if body.api_key:
    patch["api_key"] = body.api_key
  if body.base_url is not None:
    patch["base_url"] = body.base_url
  return _lcs.update_config(patch)


@router.get("/settings/models")
async def list_models():
  from app.config import settings
  return {
    "providers": list_providers(),
    "current": {
      "llm_provider": settings.llm_provider,
      "llm_model": settings.llm_model,
      "llm_api_base": settings.llm_api_base,
      "embedding_provider": settings.embedding_provider,
      "embedding_model": settings.embedding_model,
    },
  }


class CustomModelsRequest(BaseModel):
  """Auto-detect models exposed by an OpenAI-compatible endpoint."""
  base_url: str = Field(..., description="e.g. https://api.openai.com/v1")
  api_key: str = Field(..., description="Bearer token")


# Hard cap on the upstream /models response. Hundreds of model ids fit in
# well under a megabyte; anything bigger is almost certainly a misconfigured
# endpoint (or a SSRF probe) and would risk OOM if we let httpx buffer it.
_CUSTOM_MODELS_MAX_BYTES = 5 * 1024 * 1024


@router.post("/settings/custom-models")
async def custom_models(body: CustomModelsRequest):
  """Call <base_url>/models with Authorization Bearer <api_key>, return id list."""
  from app.config import settings as _settings
  base = body.base_url.rstrip("/")
  url = base + "/models"
  # SSRF guard. link-local / cloud-metadata / multicast are ALWAYS rejected;
  # RFC1918 + loopback require HD_CUSTOM_MODELS_ALLOW_PRIVATE=true so local
  # Ollama works without exposing internal infra to every web caller.
  try:
    check_url(url, allow_private=_settings.custom_models_allow_private)
  except ValueError as e:
    raise HTTPException(status_code=400, detail=f"base_url 被拒绝：{e}")
  headers = {"Authorization": f"Bearer {body.api_key}"}
  try:
    async with httpx.AsyncClient(timeout=15.0) as cli:
      async with cli.stream("GET", url, headers=headers) as r:
        if r.status_code != 200:
          # Drain up to 2 KiB for the error message without unbounded reads.
          err_body = b""
          async for chunk in r.aiter_bytes():
            err_body += chunk
            if len(err_body) > 2048:
              break
          raise HTTPException(status_code=400, detail=f"上游返回 {r.status_code}: {err_body[:200].decode('utf-8', 'replace')}")
        # Bounded read so a 1 GB JSON cannot OOM us.
        buf = bytearray()
        async for chunk in r.aiter_bytes():
          buf.extend(chunk)
          if len(buf) > _CUSTOM_MODELS_MAX_BYTES:
            raise HTTPException(
              status_code=400,
              detail=f"上游响应超过 {_CUSTOM_MODELS_MAX_BYTES} 字节，已中止",
            )
        body_bytes = bytes(buf)
  except HTTPException:
    raise
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"网络错误：{e}")
  try:
    import json as _json
    j = _json.loads(body_bytes.decode("utf-8", "replace"))
  except Exception:
    raise HTTPException(status_code=400, detail="上游响应不是 JSON")
  data = j.get("data") or j.get("models") or []
  ids: list[str] = []
  for m in data:
    if isinstance(m, dict):
      mid = m.get("id") or m.get("name")
      if mid: ids.append(str(mid))
    elif isinstance(m, str):
      ids.append(m)
  if not ids:
    raise HTTPException(status_code=400, detail="未识别到任何模型")
  # 推断 provider：根据 base_url 关键字
  provider = "openai"
  b = body.base_url.lower()
  if "deepseek" in b: provider = "deepseek"
  elif "zhipu" in b or "bigmodel" in b: provider = "zhipu"
  elif "moonshot" in b: provider = "moonshot"
  elif "siliconflow" in b: provider = "siliconflow"
  elif "ollama" in b or "11434" in b: provider = "ollama"
  return {"provider": provider, "base_url": base, "models": ids}

@router.get("/ocr-status")
def ocr_status_endpoint():
  """OCR availability for the settings UI (helps users install chi_sim)."""
  from app.tools.ocr import ocr_status
  return ocr_status()
