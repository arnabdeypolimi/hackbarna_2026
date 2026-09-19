"""Environment-backed configuration. No secret appears in source."""
from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
