"""Environment-backed configuration. No secret appears in source."""
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# A blank line in .env ("KEY=") reads as "", which str alone would accept.
Secret = Annotated[str, Field(min_length=1)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM — Nebius Token Factory, OpenAI-compatible. The same key/URL feed the
    # SGR agent, the catalog embedder and VoiceMem's ingest LLM (phase-2 D7).
    # OPENAI_* is accepted as an alias so an OpenAI-SDK-style .env keeps working.
    nebius_api_key: Secret = Field(
        validation_alias=AliasChoices("NEBIUS_API_KEY", "OPENAI_API_KEY"))
    nebius_base_url: str = Field(
        default="https://api.tokenfactory.nebius.com/v1/",
        validation_alias=AliasChoices("NEBIUS_BASE_URL", "OPENAI_BASE_URL"))
    # Instruct (non-thinking): 0.40 s median TTFT measured on Nebius, vs ~1.1 s
    # for DeepSeek-V4-Flash whose hidden reasoning precedes the first word.
    # Alternative turn brain: nvidia/Nemotron-3_5-Lightning (286 ms TTFT) with
    # LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}.
    llm_model: str = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    llm_extra_body: dict[str, Any] = {}

    # SLNG — one key covers STT and TTS
    slng_api_key: Secret
    # Regional routing is a header on the single gateway host; per-region
    # hostnames such as eu.api.slng.ai do not resolve.
    slng_base_url: str = "api.slng.ai"
    slng_world_part: str | None = "eu"
    slng_stt_model: str = "reson8/reson8stt:v1"
    slng_tts_model: str = "cartesia/sonic:3"
    slng_tts_voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02"
    slng_tts_encoding: str = "linear16"
    slng_tts_sample_rate: int = 24000

    # Anam
    anam_api_key: Secret
    anam_avatar_id: Secret
    anam_avatar_model: str = "cara-4"

    # Media — cara-4 portrait; width and height must be supplied together
    video_width: int = 768
    video_height: int = 1152
    target_fps: int = 25

    # Turn-taking — silence after the last word before the LLM runs
    turn_silence_s: float = 0.5

    control_token_ttl_s: int = 3600

    # Agent implementation: `sgr` = phase-2 SGR agent (memory + recs + TV
    # commands); `chat` = plain OpenAILLMService voice chat; `stub` = scripted
    # test double.
    agent_impl: Literal["stub", "chat", "sgr"] = "sgr"

    # Embeddings (catalog index + query) — the only embedding model on Nebius
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
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
