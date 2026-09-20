"""In-memory session and screen-state store.

Screen state is pushed by the TV app (spec §D2): latest wins, no history.
A dict is deliberate for phase 1 — swapping in Redis later touches only
SessionStore, not its callers.
"""
import secrets
import time
import uuid
from dataclasses import dataclass, field

from tv_avatar.catalog import SessionPersona, get_catalog
from tv_avatar.control.protocol import ScreenState


@dataclass
class SessionState:
    session_id: str
    control_token: str
    expires_at: float
    #: Avatar and language, pinned for the session's lifetime. Defaults to the
    #: catalog's default pairing so tests and tools can build a state directly.
    persona: SessionPersona = field(default_factory=lambda: get_catalog().resolve())
    created_at: float = field(default_factory=time.time)
    screen: ScreenState | None = None
    current_turn_id: str | None = None
    #: Personalisation key (D10). Sessions come and go; the couch persists.
    user_id: str = ""

    def update_screen(self, state: ScreenState) -> None:
        self.screen = state

    def new_turn(self) -> str:
        self.current_turn_id = f"turn_{uuid.uuid4().hex[:8]}"
        return self.current_turn_id

    def render_for_prompt(self) -> str:
        """Compact rendering injected fresh into every LLM run (spec §4)."""
        if self.screen is None:
            return "Screen state: unknown (the TV app has not reported yet)."
        s = self.screen
        lines = [f"View: {s.view}"]
        if s.rail_id:
            lines.append(f"Rail: {s.rail_id}")
        for tile in s.tiles:
            marker = " <- focused" if tile.position == s.focus_index else ""
            shop = " [shop]" if tile.shoppable else ""
            lines.append(f"  [{tile.position}] {tile.name} (id={tile.title_id}){shop}{marker}")
        pb = s.playback
        if pb.state == "stopped":
            lines.append("Playback: stopped")
        else:
            lines.append(
                f"Playback: {pb.state} {pb.title_id} at {pb.position_s:.0f}s"
            )
        return "\n".join(lines)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def create(self, ttl_s: int, persona: SessionPersona, user_id: str | None = None) -> SessionState:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        session = SessionState(
            session_id=session_id,
            control_token=secrets.token_urlsafe(32),
            expires_at=time.time() + ttl_s,
            persona=persona,
            user_id=user_id or f"anon_{session_id}",
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def authenticate(self, session_id: str, token: str) -> SessionState:
        """The session id travels in a URL across app boundaries, so it is not
        a credential. The token is (spec §7, Control channel authentication)."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if not secrets.compare_digest(session.control_token, token):
            raise PermissionError("invalid control token")
        if time.time() > session.expires_at:
            raise PermissionError("control token expired")
        return session

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def others_for_user(self, user_id: str, except_session_id: str) -> list[str]:
        """Other live sessions of the same user — one couch runs one avatar."""
        return [sid for sid, s in self._sessions.items()
                if s.user_id == user_id and sid != except_session_id]

    def sweep_expired(self, now: float | None = None) -> list[str]:
        """Remove every session past its ``expires_at``; return their ids.

        Expiry is otherwise only checked at control-socket connect, so a
        client that never connects would leak forever without this."""
        now = time.time() if now is None else now
        expired = [sid for sid, s in self._sessions.items() if now > s.expires_at]
        for sid in expired:
            del self._sessions[sid]
        return expired
