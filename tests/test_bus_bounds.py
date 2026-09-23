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


async def test_a_zero_cap_still_queues_the_newest_event():
    """A cap of 0 has nothing to evict when the new event is counted, so it is clamped
    to 1 rather than leaving the count off by one for the life of the session."""
    bus = CommandBus(max_queued_events=0)
    for i in range(3):
        bus.publish(_event(i))
    assert (await bus.next_outbound()).text == "partial 2"
    assert bus._queued_events == 0


async def test_peek_leaves_the_message_for_a_retry():
    bus = CommandBus()
    bus.publish(AgentStatusMsg(state="idle"))
    first = await bus.peek_outbound()
    second = await bus.peek_outbound()
    assert first is second
    bus.pop_outbound(first)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.peek_outbound(), timeout=0.05)


async def test_pop_after_a_barge_in_removed_the_in_flight_command_keeps_the_next_message():
    """The writer peeked `home`, and while the send was in flight the turn was cancelled.
    The pop must not take `resume` (the next turn's command) in its place."""
    bus = CommandBus()
    await bus.dispatch("home", {}, turn_id="t1")
    in_flight = await bus.peek_outbound()
    await bus.dispatch("resume", {}, turn_id="t2")
    assert bus.cancel_turn("t1") == 1
    bus.pop_outbound(in_flight)
    assert (await bus.next_outbound()).verb == "resume"


async def test_pop_after_the_event_cap_dropped_the_in_flight_event_keeps_the_next_message():
    bus = CommandBus(max_queued_events=1)
    bus.publish(_event(0))
    in_flight = await bus.peek_outbound()
    bus.publish(_event(1))  # cap: drops the in-flight event 0 from the queue
    bus.pop_outbound(in_flight)
    assert (await bus.next_outbound()).text == "partial 1"
