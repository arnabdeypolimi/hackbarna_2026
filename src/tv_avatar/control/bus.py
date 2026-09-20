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
from dataclasses import dataclass, field

from loguru import logger
from opentelemetry import baggage
from opentelemetry.trace import Link, SpanContext, StatusCode

from tv_avatar import tracing as tel
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


@dataclass
class _CommandOrigin:
    context: SpanContext
    queued_at: float
    turn_id: str
    awaits: bool
    attributes: dict
    replies: set[str] = field(default_factory=set)

#: Non-command messages (status, transcripts) kept while no socket is draining
#: the queue. The observer publishes one per interim transcript, so a voice
#: session in the Degraded state (TV app disconnected) would otherwise grow the
#: queue for the whole token TTL. Commands are never dropped here — they are
#: what the spec says must survive a reconnect; a stale status is worthless.
MAX_QUEUED_EVENTS = 64


class CommandBus:
    def __init__(self, search_timeout_s: float | None = None, *,
                 max_queued_events: int = MAX_QUEUED_EVENTS) -> None:
        self._outbound: deque[ServerMessage] = deque()
        self._queued_events = 0
        self._max_queued_events = max_queued_events
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._pending_turn: dict[str, str] = {}  # command_id -> turn_id
        self._origins: OrderedDict[str, _CommandOrigin] = OrderedDict()
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
            tel.META_TURN_ID: turn_id,
            ATTR_OBS_INPUT: json.dumps(msg.args, ensure_ascii=False),
        }) as span:
            self._origins[msg.id] = _CommandOrigin(
                span.get_span_context(), time.monotonic(), turn_id, awaits,
                {**dict(baggage.get_all()), META_VERB: msg.verb, tel.META_TURN_ID: turn_id})
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
        origin = self._origins.pop(command_id, None)
        return (origin.context, origin.queued_at) if origin is not None else None

    def has_origin(self, command_id: str) -> bool:
        return command_id in self._origins

    def mark_sent(self, command_id: str) -> None:
        origin = self._origins.get(command_id)
        if origin is not None:
            self._delivery(command_id, "sent")

    def mark_send_failed(self, command_id: str) -> None:
        self._delivery(command_id, "send_failed")

    def _delivery(self, command_id: str, status: str) -> None:
        origin = self._origins.get(command_id)
        if origin is None:
            return
        with tel.attribute_scope(origin.attributes), observation("tv.command_delivery", type=tel.OBS_TYPE_EVENT,
                         parent_context=tel.detached_from_span(), links=(Link(origin.context),), **{
            **origin.attributes, ATTR_COMMAND_ID: command_id, META_STATUS: status,
        }) as span:
            if status == "send_failed":
                span.set_status(StatusCode.ERROR, "send_failed")
            logger.info("tv.command.delivery", event="tv.command.delivery", command_id=command_id,
                        turn_id=origin.turn_id, status=status)

    def record_reply(self, command_id: str, status: str, payload: dict, *, reply_type: str) -> None:
        origin = self._origins.get(command_id)
        if origin is None or reply_type in origin.replies:
            return
        origin.replies.add(reply_type)
        with tel.attribute_scope(origin.attributes), observation("tv.command_result", type=tel.OBS_TYPE_EVENT,
                         parent_context=tel.detached_from_span(), links=(Link(origin.context),), **{
            **origin.attributes, ATTR_COMMAND_ID: command_id, META_STATUS: status,
            tel.META_REPLY_TYPE: reply_type,
            tel.ATTR_COMMAND_ROUNDTRIP_MS: round((time.monotonic() - origin.queued_at) * 1000),
            ATTR_OBS_OUTPUT: json.dumps(payload, ensure_ascii=False, default=str),
        }) as span:
            if status in {"failed", "error", "invalid"}:
                span.set_status(StatusCode.ERROR, status)
            logger.info("tv.command.reply", event="tv.command.reply", command_id=command_id,
                        turn_id=origin.turn_id, status=status, reply_type=reply_type)
        if not origin.awaits or origin.replies == {"ack", "result"}:
            self._origins.pop(command_id, None)

    def publish(self, msg: ServerMessage) -> None:
        """Queue a non-command server message (agent_status, transcript).

        Shares the command queue so the client sees events and commands in
        the order the pipeline produced them."""
        self._enqueue(msg)

    def _enqueue(self, msg: ServerMessage) -> None:
        if not isinstance(msg, CommandMsg):
            self._queued_events += 1
            if self._queued_events > self._max_queued_events:
                self._drop_oldest_event()
        self._outbound.append(msg)
        if isinstance(msg, CommandMsg):
            self._delivery(msg.id, "queued")
        self._ready.set()

    def _drop_oldest_event(self) -> None:
        for i, queued in enumerate(self._outbound):
            if not isinstance(queued, CommandMsg):
                del self._outbound[i]
                self._queued_events -= 1
                return

    async def next_outbound(self) -> ServerMessage:
        """Take the next message. See ``peek_outbound`` for the socket writer."""
        msg = await self.peek_outbound()
        self.pop_outbound(msg)
        return msg

    async def peek_outbound(self) -> ServerMessage:
        """Wait for the next message without removing it, so the writer can send
        first and pop after: a send that fails mid-disconnect leaves the message
        queued for the reconnecting client instead of losing it."""
        while not self._outbound:
            self._ready.clear()
            await self._ready.wait()
        return self._outbound[0]

    def pop_outbound(self, sent: ServerMessage) -> None:
        """Remove ``sent`` once it is on the wire — only if it is still the head.
        While the send was in flight a barge-in (``cancel_turn``) or the event cap
        may have removed it already; popping blindly would then discard the
        message behind it, which nobody has sent."""
        if not self._outbound or self._outbound[0] is not sent:
            return
        if not isinstance(self._outbound.popleft(), CommandMsg):
            self._queued_events -= 1

    def cancel_turn(self, turn_id: str) -> int:
        """Drop queued-but-unsent commands for an interrupted turn.

        Commands already handed to the WebSocket are NOT rolled back
        (spec §9, rule 4). A ``search_catalog`` handler still awaiting its
        result is released immediately with ``{"status": "cancelled"}`` so
        the interrupted turn does not sit out the 400 ms timeout."""
        for msg in self._outbound:
            if isinstance(msg, CommandMsg) and msg.turn_id == turn_id:
                self._delivery(msg.id, "dropped")
                self._origins.pop(msg.id, None)
        keep = deque(
            m for m in self._outbound
            if not isinstance(m, CommandMsg) or m.turn_id != turn_id
        )
        dropped = len(self._outbound) - len(keep)
        self._outbound = keep  # only commands were removed; the event count is unchanged
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
