"""HTTP session lifecycle, the control WebSocket, the WebRTC offer endpoint
and the catalog sample used by the demo client."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from tv_avatar.config import Settings, get_settings
from tv_avatar.control.channel import ControlChannel
from tv_avatar.control.protocol import PROTOCOL_VERSION, ErrorMsg
from tv_avatar.logging import setup_logging
from tv_avatar.runtime import Runtime, build_runtime
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionStore


class CreateSessionRequest(BaseModel):
    user_id: str | None = None


class OfferRequest(BaseModel):
    sdp: str
    type: str
    token: str
    avatar: bool | None = None


def create_app(store: SessionStore | None = None, runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = _settings()
        setup_logging(settings.log_level if settings else "INFO")
        if app.state.runtime is None and settings is not None:
            app.state.runtime = build_runtime(settings)
        if app.state.runtime is not None and settings is not None and settings.agent_impl == "sgr":
            app.state.runtime.warm_in_background()
        yield
        if app.state.runtime is not None:
            await app.state.runtime.close()

    app = FastAPI(title="tv-avatar", lifespan=lifespan)
    app.state.store = store or SessionStore()
    app.state.manager = SessionManager()
    app.state.runtime = runtime

    @app.post("/sessions")
    async def create_session(body: CreateSessionRequest | None = None) -> dict:
        settings = _settings()
        ttl = settings.control_token_ttl_s if settings else 3600
        session = app.state.store.create(ttl, user_id=body.user_id if body else None)
        app.state.manager.bus_for(session.session_id)
        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "control_token": session.control_token,
            "control_url": f"/sessions/{session.session_id}/control",
            "offer_url": f"/sessions/{session.session_id}/offer",
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
        recorder = app.state.runtime.recorder if app.state.runtime is not None else None
        await ControlChannel(websocket, session, bus, recorder=recorder).run()

    @app.post("/sessions/{session_id}/offer")
    async def offer(session_id: str, body: OfferRequest) -> dict:
        """WebRTC media plane: browser mic in, SLNG speech + Anam video out."""
        try:
            session = app.state.store.authenticate(session_id, body.token)
        except (KeyError, PermissionError):
            raise HTTPException(status_code=401, detail="invalid session or token") from None
        settings = _settings()
        if settings is None:
            raise HTTPException(status_code=503, detail="speech keys not configured")

        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

        from tv_avatar.pipeline.runner import run_session

        with_avatar = body.avatar if body.avatar is not None else bool(settings.anam_api_key)
        connection = SmallWebRTCConnection()
        await connection.initialize(sdp=body.sdp, type=body.type)
        transport = SmallWebRTCTransport(connection, TransportParams(
            audio_in_enabled=True, audio_out_enabled=True,
            video_out_enabled=with_avatar, video_out_is_live=with_avatar,
            video_out_width=settings.video_width, video_out_height=settings.video_height,
        ))
        bus = app.state.manager.bus_for(session_id)
        task = asyncio.create_task(run_session(session, bus, transport, with_avatar=with_avatar,
                                               runtime=app.state.runtime))
        task.add_done_callback(lambda t: _log_pipeline_end(session_id, t))
        return connection.get_answer()

    @app.get("/catalog/sample")
    async def catalog_sample(limit: int = Query(default=8, ge=1, le=50)) -> dict:
        runtime = app.state.runtime
        if runtime is None or runtime.catalog is None:
            return {"titles": []}
        return {"titles": [
            {"title_id": i.title_id, "name": i.name, "year": i.year, "genres": i.genres,
             "poster_path": i.poster_path}
            for i in runtime.catalog.sample(limit)
        ]}

    mock = Path(__file__).resolve().parents[2] / "tools" / "mock_tv_client"
    if mock.is_dir():
        app.mount("/mock", StaticFiles(directory=mock, html=True), name="mock")

    return app


def _log_pipeline_end(session_id: str, task: asyncio.Task) -> None:
    log = logger.bind(session_id=session_id)
    if task.cancelled():
        log.info("pipeline cancelled")
    elif task.exception() is not None:
        log.opt(exception=task.exception()).error("pipeline crashed")
    else:
        log.info("pipeline finished")


def _settings() -> Settings | None:
    try:
        return get_settings()
    except Exception:  # noqa: BLE001 — tests run without keys present
        return None


app = create_app()
