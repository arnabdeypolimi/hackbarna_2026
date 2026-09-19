"""One command bus per session; the pipeline and the socket share it."""
from tv_avatar.control.bus import CommandBus


class SessionManager:
    def __init__(self) -> None:
        self._buses: dict[str, CommandBus] = {}

    def bus_for(self, session_id: str) -> CommandBus:
        return self._buses.setdefault(session_id, CommandBus())

    def drop(self, session_id: str) -> None:
        self._buses.pop(session_id, None)
