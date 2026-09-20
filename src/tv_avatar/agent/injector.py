"""ScreenContextInjector: stamps the current screen (enriched from the catalog)
and the user's recent activity into the system message of every LLMContextFrame.

Stamps, never stores — the latest screen wins and history never pollutes the
conversation (D9). The agent adds the memory block itself because that read
awaits the speculative prefetch. Every other frame passes through untouched.
"""
from typing import Protocol

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tv_avatar.agent.prompt import build_system_prompt
from tv_avatar.control.protocol import ScreenState
from tv_avatar.history.store import HistoryStore
from tv_avatar.session.state import SessionState, shop_mark
from tv_avatar.shop import get_shop


class _Catalog(Protocol):
    def lookup(self, title_id: str): ...


def render_shop(catalog: _Catalog | None) -> str:
    """Every shelf with the title's catalogue name, so "the Barbie merch" resolves to an
    id whether or not Barbie is on screen."""
    def name_of(title_id: str) -> str | None:
        item = catalog.lookup(title_id) if catalog is not None else None
        return item.name if item is not None else None
    return get_shop().render_shelves(name_of)


def render_screen(session: SessionState, catalog: _Catalog | None) -> str:
    """Phase-1 render enriched per tile: `Sicario (2015) — Crime, Thriller (id=273481) <- focused`."""
    screen: ScreenState | None = session.screen
    if screen is None:
        return session.render_for_prompt()
    lines = [f"View: {screen.view}"]
    if screen.rail_id:
        lines.append(f"Rail: {screen.rail_id}")
    for tile in screen.tiles:
        item = catalog.lookup(tile.title_id) if catalog is not None else None
        label = item.label() if item is not None else tile.name
        marker = " <- focused" if tile.position == screen.focus_index else ""
        lines.append(f"  [{tile.position}] {label} (id={tile.title_id}){shop_mark(tile)}{marker}")
    pb = screen.playback
    if pb.state == "stopped":
        lines.append("Playback: stopped")
    else:
        item = catalog.lookup(pb.title_id) if catalog is not None and pb.title_id else None
        name = f" ({item.name})" if item is not None else ""
        lines.append(f"Playback: {pb.state} {pb.title_id}{name} at {pb.position_s:.0f}s")
    return "\n".join(lines)


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
