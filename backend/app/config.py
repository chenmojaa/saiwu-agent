from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
  )

  # ---- LLM ----
  llm_provider: str = "openai"
  llm_model: str = "gpt-4o-mini"
  llm_api_key: str = ""
  llm_api_base: str = ""

  # ---- Embedding ----
  embedding_provider: str = "openai"
  embedding_model: str = "text-embedding-3-small"
  embedding_api_key: str = ""
  embedding_api_base: str = "https://api.minimax.chat/v1"
  embedding_device: str = "cpu"

  # ---- Server ----
  host: str = "127.0.0.1"
  port: int = 8000
  log_level: str = "info"

  # ---- Storage (P2) ----
  data_dir: str = "./data"
  sqlite_path: str = "./data/notes.db"
  chroma_dir: str = "./data/chroma"
  notes_dir: str = "./data/notes"


  # ---- Agent tools (tool-calling for MCP + skills) ----
  tools_enabled: bool = True                       # HD_TOOLS_ENABLED; master switch for tool-calling
  tools_max_steps: int = 6                          # HD_TOOLS_MAX_STEPS; cap on tool-call iterations
  mcp_call_timeout_sec: float = 30.0                # HD_MCP_CALL_TIMEOUT; per-tool call budget
  mcp_init_timeout_sec: float = 10.0                # HD_MCP_INIT_TIMEOUT; per-server startup budget
  llm_max_retries: int = 3                          # HD_LLM_MAX_RETRIES; transient failures retry budget (0 = no retry)
  # ---- Feishu (Lark) sync ----
  feishu_enabled: bool = False
  feishu_app_id: str = ""
  feishu_app_secret: str = ""
  feishu_api_base: str = "https://open.feishu.cn"
  feishu_space_ids: str = ""          # comma-separated list; empty = all visible spaces
  feishu_sync_interval_min: int = 15   # minutes between sync runs
  feishu_page_size: int = 50
  feishu_web_url: str = ""             # for constructing view URLs e.g. https://{tenant}.feishu.cn
  custom_models_allow_private: bool = False       # HD_CUSTOM_MODELS_ALLOW_PRIVATE; allow RFC1918 / loopback in /settings/custom-models (for local Ollama)

  # ---- Agent / Router (OPTIMIZATION.md \u00a72.5) ----
  use_graph: bool = True                       # HD_USE_GRAPH; false = legacy direct-call path
  router_enabled: bool = True                  # HD_ROUTER_ENABLED; false = always intent=chat
  router_model: str = ""                       # empty = use main LLM model
  router_base_url: str = ""                    # empty = use main base_url
  research_max_iter: int = 3                   # HD_RESEARCH_MAX_ITER
  research_target_chunks: int = 8              # HD_RESEARCH_TARGET_CHUNKS
  planner_enabled: bool = True                  # HD_PLANNER_ENABLED; task planning before research
  planner_max_steps: int = 4                    # HD_PLANNER_MAX_STEPS; max sub-queries per plan
  parallel_plan_enabled: bool = True             # HD_PARALLEL_PLAN_ENABLED; run plan steps concurrently with asyncio.gather                    # HD_PLANNER_MAX_STEPS; max sub-queries per plan
  memory_extraction_enabled: bool = True        # HD_MEMORY_EXTRACTION_ENABLED; auto fact extraction after each turn
  memory_max_facts: int = 8                     # HD_MEMORY_MAX_FACTS; max recalled facts injected per turn
  ingest_provider: str = ""                    # empty = use main LLM for metadata extraction

  # ---- Ingestion ----
  chunk_size: int = 500
  chunk_overlap: int = 80


@lru_cache
def get_settings() -> Settings:
  return Settings()


settings = get_settings()
