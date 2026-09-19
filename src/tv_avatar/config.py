"""Environment-backed configuration. No secret appears in source."""
from functools import lru_cache
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# A blank line in .env ("KEY=") reads as "", which str alone would accept.
Secret = Annotated[str, Field(min_length=1)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM — Nebius Token Factory, OpenAI-compatible
    nebius_api_key: Secret
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    # Instruct (non-thinking): 0.40 s median TTFT measured on Nebius, vs ~1.1 s
    # for DeepSeek-V4-Flash whose hidden reasoning precedes the first word.
    llm_model: str = "Qwen/Qwen3-30B-A3B-Instruct-2507"

    # SLNG — one key covers STT and TTS
    slng_api_key: Secret
    # Regional routing is a header on the single gateway host; per-region
    # hostnames such as eu.api.slng.ai do not resolve.
    slng_base_url: str = "api.slng.ai"
    slng_world_part: str | None = "eu"
    slng_stt_model: str = "reson8/reson8stt:v1"
    slng_tts_model: str = "cartesia/sonic:3"
    slng_tts_encoding: str = "linear16"
    slng_tts_sample_rate: int = 24000

    # Anam. Avatar ids, models and voices live in avatars.yaml (tv_avatar.catalog).
    anam_api_key: Secret

    # Media — cara-4 portrait; width and height must be supplied together
    video_width: int = 768
    video_height: int = 1152
    target_fps: int = 25

    # Turn-taking — silence after the last word before the LLM runs
    turn_silence_s: float = 0.5

    control_token_ttl_s: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
