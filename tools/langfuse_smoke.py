"""Five-second check that traces reach Langfuse — without speaking into a mic.

The OTLP handshake has four ways to fail silently (wrong region host, gRPC
exporter, header encoding, missing ``x-langfuse-ingestion-version: 4`` so data
lags ~10 minutes and looks "missing"). This exports one root span ``tv.smoke``
synchronously, reports the exporter's verdict, then reads the trace back from the
public API.

    uv run python tools/langfuse_smoke.py              # export + read back
    uv run python tools/langfuse_smoke.py --no-readback

Exit codes: 0 ok, 1 export or read-back failed, 2 tracing not configured.
"""
import argparse
import asyncio
import base64
import contextlib
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger
from opentelemetry import trace
from opentelemetry.processor.baggage import ALLOW_ALL_BAGGAGE_KEYS, BaggageSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from pydantic import ValidationError

from tv_avatar import tracing as tel
from tv_avatar.config import Settings
from tv_avatar.logging import flush_logging, setup_logging
from tv_avatar.session.state import SessionState
from tv_avatar.tracing import (
    ATTR_OBS_TYPE,
    OBS_TYPE_SPAN,
    build_exporter,
    session_scope,
    tracing_wanted,
)

SESSION_ID = "smoke"


def _settings() -> Settings | None:
    try:
        return Settings()
    except ValidationError as exc:
        missing = sorted(str(err["loc"][0]).upper() for err in exc.errors())
        print(f"not configured: .env is missing {', '.join(missing)}", file=sys.stderr)
        return None


def export_one(settings: Settings) -> bool:
    """One span through a SimpleSpanProcessor — the one permitted place for it,
    since the batch processor hides the HTTP verdict in its own thread."""
    exporter = build_exporter(settings)
    print(f"endpoint: {exporter._endpoint}")
    verdicts: list[str] = []

    class _Capture(logging.Handler):        # the exporter logs the HTTP status on failure
        def emit(self, record: logging.LogRecord) -> None:
            verdicts.append(record.getMessage())

    logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").addHandler(_Capture())

    outcome: list[SpanExportResult] = []

    class _Verdict(SimpleSpanProcessor):
        def on_end(self, span) -> None:
            outcome.append(self.span_exporter.export((span,)))

    # Not the global provider: this tool must not depend on TRACING_ENABLED's batch setup.
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    provider.add_span_processor(BaggageSpanProcessor(ALLOW_ALL_BAGGAGE_KEYS))
    provider.add_span_processor(_Verdict(exporter))
    session = SessionState(SESSION_ID, "tok", 0, user_id="smoke-user")
    with session_scope(session, settings), provider.get_tracer("tv_avatar.smoke").start_as_current_span(
            "tv.smoke", attributes={ATTR_OBS_TYPE: OBS_TYPE_SPAN, "tv.smoke.at": time.time()}):
        pass
    provider.shutdown()
    ok = bool(outcome) and outcome[0] is SpanExportResult.SUCCESS
    print(f"export: {'ok' if ok else 'FAILED'}" + (f" — {verdicts[-1]}" if verdicts else ""))
    return ok


def read_back(settings: Settings, attempts: int = 5) -> bool:
    """Ask the public API for the smoke session; a 200 on export but nothing here
    within a few seconds is the ingestion-version-header failure mode.

    Plain HTTP rather than `langfuse-cli`: the CLI refuses the v3 endpoints that
    a self-hosted v3 server still serves, and the v4 one 404s there. Try the v4
    observations API first, fall back to the v3 traces list."""
    host = settings.langfuse_host.rstrip("/")
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    urls = [f"{host}/api/public/v2/observations?sessionId={SESSION_ID}&limit=1&fromStartTime={since}",
            f"{host}/api/public/traces?sessionId={SESSION_ID}&limit=1"]
    token = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
    for attempt in range(1, attempts + 1):
        for url in urls:
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    continue
                print(f"read-back: HTTP {err.code} from {url}", file=sys.stderr)
                return False
            except (urllib.error.URLError, json.JSONDecodeError) as err:
                print(f"read-back: {err}", file=sys.stderr)
                return False
            if body.get("data"):
                print(f"read-back: trace visible via {url.split('?')[0]} (attempt {attempt})")
                return True
        time.sleep(2)
    print("read-back: FAILED — exported but not visible after ~10 s; check the "
          "x-langfuse-ingestion-version header and the region host", file=sys.stderr)
    return False


async def synthetic_turn(settings: Settings) -> tuple[str, str]:
    from pipecat.frames.frames import AggregatedTextFrame, LLMContextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.tests.utils import run_test

    from tv_avatar.agent.service import SGRAgentService
    from tv_avatar.control.bus import CommandBus
    from tv_avatar.memory.fake import FakeMemoryLane

    settings = settings.model_copy(update={"agent_impl": "sgr", "llm_model": "synthetic-fixture",
                                           "nebius_base_url": "http://synthetic.invalid",
                                           "agent_max_cycles": 2})
    session = SessionState("telemetry-smoke-" + uuid.uuid4().hex[:12], "synthetic-token", 0,
                           user_id="telemetry-smoke")
    bus = CommandBus()
    scripts = iter([
        {"intent": "search", "request": {"operation": "lookup", "title": None, "title_id": None}, "say": "Let me look.",
         "actions": [{"verb": "search_catalog", "query": "space"}]},
        {"intent": "search", "request": {"operation": "lookup", "title": None, "title_id": "17431"}, "say": "I found Moon.",
         "actions": [{"verb": "show_titles", "title_ids": ["17431"], "label": "Space films"}]},
    ])

    async def create(**kwargs):
        async def chunks():
            yield SimpleNamespace(model="synthetic-fixture", id="synthetic-completion",
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
                choices=[SimpleNamespace(delta=SimpleNamespace(content=json.dumps(next(scripts))),
                                         finish_reason="stop")])
        return chunks()

    async def tv():
        while True:
            msg = await bus.next_outbound()
            bus.mark_sent(msg.id)
            bus.record_reply(msg.id, "ok", {"ok": True}, reply_type="ack")
            if msg.verb == "search_catalog":
                result = {"status": "ok", "titles": [{"title_id": "17431", "name": "Moon"}]}
                bus.resolve(msg.id, result)
                bus.record_reply(msg.id, "ok", result, reply_type="result")

    spoken = []

    class Sink(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, AggregatedTextFrame):
                spoken.append(frame.text)
            await self.push_frame(frame, direction)

    agent = SGRAgentService(settings, bus, FakeMemoryLane(), None, None, session,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    original_setup = agent.setup

    async def setup(cfg):
        await original_setup(cfg)
        agent._tracing_enabled = True

    agent.setup = setup
    context = LLMContext([{"role": "user", "content": "Find a space film."}])
    with session_scope(session, settings), tel.observation("telemetry.synthetic_turn", type="agent", **{
        tel.ATTR_OBS_INPUT: "Find a space film.",
    }) as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        tv_task = asyncio.create_task(tv())
        try:
            await run_test(Pipeline([agent, Sink()]),
                           frames_to_send=[LLMContextFrame(context=context)],
                           expected_down_frames=None, start_timeout=5)
            await asyncio.sleep(0)
            assert spoken == ["Let me look.", "I found Moon."], spoken
            root.set_attribute(tel.ATTR_OBS_OUTPUT, " ".join(spoken))
        finally:
            tv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tv_task
    return session.session_id, trace_id


def audit_synthetic(settings: Settings, session_id: str, trace_id: str) -> bool:
    token = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
    url = settings.langfuse_host.rstrip("/") + "/api/public/traces/" + trace_id
    required = {"agent.cycle", "agent.plan", "agent.action", "agent.speech", "agent.feedback"}
    for _ in range(10):
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.load(response)
        except urllib.error.HTTPError as err:
            if err.code != 404:
                logger.error("telemetry.audit.failed", status=err.code)
                return False
        else:
            observations = data.get("observations", [])
            names = {item["name"] for item in observations}
            cycles = [item for item in observations if item["name"] == "agent.cycle"]
            plans = [item for item in observations if item["name"] == "agent.plan"]
            content_ok = (all(item.get("output") for item in cycles + plans) if settings.trace_content
                          else all(item.get("output") is None and item.get("input") is None
                                   for item in observations))
            schema_names = {item.get("metadata", {}).get("schema_name") for item in cycles}
            types_ok = all(item.get("type") == "GENERATION" for item in cycles)
            agent_ok = any(item["name"] == "llm" and item.get("type") == "AGENT" for item in observations)
            if (required <= names and len(cycles) == len(plans) == 2 and content_ok and types_ok
                    and agent_ok and schema_names == {"turn_plan", "turn_plan_final"}):
                list_url = settings.langfuse_host.rstrip("/") + "/api/public/traces?sessionId=" + session_id
                with urllib.request.urlopen(urllib.request.Request(
                        list_url, headers={"Authorization": f"Basic {token}"}), timeout=10) as response:
                    traces = json.load(response).get("data", [])
                linked = []
                for item in traces:
                    if item["id"] == trace_id:
                        continue
                    detail_url = settings.langfuse_host.rstrip("/") + "/api/public/traces/" + item["id"]
                    with urllib.request.urlopen(urllib.request.Request(
                            detail_url, headers={"Authorization": f"Basic {token}"}), timeout=10) as response:
                        linked.extend(json.load(response).get("observations", []))
                replies = [item for item in linked if item["name"] == "tv.command_result"]
                deliveries = [item for item in linked if item["name"] == "tv.command_delivery"]
                if len(replies) != 3 or len(deliveries) != 4:
                    time.sleep(2)
                    continue
                if not settings.trace_content and any(item.get("input") is not None or
                                                       item.get("output") is not None for item in linked):
                    logger.error("telemetry.audit.content_leak", session_id=session_id)
                    return False
                logger.info("telemetry.audit.passed", event="telemetry.audit.passed",
                            session_id=session_id, trace_id=trace_id,
                            observations=len(observations) + len(linked), command_replies=len(replies))
                return True
        time.sleep(2)
    logger.error("telemetry.audit.missing_observations", event="telemetry.audit.missing_observations",
                 session_id=session_id, trace_id=trace_id)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-readback", action="store_true", help="export only")
    parser.add_argument("--agent-fixture", action="store_true", help="trace a synthetic two-cycle search; no model calls")
    args = parser.parse_args()
    settings = _settings()
    if settings is None:
        return 2
    if not tracing_wanted(settings):
        print("not configured: set TRACING_ENABLED=true and LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
              "(or OTEL_EXPORTER_OTLP_ENDPOINT) in .env", file=sys.stderr)
        return 2
    if args.agent_fixture:
        setup_logging(settings.log_level, settings=settings)
        tel.setup_tracing(settings)
        try:
            session_id, trace_id = asyncio.run(synthetic_turn(settings))
        finally:
            tel.shutdown_tracing()
        ok = (args.no_readback or bool(settings.otel_exporter_otlp_endpoint)
              or audit_synthetic(settings, session_id, trace_id))
        flush_logging()
        return 0 if ok else 1
    if not export_one(settings):
        return 1
    if args.no_readback or settings.otel_exporter_otlp_endpoint:
        return 0
    return 0 if read_back(settings) else 1


if __name__ == "__main__":
    if "--agent-fixture" not in sys.argv:
        trace.set_tracer_provider(trace.NoOpTracerProvider())  # nothing else may install one here
    sys.exit(main())
