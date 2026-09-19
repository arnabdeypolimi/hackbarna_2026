"""VoiceMem in text mode (D6): one VoiceMem per user_id, lazily built.

Read path is local (E5-small embedding + slot classifier, CPU pinned to two
threads — measured faster than all-threads on the dev Mac). Ingest is a cloud
LLM call on VOICEMEM_CHAT_MODEL and runs on its own single-worker executor so
a slow write can never starve `to_thread`'s shared pool during a prefetch.

Verified on voicemem 0.2.3: `VoiceMem.from_config` exists; `mode="leftbrain_only"`
accepts plain-text ingest (the default text_mode also runs the rightbrain, which
costs seconds per search for output we do not use);
`warmup(audio=False)` loads only E5 (no ASR/VAD/perception models); E5 falls
back to the HF id `intfloat/multilingual-e5-small` and downloads on first
use when no `VOICEMEM_MODELS_DIR/embedding` dir exists.
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from tv_avatar.config import Settings
from tv_avatar.memory.lane import BaseMemoryLane, MemoryBlock

E5_THREADS = 2
# VoiceMem's ingest-side graph/slot layer embeds through the OpenAI SDK without
# `dimensions=`, so it gets the model's native width: 4096 for Qwen3-Embedding-8B.
NEBIUS_EMBED_NATIVE_DIM = 4096


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff" for ch in text)


def _configure_env(settings: Settings) -> None:
    models_dir = Path(settings.voicemem_local_models_dir)
    if models_dir.is_dir():
        os.environ.setdefault("VOICEMEM_MODELS_DIR", str(models_dir.resolve()))
    else:
        logger.info(
            "VoiceMem local models dir missing; E5 will come from the HF cache "
            "(optional: hf download zhifeixie/VoiceMem_Default_Models_Env --local-dir {})",
            models_dir,
        )
    os.environ.setdefault("VOICEMEM_VERBOSE", "0")
    # Off-turn cloud embeddings (graph entity layer, slot naming) — Nebius, not OpenAI.
    os.environ.setdefault("OPENAI_EMBEDDING_MODEL", settings.embedding_model)
    os.environ.setdefault("VOICEMEM_EMBED_DIM", str(NEBIUS_EMBED_NATIVE_DIM))


def _pin_torch_threads() -> None:
    try:
        import torch
        torch.set_num_threads(E5_THREADS)
    except Exception as err:  # noqa: BLE001 — torch is a voicemem dependency; log, don't fail
        logger.warning("could not pin torch threads: {}", type(err).__name__)


class VoiceMemLane(BaseMemoryLane):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._vms: dict[str, Any] = {}
        self._ingest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicemem-ingest")
        self._lock = asyncio.Lock()
        _configure_env(settings)

    def _make_vm(self, user_id: str):
        from voicemem import VoiceMem
        root = Path(self._settings.memory_root) / user_id
        root.mkdir(parents=True, exist_ok=True)
        return VoiceMem.from_config({
            "api_key": self._settings.nebius_api_key,
            "base_url": self._settings.nebius_base_url,
            # leftbrain_only: facts/preferences only. The right brain (persona/
            # affect) ran cloud LLM calls inside every search — 300–2150 ms vs
            # 17–66 ms measured — and wrote Chinese coaching notes we discarded.
            "mode": "leftbrain_only",
            "user_id": user_id,
            "memory_root": str(root),
            "top_k": 5,
            "embedding": {"provider": "local"},   # multilingual-e5-small, CPU — read path stays local
            "slots": {"provider": "local"},       # shares the E5 instance; no LLM hop
            # ingest / extraction only — off the turn. api_key/base_url here make
            # VoiceMem export OPENAI_API_KEY/OPENAI_BASE_URL itself, so every one
            # of its internal OpenAI clients targets Nebius even when .env was
            # not exported into the process environment.
            "llm": {"provider": "openai",
                    "config": {"model": self._settings.voicemem_chat_model,
                               "api_key": self._settings.nebius_api_key,
                               "base_url": self._settings.nebius_base_url}},
        })

    async def _vm_for(self, user_id: str):
        if user_id in self._vms:
            return self._vms[user_id]
        async with self._lock:
            if user_id not in self._vms:
                _pin_torch_threads()
                self._vms[user_id] = await asyncio.to_thread(self._make_vm, user_id)
                logger.bind(user_id=user_id).info("voicemem space opened")
        return self._vms[user_id]

    async def warmup(self) -> None:
        """Load E5 once per process. Called from create_task at app start."""
        def load() -> None:
            _pin_torch_threads()
            from voicemem.leftbrain.local_e5_embedder import shared_e5
            shared_e5()
        await asyncio.to_thread(load)
        logger.info("voicemem E5 warm")

    async def _search(self, user_id: str, text: str) -> MemoryBlock:
        vm = await self._vm_for(user_id)
        result = await asyncio.to_thread(vm.search, text)
        left = list(getattr(result, "result_leftbrain", []) or [])
        # The right brain writes its experience notes in Chinese ("有效方式：…");
        # they are VoiceMem-internal coaching, not something to tell an English LLM.
        right = [line for line in (getattr(result, "result_rightbrain", []) or []) if not _has_cjk(line)]
        directive = (getattr(result, "rb_directive", "") or "").strip()
        # VoiceMem appends a "Note: the memory system found no specific evidence…"
        # directive when it has nothing; that is not a memory and costs ~50 tokens.
        if directive and (left or right) and not directive.lower().startswith("note:"):
            right.append(directive)
        return MemoryBlock.from_lines(left, right)

    async def _ingest(self, user_id: str, user_text: str, assistant_text: str) -> dict:
        vm = await self._vm_for(user_id)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._ingest_pool, lambda: vm.ingest(user_text, agent_reply=assistant_text or None)
        )
        return result if isinstance(result, dict) else {"result": result}
