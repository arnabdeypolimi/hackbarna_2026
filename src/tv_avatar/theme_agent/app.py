"""The fal proxy the browser-side weather agent talks through.

`fal.realtime.open(wma(...))` in @fal-ai/client posts to fal's signalling bridge with
the caller's credentials. A browser must not hold the key, so the SDK's `proxyUrl`
option sends every one of those calls here instead: the real URL rides in
`x-fal-target-url`, and this app adds `Authorization: Key ...` and relays the answer.
Media never passes through it; once the SDP is exchanged the video flows from fal
straight to the browser.

This is deliberately not fal's generic server proxy. That one forwards to any fal host,
which on an unauthenticated route would let anyone spend the key on any model. This one
allows the three bridge calls a Director session makes, for one configured model, and
counts the sessions it opens.
"""
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from tv_avatar.theme_agent.settings import ThemeAgentSettings

TARGET_URL_HEADER = "x-fal-target-url"
BRIDGE_HOST = "wma.fal.run"
#: The calls the SDK's WMA transport makes: TURN credentials, the SDP offer, and the
#: lease heartbeat. Anything else is somebody using this route for something it is not.
BRIDGE_PATHS = frozenset({"/ice", "/session", "/session/heartbeat"})
#: Any port on a loopback name, and nothing that merely starts with one: the pattern is
#: matched against the whole origin, so http://localhost.evil.example does not pass.
LOOPBACK_ORIGIN = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
#: An SDP offer is a few KB; a body past this is not one.
MAX_BODY_BYTES = 64 * 1024
#: /session waits for fal to find a runner, so it gets far longer than the others.
SESSION_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class SessionBudget:
    """Sessions opened per UTC day. In memory: a restart resets it, which errs toward
    letting a session through and is not worth a database for a dev-scale guard."""

    def __init__(self, cap: int, today: Callable[[], date] | None = None) -> None:
        self.cap = cap
        self._today = today or (lambda: datetime.now(UTC).date())
        self._day = self._today()
        self.used = 0

    def take(self) -> bool:
        if (today := self._today()) != self._day:
            self._day, self.used = today, 0
        if self.used >= self.cap:
            return False
        self.used += 1
        return True

    def refund(self) -> None:
        self.used = max(0, self.used - 1)


def create_theme_agent_app(
    settings: ThemeAgentSettings | None = None,
    client: httpx.AsyncClient | None = None,
    budget: SessionBudget | None = None,
) -> FastAPI:
    settings = settings or ThemeAgentSettings()
    budget = budget or SessionBudget(settings.theme_agent_max_sessions_per_day)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = client or httpx.AsyncClient()
        try:
            yield
        finally:
            if client is None:
                await app.state.http.aclose()

    app = FastAPI(title="theme-agent", lifespan=lifespan)
    loopback = LOOPBACK_ORIGIN if settings.theme_agent_allow_localhost else None
    # Browsers preflight the custom target header. No credentials are involved, so a
    # wildcard on headers is safe; the origins are what limit which pages can call this.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_origin_regex=loopback,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        max_age=600,
    )

    # Judged once, up front: a key that cannot be sent is the same on every request.
    key_problem = settings.fal_key_problem

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "configured": key_problem is None,
            "problem": key_problem,
            "endpoint": settings.theme_agent_endpoint,
            "sessions_left_today": max(0, budget.cap - budget.used),
        }

    @app.post("/fal/proxy")
    async def proxy(request: Request) -> Response:
        if key_problem:
            raise HTTPException(503, key_problem)

        target = urlsplit(request.headers.get(TARGET_URL_HEADER, ""))
        # netloc, not hostname: it keeps userinfo and ports in view, so
        # https://wma.fal.run@elsewhere.example/ does not pass as the bridge.
        if not (
            target.scheme == "https"
            and target.netloc == BRIDGE_HOST
            and target.path in BRIDGE_PATHS
            and not target.query
        ):
            raise HTTPException(403, "That target is not allowed")

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Request body too large")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise HTTPException(400, "The body must be a JSON object")

        # A heartbeat carries only a session id; ice and session name the model.
        if target.path != "/session/heartbeat" and (
            payload.get("app_id") != settings.theme_agent_endpoint
        ):
            raise HTTPException(403, "That endpoint is not allowed")

        opening = target.path == "/session"
        if opening and not budget.take():
            raise HTTPException(429, "The daily backdrop limit has been reached")

        try:
            upstream = await request.app.state.http.post(
                target.geturl(),
                content=body,
                headers={
                    "Authorization": f"Key {settings.fal_key}",
                    "Content-Type": "application/json",
                },
                timeout=SESSION_TIMEOUT if opening else DEFAULT_TIMEOUT,
            )
        except httpx.TimeoutException:
            if opening:
                budget.refund()
            raise HTTPException(504, "fal did not answer in time") from None
        except Exception as exc:  # noqa: BLE001 - any failure means nothing started
            # The request never reached fal, so nothing is charged to the day. It is
            # answered here, not left to crash: an unhandled error carries no CORS
            # headers, so the browser reports it only as "Failed to fetch". Logged by
            # type and message, not as a traceback: loguru prints the locals of one,
            # and the request's headers hold the key.
            if opening:
                budget.refund()
            logger.error("theme agent: could not forward {}: {}: {}",
                         target.path, type(exc).__name__, exc)
            raise HTTPException(502, "fal could not be reached") from None

        if opening:
            if upstream.status_code >= 400:
                budget.refund()  # nothing was started, so nothing to charge to the day
            else:
                logger.info("theme agent: session opened ({}/{} today)",
                            budget.used, budget.cap)
        # Status, type and body only: fal's own headers are not this app's to relay.
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    return app
