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


def test_bound_context_appears_in_format():
    setup_logging("INFO")
    captured: list[str] = []
    sink_id = logger.add(captured.append, format="{extra[session_id]} {extra[turn_id]} {message}")
    try:
        logger.bind(session_id="sess_1", turn_id="turn_1").info("turn")
    finally:
        logger.remove(sink_id)
    assert captured and captured[0].startswith("sess_1 turn_1 turn")
