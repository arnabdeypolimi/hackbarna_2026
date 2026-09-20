import logging

from loguru import logger

from tv_avatar.logging import setup_logging


def test_stdlib_records_are_routed_into_loguru():
    setup_logging("DEBUG")
    captured: list[str] = []
    sink_id = logger.add(captured.append, format="{extra} {message}")
    try:
        logging.getLogger("mem0").warning("memory graph %s", "warm")
    finally:
        logger.remove(sink_id)
    assert any("memory graph warm" in line for line in captured)


def test_json_logs_are_correlated_and_content_policy_applies(capsys, otel):
    import json

    from tv_avatar.tracing import observation

    setup_logging("DEBUG", json_output=True, content=False)
    with observation("test", type="span") as span:
        logger.bind(session_id="s", turn_id="t").info(
            "agent.cycle.completed", event="agent.cycle.completed", raw="private-payload")
    from tv_avatar.logging import flush_logging
    flush_logging()
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    record = next(line for line in lines if line["event"] == "agent.cycle.completed")
    assert record["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert record["turn_id"] == "t"
    assert "private-payload" not in str(record)
    setup_logging("INFO")


def test_slow_log_sink_drops_instead_of_blocking():
    from threading import Event
    from types import SimpleNamespace

    from tv_avatar.logging import BackgroundSink

    entered, release = Event(), Event()

    class Slow:
        def write(self, value):
            entered.set()
            release.wait(2)

        def flush(self):
            pass

    sink = BackgroundSink(Slow(), json_output=False, capacity=1)
    message = SimpleNamespace(record={})
    try:
        sink.write(message)
        assert entered.wait(1)
        for _ in range(10):
            sink.write(message)
        assert sink.dropped == 9
    finally:
        release.set()
        sink.stop()


def test_bound_context_appears_in_format():
    setup_logging("INFO")
    captured: list[str] = []
    sink_id = logger.add(captured.append, format="{extra[session_id]} {extra[turn_id]} {message}")
    try:
        logger.bind(session_id="sess_1", turn_id="turn_1").info("turn")
    finally:
        logger.remove(sink_id)
    assert captured and captured[0].startswith("sess_1 turn_1 turn")
