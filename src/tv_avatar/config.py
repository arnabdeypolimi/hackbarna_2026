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
    # SGR agent, the cloud embedder option and the memory summariser (phase-2 D7).
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
    # Thinking is always off: instruct models ignore the flag, reasoning models
    # (Nemotron, DeepSeek, Qwen-thinking) would otherwise spend seconds before
    # the first `say` byte and break the JSON envelope.
    llm_extra_body: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}

    # SLNG — one key covers STT and TTS
    slng_api_key: Secret
    # Regional routing is a header on the single gateway host; per-region
    # hostnames such as eu.api.slng.ai do not resolve.
    slng_base_url: str = "api.slng.ai"
    slng_world_part: str | None = "eu"
    # Deepgram Nova 3 hosted on SLNG (language comes from the init config; the plugin
    # docs' "slng/deepgram/nova:3-en" is rejected with 422). Alternative: reson8/reson8stt:v1.
    slng_stt_model: str = "deepgram/nova:3"
    slng_tts_model: str = "cartesia/sonic:3"
    slng_tts_encoding: str = "linear16"
    slng_tts_sample_rate: int = 24000

    # Anam. Avatar ids, models and voices live in avatars.yaml (tv_avatar.catalog).
    anam_api_key: Secret
    # Local overrides for the catalog's *default* avatar: Anam avatar ids are
    # account-scoped, so the shared yaml's id can be missing under your key
    # ("Avatar with id … does not exist"). A saved Persona from Anam Lab wins
    # over an avatar id. Other avatars in the catalog are unaffected.
    anam_avatar_id: str = ""
    anam_persona_id: str = ""

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

    # Embeddings (catalog index + query). `local` = multilingual-e5-small
    # in-process via sentence-transformers (~15 ms/query, 384 dims);
    # `nebius` = the cloud model below (200–500 ms — blew the tool budget in
    # the field). The index must be built with the same provider.
    embedding_provider: Literal["local", "nebius"] = "local"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_dimensions: int = 1024

    @property
    def effective_embedding_dimensions(self) -> int:
        from tv_avatar.e5 import E5_DIM
        return E5_DIM if self.embedding_provider == "local" else self.embedding_dimensions

    # Long-term memory: one LLM-written profile per user under memory_root,
    # rewritten from the session transcript when the session ends. Titles are
    # not part of it — history.db is the viewing log. Empty model = llm_model.
    memory_model: str = ""
    memory_profile_max_words: int = 200
    #: Also fold the pending transcript into the profile every N ingested turns
    #: (off the turn), so what the viewer said this session reaches the prompt
    #: before the session ends — the conversation window is only ~5 exchanges.
    #: 0 = only at session end.
    memory_refresh_every_turns: int = Field(default=6, ge=0)

    # Phase-2 local data — everything under data/ is git-ignored
    catalog_path: str = "data/catalog.parquet"
    qdrant_path: str = "data/qdrant_db"
    history_db_path: str = "data/history.db"
    memory_root: str = "data/memory"
    catalog_index_limit: int = 100_000

    # Turn behaviour
    mem_prefetch_min_chars: int = 6
    tool_timeout_s: float = 0.4
    #: SGR cycles per turn. Cycle 1 streams the envelope; each further cycle
    #: is a full LLM round trip that feeds tool results back. The last cycle
    #: refuses internal tools so the loop always ends in speech. 2 is the
    #: measured sweet spot on a voice budget; every extra cycle is ~1 s of
    #: waiting the viewer hears.
    agent_max_cycles: int = Field(default=2, ge=1, le=4)
    #: Every cycle after the first must produce its first `say` byte within this
    #: budget or the tool results are spoken from a template instead.
    cycle_first_byte_s: float = Field(
        default=1.2, validation_alias=AliasChoices("CYCLE_FIRST_BYTE_S", "CYCLE2_FIRST_BYTE_S"))
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
