"""HTTP session lifecycle and the control WebSocket endpoint."""
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket
from fastapi.staticfiles import StaticFiles

from tv_avatar.config import get_settings
from tv_avatar.control.channel import ControlChannel
from tv_avatar.control.protocol import PROTOCOL_VERSION, ErrorMsg
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionStore


def create_app(store: SessionStore | None = None) -> FastAPI:
    app = FastAPI(title="tv-avatar")
    app.state.store = store or SessionStore()
    app.state.manager = SessionManager()

    @app.post("/sessions")
    async def create_session() -> dict:
        ttl = get_settings().control_token_ttl_s if _settings_available() else 3600
        session = app.state.store.create(ttl)
        app.state.manager.bus_for(session.session_id)
        return {
            "session_id": session.session_id,
            "control_token": session.control_token,
            "control_url": f"/sessions/{session.session_id}/control",
            "protocol_version": PROTOCOL_VERSION,
        }

    @app.websocket("/sessions/{session_id}/control")
    async def control(websocket: WebSocket, session_id: str,
                      token: str = Query(default="")) -> None:
        await websocket.accept()
        try:
            session = app.state.store.authenticate(session_id, token)
        except (KeyError, PermissionError):
            # Deliberately indistinguishable: an unknown session and a bad
            # token leak different information if reported separately.
            await websocket.send_text(
                ErrorMsg(code="unauthorized",
                         message="invalid session or token").model_dump_json()
            )
            await websocket.close(code=4401)
            return
        bus = app.state.manager.bus_for(session_id)
        await ControlChannel(websocket, session, bus).run()

    mock = Path(__file__).resolve().parents[2] / "tools" / "mock_tv_client"
    if mock.is_dir():
        app.mount("/mock", StaticFiles(directory=mock, html=True), name="mock")

    return app


def _settings_available() -> bool:
    try:
        get_settings()
        return True
    except Exception:  # noqa: BLE001 — tests run without keys present
        return False


app = create_app()
