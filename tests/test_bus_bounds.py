"""The bus survives a TV app that is not there to drain it."""
import asyncio

import pytest

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import AgentStatusMsg, TranscriptMsg


def _event(i: int) -> TranscriptMsg:
    return TranscriptMsg(role="user", text=f"partial {i}", final=False)


async def test_events_are_capped_but_commands_are_not():
    bus = CommandBus(max_queued_events=3)
    await bus.dispatch("home", {}, turn_id="t1")
    for i in range(10):
        bus.publish(_event(i))
    await bus.dispatch("back", {}, turn_id="t1")

    drained = []
    while bus._outbound:
        drained.append(await bus.next_outbound())
    verbs = [m.verb for m in drained if m.type == "command"]
    events = [m.text for m in drained if m.type == "transcript"]
    assert verbs == ["home", "back"]
    assert events == ["partial 7", "partial 8", "partial 9"]  # oldest dropped, order kept


async def test_event_count_tracks_pops_so_the_cap_is_a_window_not_a_lifetime_total():
    bus = CommandBus(max_queued_events=2)
    for i in range(2):
        bus.publish(_event(i))
    await bus.next_outbound()
    await bus.next_outbound()
    bus.publish(_event(2))
    bus.publish(_event(3))
    assert [(await bus.next_outbound()).text for _ in range(2)] == ["partial 2", "partial 3"]


async def test_peek_leaves_the_message_for_a_retry():
    bus = CommandBus()
    bus.publish(AgentStatusMsg(state="idle"))
    first = await bus.peek_outbound()
    second = await bus.peek_outbound()
    assert first is second
    bus.pop_outbound()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.peek_outbound(), timeout=0.05)
