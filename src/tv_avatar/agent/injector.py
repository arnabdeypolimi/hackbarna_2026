"""ScreenContextInjector: stamps the current screen (enriched from the catalog)
and the user's recent activity into the system message of every LLMContextFrame.

Serves the `stub` pipeline only. The SGR agent writes its own system prompt
(SGRAgentService.build_messages) — a second writer upstream would stamp the same
sections twice. Stamps, never stores — the latest screen wins and history never
pollutes the conversation (D9). Every other frame passes through untouched.
"""
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tv_avatar.agent.prompt import (
    _Catalog,
    build_system_prompt,
    render_screen,
    render_shop,
)
from tv_avatar.history.store import HistoryStore
from tv_avatar.session.state import SessionState

__all__ = ["ScreenContextInjector", "render_screen"]


class ScreenContextInjector(FrameProcessor):
    def __init__(self, session: SessionState, *, catalog: _Catalog | None = None,
                 history: HistoryStore | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._catalog = catalog
        self._history = history

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            await self._stamp(frame)
        await self.push_frame(frame, direction)

    async def _stamp(self, frame: LLMContextFrame) -> None:
        user_id = self._session.user_id or self._session.session_id
        history = (await self._history.render_for_prompt(user_id, self._catalog)
                   if self._history is not None else "Recently watched: (none yet)")
        system = "\n\n".join([
            build_system_prompt(self._session.persona.language),
            "# Screen\n" + render_screen(self._session, self._catalog),
            "# Shop\n" + render_shop(self._catalog),
            "# Recent activity\n" + history,
        ])
        messages = [m for m in frame.context.get_messages() if m.get("role") != "system"]
        frame.context.set_messages([{"role": "system", "content": system}, *messages])
