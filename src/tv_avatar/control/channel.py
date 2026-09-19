"""Control WebSocket: commands down, screen state up.

Reader and writer run concurrently so a silent client never blocks
command delivery, and a chatty client never delays it.
"""
import asyncio

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import (
    AgentStatusMsg,
    ErrorMsg,
    ProtocolError,
    parse_client_message,
)
from tv_avatar.session.state import SessionState


class ControlChannel:
    def __init__(self, websocket: WebSocket, session: SessionState, bus: CommandBus):
        self._ws = websocket
        self._session = session
        self._bus = bus

    async def run(self) -> None:
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
                logger.warning("control channel ended: {}", exc)

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
                self._session.update_screen(msg.state)
            case "result":
                self._bus.resolve(msg.command_id, msg.data)
            case "ack":
                if not msg.ok:
                    logger.warning("command {} failed: {}", msg.command_id, msg.error)
            case "user_event":
                logger.info("user event {}: {}", msg.event, msg.detail)

    async def _write_loop(self) -> None:
        while True:
            command = await self._bus.next_outbound()
            await self._ws.send_text(command.model_dump_json())
