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
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from tv_avatar.config import Settings
from tv_avatar.memory.lane import BaseMemoryLane, MemoryBlock

E5_THREADS = 2
#: multilingual-e5-small output width — used for VoiceMem's anchor/graph vectors too.
E5_DIM = 384
#: Appended to VoiceMem's two Chinese extraction prompts so notes come out in English.
_ENGLISH_ONLY = (
    "\n\nLANGUAGE: write every label, note and free-text value in ENGLISH, 3-12 words, "
    "regardless of the examples above. Keep the slot keys exactly as listed."
)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff" for ch in text)


#: VoiceMem's right-brain *render template* vocabulary (rightbrain/brain.py). The
#: notes themselves are English after the prompt patch; these fixed tokens are not.
_RENDER_VOCAB = {
    "（喜好与厌恶）": " (likes/dislikes)", "（表达风格）": " (communication style)",
    "（思维模式）": " (thinking style)", "（应对方式）": " (coping style)", "（情绪）": " (emotion)",
    "｜他说过：": " — said: ", "｜她说过：": " — said: ", "｜说过：": " — said: ",
    "✓ 有效方式：": "✓ Effective approach: ", "✗ 无效方式：": "✗ Ineffective approach: ",
    "（下次：": " (next time: ", "）": ")", "：": ": ",
}


def _english_note(line: str) -> str | None:
    """Translate the template tokens; drop the line only if Chinese content remains."""
    for zh, en in _RENDER_VOCAB.items():
        line = line.replace(zh, en)
    return None if _has_cjk(line) else " ".join(line.split())


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
    # Anchor/graph vectors are produced by the local E5 (see _patch_voicemem), so
    # the dimension check must expect E5's width, not a cloud model's.
    os.environ["VOICEMEM_EMBED_DIM"] = str(E5_DIM)
    os.environ.setdefault("OPENAI_EMBEDDING_MODEL", "local/multilingual-e5-small")


_patched = False


def _patch_voicemem() -> None:
    """Two seams VoiceMem 0.2.3 leaves open, applied once per process:

    1. `Orchestrator._embed_uncached` — the graph/anchor embedder used by the
       right brain's query lookup and both brains' graph writes. Upstream it is
       an OpenAI-SDK call (cloud jitter 108 ms–1.8 s *on the read path*); we route
       it to the same local E5 the memory vectors already use.
    2. The two extraction prompts that ask for Chinese labels — the merged
       fact/trait prompt and the right brain's attribution prompt — get an
       English-only instruction appended, so `result_rightbrain` is usable by
       an English agent. The inner-OS prompt is already language-aware.
    """
    global _patched
    if _patched:
        return
    import numpy as np
    from voicemem.leftbrain import merged_extraction
    from voicemem.leftbrain.local_e5_embedder import shared_e5
    from voicemem.orchestrator import Orchestrator
    from voicemem.rightbrain.brain import RightBrain

    model = shared_e5()
    if not getattr(model, "_tv_avatar_locked", False):
        raw_encode = model.encode

        def locked_encode(*args, **kwargs):
            with _e5_lock:
                return raw_encode(*args, **kwargs)

        model.encode = locked_encode
        model._tv_avatar_locked = True

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        return np.asarray(model.encode([f"passage: {t}" for t in texts], normalize_embeddings=True)).tolist()

    Orchestrator._embed_uncached = _embed_local
    if _ENGLISH_ONLY not in merged_extraction.PROMPT_ADDENDUM:
        merged_extraction.PROMPT_ADDENDUM += _ENGLISH_ONLY
    if _ENGLISH_ONLY not in RightBrain._ATTRIBUTION_PROMPT:
        # Top and bottom: the body is Chinese and the model otherwise mirrors it.
        RightBrain._ATTRIBUTION_PROMPT = _ENGLISH_ONLY.strip() + "\n\n" + RightBrain._ATTRIBUTION_PROMPT + _ENGLISH_ONLY
    _patched = True
    logger.info("voicemem patched: local E5 anchors ({} dims), English-only notes", E5_DIM)


_torch_pinned = False
#: E5 forward passes from VoiceMem's worker threads and our own must not overlap:
#: concurrent torch inference plus a thread-count change crashed the process.
_e5_lock = threading.Lock()


def _pin_torch_threads() -> None:
    """Once per process, before any inference — changing it mid-inference is fatal."""
    global _torch_pinned
    if _torch_pinned:
        return
    try:
        import torch
        torch.set_num_threads(E5_THREADS)
        _torch_pinned = True
    except Exception as err:  # noqa: BLE001 — torch is a voicemem dependency; log, don't fail
        logger.warning("could not pin torch threads: {}", type(err).__name__)


def _memory_root(root: Path) -> Path:
    """A space written with another vector width cannot be reused; park it."""
    marker = root / ".embed_dim"
    if root.exists() and (not marker.exists() or marker.read_text().strip() != str(E5_DIM)):
        parked = root.with_name(root.name + ".pre-e5")
        if parked.exists():
            import shutil
            shutil.rmtree(parked)
        root.rename(parked)
        logger.warning("voicemem space {} used another embedding width; parked as {}", root.name, parked.name)
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(E5_DIM))
    return root


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
        _pin_torch_threads()
        _patch_voicemem()
        root = _memory_root(Path(self._settings.memory_root) / user_id)
        return VoiceMem.from_config({
            "api_key": self._settings.nebius_api_key,
            "base_url": self._settings.nebius_base_url,
            "mode": self._settings.voicemem_mode,   # see Settings.voicemem_mode
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
                self._vms[user_id] = await asyncio.to_thread(self._make_vm, user_id)
                logger.bind(user_id=user_id).info("voicemem space opened")
        return self._vms[user_id]

    async def warmup(self) -> None:
        """Load E5 once per process. Called from create_task at app start."""
        def load() -> None:
            _pin_torch_threads()
            _patch_voicemem()  # loads and wraps the shared E5
        await asyncio.to_thread(load)
        logger.info("voicemem E5 warm")

    async def _search(self, user_id: str, text: str) -> MemoryBlock:
        vm = await self._vm_for(user_id)
        result = await asyncio.to_thread(vm.search, text)
        left = list(getattr(result, "result_leftbrain", []) or [])
        # The right brain writes its experience notes in Chinese ("有效方式：…");
        # they are VoiceMem-internal coaching, not something to tell an English LLM.
        right = [en for line in (getattr(result, "result_rightbrain", []) or []) if (en := _english_note(line))]
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
