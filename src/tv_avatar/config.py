"""Environment-backed configuration. No secret appears in source."""
from functools import lru_cache
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SLNG — one key covers STT and TTS
    slng_api_key: str
    slng_base_url: str = "eu.api.slng.ai"
    slng_stt_model: str = "slng/deepgram/nova:3-en"
    slng_tts_model: str = "slng/deepgram/aura:2-en"
    slng_tts_voice: str = "aura-2-thalia-en"

    # Anam
    anam_api_key: str
    anam_avatar_id: str
    anam_avatar_model: str = "cara-4"

    # Media — cara-4 portrait; width and height must be supplied together
    video_width: int = 768
    video_height: int = 1152
    target_fps: int = 25

    control_token_ttl_s: int = 3600

    # LLM provider (phase 2, D7) — OpenAI-compatible; the same two variables are
    # read directly by VoiceMem/mem0, so this layer only reads them, never sets them.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    llm_model: str = "nvidia/Nemotron-3_5-Lightning"
    llm_extra_body: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_dimensions: int = 1024

    # Phase-2 local data — everything under data/ is git-ignored
    voicemem_local_models_dir: str = "data/voicemem_models"
    voicemem_chat_model: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    catalog_path: str = "data/catalog.parquet"
    qdrant_path: str = "data/qdrant_db"
    history_db_path: str = "data/history.db"
    memory_root: str = "data/voicemem"
    catalog_index_limit: int = 100_000

    # Turn behaviour
    mem_prefetch_min_chars: int = 6
    tool_timeout_s: float = 0.4
    agent_impl: Literal["stub", "sgr"] = "stub"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
