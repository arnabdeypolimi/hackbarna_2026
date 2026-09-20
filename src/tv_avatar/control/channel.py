"""Control WebSocket: commands and status down, screen state up.

Reader and writer run concurrently so a silent client never blocks
command delivery, and a chatty client never delays it.
"""
import asyncio
import contextlib
import json
import time

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from opentelemetry.trace import Link, StatusCode

from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import (
    AgentStatusMsg,
    ErrorMsg,
    ProtocolError,
    parse_client_message,
)
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.session.state import SessionState
from tv_avatar.tracing import (
    ATTR_COMMAND_ID,
    ATTR_COMMAND_ROUNDTRIP_MS,
    ATTR_OBS_OUTPUT,
    ATTR_OBS_TYPE,
    META_STATUS,
    OBS_TYPE_EVENT,
    session_scope,
    tracer,
)


class ControlChannel:
    def __init__(self, websocket: WebSocket, session: SessionState, bus: CommandBus,
                 recorder: HistoryRecorder | None = None, settings: Settings | None = None):
        self._ws = websocket
        self._session = session
        self._bus = bus
        self._recorder = recorder
        self._settings = settings
        self._log = logger.bind(session_id=session.session_id, user_id=session.user_id)

    async def run(self) -> None:
        # The socket handler is its own task, so it does not inherit the pipeline
        # runner's baggage: the session identity is attached here for the acks.
        scope = (session_scope(self._session, self._settings) if self._settings is not None
                 else contextlib.nullcontext())
        with scope:
            await self._run()

    async def _run(self) -> None:
        await self._ws.send_text(AgentStatusMsg(state="idle").model_dump_json())
        reader = asyncio.create_task(self._read_loop())
        writer = asyncio.create_task(self._write_loop())
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                self._log.warning("control channel ended: {}", exc)

    async def _read_loop(self) -> None:
        while True:
            raw = await self._ws.receive_text()
            try:
                msg = parse_client_message(raw)
            except ProtocolError as err:
                await self._ws.send_text(
                    ErrorMsg(code=err.code, message=err.message).model_dump_json()
                )
                continue
            await self._handle(msg)

    async def _handle(self, msg) -> None:
        match msg.type:
            case "screen_state":
                # No span: high volume, and the agent stamps the latest screen
                # into its prompt, which the `llm` span's input shows.
                old = self._session.screen
                self._session.update_screen(msg.state)
                if self._recorder is not None:
                    self._recorder.spawn(
                        self._recorder.on_screen_transition(self._session.user_id, old, msg.state))
            case "result":
                self._bus.resolve(msg.command_id, msg.data)
                self._record_reply(msg.command_id, str(msg.data.get("status", "ok")), msg.data)
            case "ack":
                if not msg.ok:
                    self._log.warning("command {} failed: {}", msg.command_id, msg.error)
                self._record_reply(msg.command_id, "ok" if msg.ok else "failed",
                                   {"ok": msg.ok, "error": msg.error})
            case "user_event":
                self._log.info("user event {}: {}", msg.event, msg.detail)
                if self._recorder is not None:
                    self._recorder.spawn(
                        self._recorder.on_user_event(self._session.user_id, msg.event, msg.detail))

    def _record_reply(self, command_id: str, status: str, payload: dict) -> None:
        """Close the loop the media plane cannot see: did the TV execute the
        command, and how long after the agent decided. A zero-duration root
        span (type `event`) linked to the originating `tv.command`; the
        session id arrives via this channel's baggage."""
        origin = self._bus.origin(command_id)
        if origin is None:
            return  # unknown or already answered: the protocol error path handles it
        span_context, sent_at = origin
        span = tracer().start_span("tv.command_result", links=[Link(span_context)], attributes={
            ATTR_OBS_TYPE: OBS_TYPE_EVENT, META_STATUS: status, ATTR_COMMAND_ID: command_id,
            ATTR_COMMAND_ROUNDTRIP_MS: round((time.monotonic() - sent_at) * 1000),
            ATTR_OBS_OUTPUT: json.dumps(payload, ensure_ascii=False, default=str),
        })
        if status == "failed":
            span.set_status(StatusCode.ERROR, str(payload.get("error")))
        span.end()

    async def _write_loop(self) -> None:
        while True:
            message = await self._bus.next_outbound()
            await self._ws.send_text(message.model_dump_json())
