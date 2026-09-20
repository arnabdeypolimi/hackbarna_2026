"""Bridge between the agent's tool handlers and the control WebSocket.

Two rules from spec §8 and §9 live here:
  - fire-and-forget commands never await the client, because a slow TV
    would stall the LLM turn and stall speech with it;
  - the queue is turn-scoped, so barge-in drops commands the interrupted
    turn had queued but not yet sent.
"""
import asyncio
import json
import time
import uuid
from collections import OrderedDict, deque

from opentelemetry.trace import SpanContext

from tv_avatar.agent.commands import AWAITS_RESULT, parse_command
from tv_avatar.control.protocol import CommandMsg, ServerMessage
from tv_avatar.tracing import (
    ATTR_COMMAND_AWAITS_RESULT,
    ATTR_COMMAND_ID,
    ATTR_OBS_INPUT,
    ATTR_OBS_OUTPUT,
    META_STATUS,
    META_VERB,
    OBS_TYPE_TOOL,
    observation,
)

#: Command origins remembered for the TV's reply to link back to (a session's
#: worth of commands; the TV normally answers within a second).
MAX_ORIGINS = 256


class CommandBus:
    def __init__(self, search_timeout_s: float = 0.4) -> None:
        self._outbound: deque[ServerMessage] = deque()
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._pending_turn: dict[str, str] = {}  # command_id -> turn_id
        self._origins: OrderedDict[str, tuple[SpanContext, float]] = OrderedDict()
        self._search_timeout_s = search_timeout_s

    async def dispatch(self, verb: str, args: dict, turn_id: str) -> dict:
        command = parse_command(verb, args)  # raises ValueError; never queued
        msg = CommandMsg(
            id=f"cmd_{uuid.uuid4().hex[:8]}",
            turn_id=turn_id,
            verb=command.verb.value,
            args=command.model_dump(exclude={"verb"}, exclude_none=True),
        )
        awaits = command.verb in AWAITS_RESULT
        # The span ends at enqueue for fire-and-forget verbs and covers the wait
        # for the awaited one; the TV's reply becomes a linked `tv.command_result`.
        with observation("tv.command", type=OBS_TYPE_TOOL, **{
            META_VERB: msg.verb, ATTR_COMMAND_ID: msg.id, ATTR_COMMAND_AWAITS_RESULT: awaits,
            ATTR_OBS_INPUT: json.dumps(msg.args, ensure_ascii=False),
        }) as span:
            self._origins[msg.id] = (span.get_span_context(), time.monotonic())
            while len(self._origins) > MAX_ORIGINS:
                self._origins.popitem(last=False)
            result = await self._send(msg, turn_id, awaits)
            span.set_attributes({META_STATUS: str(result.get("status")),
                                 ATTR_OBS_OUTPUT: json.dumps(result, ensure_ascii=False, default=str)})
            return result

    async def _send(self, msg: CommandMsg, turn_id: str, awaits: bool) -> dict:
        if not awaits:
            self._enqueue(msg)
            return {"status": "dispatched"}

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg.id] = future
        self._pending_turn[msg.id] = turn_id
        self._enqueue(msg)
        try:
            return await asyncio.wait_for(future, timeout=self._search_timeout_s)
        except TimeoutError:
            return {"status": "unavailable", "reason": "timeout"}
        finally:
            self._pending.pop(msg.id, None)
            self._pending_turn.pop(msg.id, None)

    def origin(self, command_id: str) -> tuple[SpanContext, float] | None:
        """The `tv.command` span context and send time of a command, once."""
        return self._origins.pop(command_id, None)

    def publish(self, msg: ServerMessage) -> None:
        """Queue a non-command server message (agent_status, transcript).

        Shares the command queue so the client sees events and commands in
        the order the pipeline produced them."""
        self._enqueue(msg)

    def _enqueue(self, msg: ServerMessage) -> None:
        self._outbound.append(msg)
        self._ready.set()

    async def next_outbound(self) -> ServerMessage:
        while not self._outbound:
            self._ready.clear()
            await self._ready.wait()
        return self._outbound.popleft()

    def cancel_turn(self, turn_id: str) -> int:
        """Drop queued-but-unsent commands for an interrupted turn.

        Commands already handed to the WebSocket are NOT rolled back
        (spec §9, rule 4). A ``search_catalog`` handler still awaiting its
        result is released immediately with ``{"status": "cancelled"}`` so
        the interrupted turn does not sit out the 400 ms timeout."""
        keep = deque(
            m for m in self._outbound
            if not isinstance(m, CommandMsg) or m.turn_id != turn_id
        )
        dropped = len(self._outbound) - len(keep)
        self._outbound = keep
        if not self._outbound:
            self._ready.clear()

        for command_id, pending_turn in list(self._pending_turn.items()):
            if pending_turn != turn_id:
                continue
            future = self._pending.get(command_id)
            if future is not None and not future.done():
                future.set_result({"status": "cancelled", "reason": "interrupted"})
        return dropped

    def resolve(self, command_id: str, data: dict) -> None:
        future = self._pending.get(command_id)
        if future is not None and not future.done():
            future.set_result(data)

    def pending_count(self) -> int:
        return len(self._pending)
