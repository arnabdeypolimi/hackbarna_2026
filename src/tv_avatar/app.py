"""HTTP session lifecycle and the control WebSocket endpoint."""
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Header, Query, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import ValidationError

from tv_avatar.config import get_settings
from tv_avatar.control.channel import ControlChannel
from tv_avatar.control.protocol import PROTOCOL_VERSION, ErrorMsg
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionStore

#: How often expired sessions (and their command buses) are reaped.
SWEEP_INTERVAL_S = 60.0


def create_app(
    store: SessionStore | None = None,
    *,
    sweep_interval_s: float = SWEEP_INTERVAL_S,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper = asyncio.create_task(_sweep_loop(app, sweep_interval_s))
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(title="tv-avatar", lifespan=lifespan)
    app.state.store = store or SessionStore()
    app.state.manager = SessionManager()

    def sweep(now: float | None = None) -> list[str]:
        """Reap expired sessions together with their buses. Returns the ids."""
        expired = app.state.store.sweep_expired(now)
        for sid in expired:
            app.state.manager.drop(sid)
        if expired:
            logger.info("swept {} expired session(s)", len(expired))
        return expired

    app.state.sweep = sweep

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

    @app.delete("/sessions/{session_id}", status_code=204)
    async def close_session(
        session_id: str,
        token: str = Header(default="", alias="X-Control-Token"),
    ) -> Response:
        """Explicit hang-up (spec §6, Closing). Token-authenticated like the socket."""
        try:
            app.state.store.authenticate(session_id, token)
        except (KeyError, PermissionError):
            return Response(status_code=401)
        app.state.store.close(session_id)
        app.state.manager.drop(session_id)
        return Response(status_code=204)

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
        # The bus deliberately survives this call returning: a dropped control
        # socket is the Degraded state (spec §6) — commands queue until the TV
        # app reconnects with the same session id. Expiry or DELETE reaps it.
        await ControlChannel(websocket, session, bus).run()

    mock = Path(__file__).resolve().parents[2] / "tools" / "mock_tv_client"
    if mock.is_dir():
        app.mount("/mock", StaticFiles(directory=mock, html=True), name="mock")

    return app


async def _sweep_loop(app: FastAPI, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        app.state.sweep()


def _settings_available() -> bool:
    """False only when required env vars are absent (tests run without keys).

    Any other failure is a real configuration bug and must propagate.
    """
    try:
        get_settings()
        return True
    except ValidationError:
        return False


app = create_app()
