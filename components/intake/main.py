"""Component A — gliq-intake.

Public HTTPS entry point. Accepts a game design document, extracts a structured
`PitchProfile`, and publishes a `ScoringRequested` message for Component B.

This is the only publicly reachable component. Runs behind nginx + certbot at
greenlightiq.fredt.io, as the systemd unit `gliq-intake`.

⚠️ The unit runs `uvicorn components.intake.main:app`, so this module MUST
expose an ASGI `app` at import time. An earlier stub exposed only `main()` and
failed the deploy.

## Layering

    main.py         <- this file: HTTP, publishing, lifecycle
    providers.py    <- provider selection (deterministic | fixture | ⏳ LLM)
    extract.py      <- pure functions; deterministic extraction
    vocabulary.py   <- the committed corpus vocabulary

Extraction is deliberately reachable without any of the transport above it, so
it can be tested offline against the same code that runs here.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from components.intake import auth, db, vocabulary, web
from components.intake.providers import get_extractor
from shared.config import Config
from shared.schemas import PitchProfile, ScoringRequested

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",  # systemd already stamps the time
)
log = logging.getLogger("gliq-intake")

#: How long to wait for Pub/Sub to confirm a publish. The producer is holding an
#: HTTP connection open for this, so it is deliberately shorter than Component
#: B's 30s — a request that cannot be published should fail while someone is
#: still watching, not eventually.
PUBLISH_TIMEOUT_SECONDS = 10


class PitchSubmission(BaseModel):
    """What a producer posts.

    ⚠️ `document` is the ONLY requirement, and the requirement is that it exists
    — not that it says anything in particular. A document that yields no genre
    and no tags is published anyway, and Component B reports it as insufficient
    information. That distinction (`we cannot evaluate this` vs `we evaluated it
    and it is bad`) is scoring's to make, and it cannot make it if intake
    rejects the submission first. ➡️ docs/SCORING.md
    """

    document: str = Field(min_length=1, description="The design document, as text or markdown")
    title: str | None = Field(
        default=None,
        description="Overrides the title extracted from the document's heading",
    )


class PitchAccepted(BaseModel):
    """202 response. The profile is echoed so the producer can see what was read."""

    pitch_id: str
    profile: PitchProfile
    extracted_by: str


class Intake:
    """Holds the long-lived clients a request handler needs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.extractor = get_extractor(config.llm_provider)
        self.publisher: Any = None
        self.topic: str | None = None
        self.publish_error: str | None = None

        # ⚠️ Read-only, and opened without blocking startup — a database
        # outage must degrade the review pages, not stop intake accepting
        # pitches. ➡️ db.py
        self.pool = db.make_pool(config.dsn)

        # ⚠️ Imported here, not at module scope: this keeps `extract`,
        # `vocabulary` and the whole test suite runnable without
        # google-cloud-pubsub installed and without credentials.
        from google.cloud import pubsub_v1

        try:
            self.publisher = pubsub_v1.PublisherClient()
            self.topic = self.publisher.topic_path(
                config.gcp_project, config.topic_scoring_requested
            )
        except Exception as exc:  # noqa: BLE001 — recorded, then reported by /healthz
            # Deliberately NOT fatal, which is a divergence from Component B.
            #
            # B has no human-facing surface, so exiting and letting systemd
            # report `failed` is the clearest signal available to it. A is the
            # one component someone points a browser at: if the process dies,
            # nginx answers 502 and the cause is invisible from outside. A
            # process that stays up and says "pubsub unavailable: <reason>" on
            # /healthz is strictly more debuggable, and /pitches still refuses
            # honestly with 503 rather than accepting work it cannot deliver.
            self.publish_error = str(exc)
            log.error("❌ Pub/Sub publisher unavailable: %s", exc)

    def publish_profile(self, profile: PitchProfile, document: str | None = None) -> str:
        """Publish a profile for scoring and return the new pitch id.

        The single path a pitch takes to Component B. Both the JSON API and the
        web form funnel through here, so the two cannot drift.

        ⚠️ `document` is the raw submitted text, carried so Component B can
        persist it and Component C's analyst can read the prose. It is the real
        document regardless of extractor — `fixture` changes what is extracted,
        never what the producer actually wrote.
        """
        request = ScoringRequested(profile=profile, document=document)
        self.publish(request)
        return str(request.pitch_id)

    def publish(self, request: ScoringRequested) -> None:
        """Publish a scoring request, or raise 503."""
        if self.publisher is None or self.topic is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Pub/Sub is unavailable, so the pitch cannot be queued: "
                f"{self.publish_error}",
            )
        try:
            future = self.publisher.publish(self.topic, request.model_dump_json().encode())
            future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as 503
            log.exception("❌ publishing %s failed", request.pitch_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Could not queue the pitch for scoring: {exc}",
            ) from exc


#: Populated by the lifespan handler below.
intake: Intake | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the clients once, at startup — never per request."""
    global intake

    config = Config.from_env()
    log.info("🚀 Component A starting")
    log.info(
        "   project=%s topic=%s provider=%s",
        config.gcp_project,
        config.topic_scoring_requested,
        config.llm_provider,
    )

    intake = Intake(config)
    app.state.intake = intake
    log.info(
        "✅ extractor=%s vocabulary=%s tags from %s titles",
        intake.extractor.name,
        len(vocabulary.TAG_COUNTS),
        f"{vocabulary.corpus_titles():,}",
    )
    if intake.publish_error is None:
        log.info("✅ listening — publishing to %s", intake.topic)
    if not config.admin_password_hash:
        # 🔒 Loud, because the failure is silent from the outside: the UI simply
        # rejects every login, which looks identical to a forgotten password.
        log.error(
            "🔒 ADMIN_PASSWORD_HASH is not set — every login will be refused. "
            "Generate one with infra/scripts/hash-password.py"
        )

    yield

    try:
        intake.pool.close()
    except Exception:  # noqa: BLE001 — shutdown must not raise
        pass
    log.info("👋 stopped")


app = FastAPI(
    title="GreenlightIQ — intake",
    description=(
        "Submit a game design document and receive a market fitment review. "
        "Component A of a three-process pipeline."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ⚠️ Read at import time, not from Config: middleware is installed before the
# lifespan handler runs, so the app object cannot wait for startup to learn its
# own signing key.
#
# 🔒 An unset SESSION_SECRET falls back to a per-process random value. That
# invalidates every session on restart — deliberately noisy — rather than
# signing cookies with a predictable constant, which would let anyone forge a
# session and walk straight past the login gate.
_session_secret = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
if not os.getenv("SESSION_SECRET"):
    log.warning(
        "🔒 SESSION_SECRET is not set — using an ephemeral key. Logins will not "
        "survive a restart. Set it with `pulumi config set --secret sessionSecret`."
    )

@app.middleware("http")
async def require_login(request: Request, call_next):
    """🔒 The gate, enforced before routing.

    ⚠️ This has to be middleware rather than a per-route dependency. FastAPI
    validates the request body BEFORE a handler runs, so an in-handler check
    never fires on a malformed request — `POST /pitches` with a bad body
    answered 422 to an anonymous caller, telling an unauthenticated stranger
    about the endpoint's schema. Checked here, authentication precedes parsing.

    Per-route `auth.require_session()` calls are kept as well. They are
    redundant while this is correct, which is the point: an edit to PUBLIC_PATHS
    cannot silently expose a handler.
    """
    if request.url.path not in auth.PUBLIC_PATHS and not auth.current_user(request):
        return auth.redirect_to_login(request)
    return await call_next(request)


# ⚠️ Order matters and is not obvious. Starlette runs the LAST-added middleware
# outermost, so SessionMiddleware must be added AFTER the gate above — otherwise
# the gate runs first, `request.session` does not exist yet, and every request
# fails with an AssertionError instead of being authenticated.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="gliq_session",
    max_age=8 * 60 * 60,
    same_site="lax",
    # ⚠️ Requires nginx to forward X-Forwarded-Proto; without it the app sees
    # plain HTTP behind the proxy, refuses to set the cookie, and every login
    # silently bounces back to the form. ➡️ infra/scripts/nginx-gliq.conf
    https_only=True,
)

app.include_router(web.router)


@app.exception_handler(auth.NotLoggedIn)
async def _not_logged_in(request: Request, _exc: auth.NotLoggedIn):
    """Send a browser to the login form instead of returning a bare 401."""
    return auth.redirect_to_login(request)


def _require_intake() -> Intake:
    if intake is None:  # pragma: no cover — lifespan always runs first
        raise HTTPException(status_code=503, detail="Service is still starting")
    return intake


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness and configuration, for deployment verification.

    ⚠️ Deliberately makes NO Pub/Sub call. The intake service account holds
    `roles/pubsub.publisher` but not `pubsub.viewer`, so `get_topic()` would
    return 403 on a perfectly healthy deployment — a health check that fails
    when the system is fine is worse than none. What it reports instead is
    whether the publisher CLIENT was constructed, which is the part that can
    actually be misconfigured on this box.
    """
    current = _require_intake()
    healthy = current.publish_error is None
    body = {
        "status": "ok" if healthy else "degraded",
        "provider": current.extractor.name,
        "topic": current.config.topic_scoring_requested,
        "vocabulary_tags": len(vocabulary.TAG_COUNTS),
        "corpus_titles": vocabulary.corpus_titles(),
    }
    if not healthy:
        body["error"] = current.publish_error
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=body)
    return body


@app.post("/pitches", status_code=status.HTTP_202_ACCEPTED, response_model=PitchAccepted)
def submit_pitch(
    request: Request,
    submission: Annotated[PitchSubmission, Body()],
) -> PitchAccepted:
    """Accept a design document, extract a profile, queue it for scoring.

    202, not 201: scoring happens asynchronously in Component B, so nothing is
    created yet at a URL the producer could fetch. The extracted profile comes
    back in the response because it is the one part of the pipeline the producer
    can sanity-check immediately — "you read my $19.99 metroidvania as a free
    puzzle game" is worth knowing before the report arrives.

    ⚠️ A sync `def`, so Starlette runs it in a threadpool: both the extraction
    and the publish confirmation block, and an `async def` would stall the event
    loop for every other request.

    🔒 Requires a session, like every other route except `/healthz`. This is a
    public endpoint that queues work and — once Part 2 lands — spends LLM credits
    per call, so leaving it open would make the login gate on the UI decorative.
    """
    auth.require_session(request)
    current = _require_intake()

    profile = current.extractor.extract(submission.document)
    if submission.title:
        profile.title = submission.title

    request = ScoringRequested(profile=profile, document=submission.document)
    current.publish(request)

    log.info(
        "✅ accepted %s — %s (%s, %d tags) via %s",
        request.pitch_id,
        profile.title or "untitled",
        profile.primary_genre or "no genre",
        len(profile.tags),
        current.extractor.name,
    )
    if not profile.primary_genre and not profile.tags:
        # Not an error — B will report it as insufficient information — but it
        # is the one outcome where the producer's document, not the system, is
        # the problem, and it is worth seeing in the journal.
        log.warning("⚠️ %s matched no genre and no tags", request.pitch_id)

    return PitchAccepted(
        pitch_id=str(request.pitch_id),
        profile=profile,
        extracted_by=current.extractor.name,
    )
