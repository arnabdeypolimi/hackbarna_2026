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
import base64
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opentelemetry import trace
from opentelemetry.processor.baggage import ALLOW_ALL_BAGGAGE_KEYS, BaggageSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from pydantic import ValidationError

from tv_avatar.config import Settings
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-readback", action="store_true", help="export only")
    args = parser.parse_args()
    settings = _settings()
    if settings is None:
        return 2
    if not tracing_wanted(settings):
        print("not configured: set TRACING_ENABLED=true and LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
              "(or OTEL_EXPORTER_OTLP_ENDPOINT) in .env", file=sys.stderr)
        return 2
    if not export_one(settings):
        return 1
    if args.no_readback or settings.otel_exporter_otlp_endpoint:
        return 0
    return 0 if read_back(settings) else 1


if __name__ == "__main__":
    trace.set_tracer_provider(trace.NoOpTracerProvider())  # nothing else may install one here
    sys.exit(main())
