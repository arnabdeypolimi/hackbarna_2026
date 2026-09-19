import asyncio

import pytest

from tv_avatar.control.bus import CommandBus


async def test_fire_and_forget_returns_immediately():
    bus = CommandBus()
    result = await asyncio.wait_for(
        bus.dispatch("pause", {}, turn_id="turn_1"), timeout=0.1
    )
    assert result == {"status": "dispatched"}


async def test_dispatched_command_appears_on_the_outbound_queue():
    bus = CommandBus()
    await bus.dispatch("play", {"title_id": "tt_9"}, turn_id="turn_1")
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert msg.verb == "play"
    assert msg.args["title_id"] == "tt_9"
    assert msg.turn_id == "turn_1"


async def test_invalid_command_never_reaches_the_queue():
    bus = CommandBus()
    with pytest.raises(ValueError):
        await bus.dispatch("play", {}, turn_id="turn_1")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_cancel_turn_drops_unsent_commands_for_that_turn_only():
    bus = CommandBus()
    await bus.dispatch("pause", {}, turn_id="turn_1")
    await bus.dispatch("home", {}, turn_id="turn_1")
    await bus.dispatch("back", {}, turn_id="turn_2")
    dropped = bus.cancel_turn("turn_1")
    assert dropped == 2
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert msg.verb == "back"


async def test_search_catalog_awaits_and_resolves():
    bus = CommandBus()
    task = asyncio.create_task(
        bus.dispatch("search_catalog", {"query": "tom hanks"}, turn_id="turn_1")
    )
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    bus.resolve(msg.id, {"titles": [{"title_id": "tt_5", "name": "Big"}]})
    result = await asyncio.wait_for(task, timeout=0.1)
    assert result["titles"][0]["name"] == "Big"


async def test_search_catalog_degrades_on_timeout():
    bus = CommandBus(search_timeout_s=0.05)
    result = await asyncio.wait_for(
        bus.dispatch("search_catalog", {"query": "x"}, turn_id="turn_1"), timeout=0.5
    )
    assert result["status"] == "unavailable"
    assert bus.pending_count() == 0


async def test_cancel_turn_releases_a_pending_search_immediately():
    bus = CommandBus(search_timeout_s=5.0)  # long: the test must not wait this out
    task = asyncio.create_task(
        bus.dispatch("search_catalog", {"query": "x"}, turn_id="turn_1")
    )
    await asyncio.wait_for(bus.next_outbound(), timeout=0.1)  # handed to the socket

    bus.cancel_turn("turn_1")

    result = await asyncio.wait_for(task, timeout=0.1)
    assert result["status"] == "cancelled"
    assert bus.pending_count() == 0


async def test_cancel_turn_leaves_other_turns_searches_pending():
    bus = CommandBus(search_timeout_s=5.0)
    task = asyncio.create_task(
        bus.dispatch("search_catalog", {"query": "x"}, turn_id="turn_2")
    )
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    bus.cancel_turn("turn_1")
    assert bus.pending_count() == 1
    bus.resolve(msg.id, {"titles": []})
    assert (await asyncio.wait_for(task, timeout=0.1)) == {"titles": []}
