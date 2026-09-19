import asyncio

import pytest

from tv_avatar.control.bus import CommandBus


async def test_barge_in_drops_unsent_commands_but_keeps_sent_ones():
    """Spec §9: queued commands for the interrupted turn are dropped;
    commands already handed to the socket are never rolled back."""
    bus = CommandBus()
    await bus.dispatch("play", {"title_id": "tt_1"}, turn_id="turn_1")
    sent = await bus.next_outbound()          # already delivered
    await bus.dispatch("home", {}, turn_id="turn_1")   # still queued

    dropped = bus.cancel_turn("turn_1")

    assert sent.verb == "play"                 # not rolled back
    assert dropped == 1
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_cancelling_one_turn_leaves_the_next_turn_intact():
    bus = CommandBus()
    await bus.dispatch("pause", {}, turn_id="turn_1")
    await bus.dispatch("resume", {}, turn_id="turn_2")
    assert bus.cancel_turn("turn_1") == 1
    assert (await bus.next_outbound()).verb == "resume"
