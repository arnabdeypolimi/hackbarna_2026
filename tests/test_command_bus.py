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


@pytest.mark.parametrize("cancel", [False, True])
async def test_search_waits_past_the_old_deadline_until_reply_or_cancellation(cancel):
    bus = CommandBus()
    task = asyncio.create_task(
        bus.dispatch("search_catalog", {"query": "Frozen"}, turn_id="turn_1")
    )
    try:
        msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
        await asyncio.sleep(0.5)
        assert not task.done()
        assert bus.pending_count() == 1
        if cancel:
            bus.cancel_turn("turn_1")
            expected = {"status": "cancelled", "reason": "interrupted"}
        else:
            expected = {"titles": [{"title_id": "1", "name": "Frozen"}]}
            bus.resolve(msg.id, expected)
        assert await asyncio.wait_for(task, timeout=0.1) == expected
        assert bus.pending_count() == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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


async def test_search_ack_and_result_both_keep_command_correlation(otel):
    bus = CommandBus()
    task = asyncio.create_task(bus.dispatch("search_catalog", {"query": "space"}, "turn_1"))
    msg = await bus.next_outbound()
    bus.mark_sent(msg.id)
    bus.record_reply(msg.id, "ok", {"ok": True}, reply_type="ack")
    bus.resolve(msg.id, {"titles": []})
    bus.record_reply(msg.id, "ok", {"titles": []}, reply_type="result")
    await task
    replies = otel.spans()["tv.command_result"]
    assert [s.attributes["langfuse.observation.metadata.reply_type"] for s in replies] == ["ack", "result"]
    assert all(s.attributes["langfuse.observation.metadata.turn_id"] == "turn_1" for s in replies)
    assert replies[0].links[0].context == replies[1].links[0].context
    bus.record_reply(msg.id, "ok", {}, reply_type="result")
    assert len(otel.spans()["tv.command_result"]) == 2


async def test_command_lifecycle_distinguishes_queue_send_and_drop(otel):
    bus = CommandBus()
    await bus.dispatch("pause", {}, "turn_1")
    await bus.dispatch("home", {}, "turn_1")
    sent = await bus.next_outbound()
    bus.mark_sent(sent.id)
    assert bus.cancel_turn("turn_1") == 1
    states = [s.attributes["langfuse.observation.metadata.status"]
              for s in otel.spans()["tv.command_delivery"]]
    assert states == ["queued", "queued", "sent", "dropped"]


async def test_dispatch_opens_a_tv_command_span_that_covers_the_wait(otel):
    bus = CommandBus()
    await bus.dispatch("play", {"title_id": "1"}, turn_id="turn_1")
    task = asyncio.create_task(bus.dispatch("search_catalog", {"query": "tom hanks"}, turn_id="turn_1"))
    await asyncio.wait_for(bus.next_outbound(), timeout=0.1)          # play
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)    # search_catalog
    await asyncio.sleep(0.02)
    bus.resolve(msg.id, {"status": "ok", "titles": []})
    await asyncio.wait_for(task, timeout=0.1)
    play, search = otel.spans()["tv.command"]
    assert play.attributes["langfuse.observation.type"] == "tool"
    assert play.attributes["langfuse.observation.metadata.verb"] == "play"
    assert play.attributes["langfuse.observation.metadata.status"] == "dispatched"
    assert play.attributes["tv.command.awaits_result"] is False
    assert play.attributes["langfuse.observation.input"] == '{"title_id": "1"}'
    assert search.attributes["langfuse.observation.metadata.status"] == "ok"
    assert search.attributes["tv.command.id"] == msg.id
    assert (search.end_time - search.start_time) >= 20_000_000     # covered the 20 ms wait (ns)
    origin, _ = bus.origin(msg.id)
    assert origin.span_id == search.context.span_id
    assert bus.origin(msg.id) is None                                # handed out once
