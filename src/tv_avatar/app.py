"""HTTP session lifecycle, WebRTC signalling, the control WebSocket, and the
catalog sample used by the demo clients."""
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Response, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel, ValidationError

from tv_avatar.config import Settings, get_settings
from tv_avatar.control.channel import ControlChannel
from tv_avatar.control.protocol import PROTOCOL_VERSION, ErrorMsg
from tv_avatar.logging import setup_logging
from tv_avatar.pipeline.runner import run_session
from tv_avatar.pipeline.transport import build_transport
from tv_avatar.runtime import Runtime, build_runtime
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionState, SessionStore

#: How often expired sessions (and their command buses) are reaped.
SWEEP_INTERVAL_S = 60.0

_TOOLS = Path(__file__).resolve().parents[2] / "tools"


class CreateSessionRequest(BaseModel):
    user_id: str | None = None


def create_app(
    store: SessionStore | None = None,
    runtime: Runtime | None = None,
    *,
    sweep_interval_s: float = SWEEP_INTERVAL_S,
) -> FastAPI:
    webrtc = SmallWebRTCRequestHandler()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = None if _missing_settings() else get_settings()
        setup_logging(settings.log_level if settings else "INFO")
        if app.state.runtime is None and settings is not None:
            app.state.runtime = build_runtime(settings)
        if app.state.runtime is not None and settings is not None and settings.agent_impl == "sgr":
            app.state.runtime.warm_in_background()
        sweeper = asyncio.create_task(_sweep_loop(app, sweep_interval_s))
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            await app.state.manager.shutdown()
            await webrtc.close()
            if app.state.runtime is not None:
                await app.state.runtime.close()

    app = FastAPI(title="tv-avatar", lifespan=lifespan)
    app.state.store = store or SessionStore()
    app.state.manager = SessionManager()
    app.state.runtime = runtime

    def sweep(now: float | None = None) -> list[str]:
        """Reap expired sessions together with their buses. Returns the ids."""
        expired = app.state.store.sweep_expired(now)
        for sid in expired:
            app.state.manager.drop(sid)
        if expired:
            logger.info("swept {} expired session(s)", len(expired))
        return expired

    app.state.sweep = sweep

    def _authenticated(session_id: str, token: str) -> SessionState:
        try:
            return app.state.store.authenticate(session_id, token)
        except (KeyError, PermissionError):
            # Deliberately indistinguishable: an unknown session and a bad
            # token leak different information if reported separately.
            raise HTTPException(401, "invalid session or token") from None

    @app.get("/config")
    async def config() -> dict:
        """Non-secret view of the configured stack, for the console header."""
        missing = _missing_settings()
        s = Settings.model_construct() if missing else get_settings()
        rt = app.state.runtime
        return {
            "configured": not missing,
            "missing": missing,
            "agent_impl": s.agent_impl,
            "llm_model": s.llm_model,
            "stt_model": s.slng_stt_model,
            "tts_model": s.slng_tts_model,
            "tts_voice": s.slng_tts_voice,
            "tts_sample_rate": s.slng_tts_sample_rate,
            "avatar_model": s.anam_avatar_model,
            "catalog_titles": len(rt.catalog) if rt is not None and rt.catalog is not None else 0,
        }

    @app.post("/sessions")
    async def create_session(body: CreateSessionRequest | None = None) -> dict:
        ttl = get_settings().control_token_ttl_s if _settings_available() else 3600
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

    @app.post("/sessions/{session_id}/offer")
    async def offer(
        session_id: str,
        request: SmallWebRTCRequest,
        token: str = Query(default=""),
        avatar: bool = Query(default=True),
        halfduplex: bool = Query(default=False),
    ) -> dict:
        """WebRTC offer → answer. Spawns the session's pipeline on first offer."""
        session = _authenticated(session_id, token)
        if not _settings_available():
            raise HTTPException(503, "media services are not configured (.env)")
        settings = get_settings()
        bus = app.state.manager.bus_for(session_id)

        async def on_connection(connection: SmallWebRTCConnection) -> None:
            transport = build_transport(connection, settings, with_avatar=avatar)
            app.state.manager.start_pipeline(
                session_id,
                run_session(session, bus, transport, with_avatar=avatar,
                            half_duplex=halfduplex, runtime=app.state.runtime),
            )

        answer = await webrtc.handle_web_request(request, on_connection)
        if answer is None:
            raise HTTPException(500, "no SDP answer produced")
        return answer

    @app.patch("/sessions/{session_id}/offer")
    async def ice_candidate(
        session_id: str,
        request: SmallWebRTCPatchRequest,
        token: str = Query(default=""),
    ) -> dict:
        _authenticated(session_id, token)
        await webrtc.handle_patch_request(request)
        return {"status": "ok"}

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
            await websocket.send_text(
                ErrorMsg(code="unauthorized",
                         message="invalid session or token").model_dump_json()
            )
            await websocket.close(code=4401)
            return
        bus = app.state.manager.bus_for(session_id)
        recorder = app.state.runtime.recorder if app.state.runtime is not None else None
        # The bus deliberately survives this call returning: a dropped control
        # socket is the Degraded state (spec §6) — commands queue until the TV
        # app reconnects with the same session id. Expiry or DELETE reaps it.
        await ControlChannel(websocket, session, bus, recorder=recorder).run()

    @app.get("/catalog/sample")
    async def catalog_sample(limit: int = Query(default=8, ge=1, le=50)) -> dict:
        rt = app.state.runtime
        if rt is None or rt.catalog is None:
            return {"titles": []}
        return {"titles": [
            {"title_id": i.title_id, "name": i.name, "year": i.year, "genres": i.genres,
             "poster_path": i.poster_path}
            for i in rt.catalog.sample(limit)
        ]}

    for mount, directory in (("/mock", _TOOLS / "mock_tv_client"), ("/demo", _TOOLS / "demo")):
        if directory.is_dir():
            app.mount(mount, StaticFiles(directory=directory, html=True), name=mount[1:])

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/demo/")

    return app


async def _sweep_loop(app: FastAPI, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        app.state.sweep()


def _missing_settings() -> list[str]:
    """Names of required env vars that are absent or blank; [] when ready.

    Only ``ValidationError`` is absorbed (tests run without keys); any other
    failure is a real configuration bug and must propagate.
    """
    try:
        get_settings()
        return []
    except ValidationError as exc:
        return sorted(str(err["loc"][0]).upper() for err in exc.errors())


def _settings_available() -> bool:
    return not _missing_settings()


app = create_app()
