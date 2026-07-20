"""The review UI — Component A's browser-facing routes.

Server-rendered Jinja. No build step, no JavaScript framework, no external
assets: the whole UI is a handful of templates and one stylesheet inlined in the
base layout, which keeps the public surface small and means nothing here can
fail to load from a CDN.

## Layering

    web.py    <- this file: HTML routes, form handling
    auth.py   <- pure; password verification and the session gate
    db.py     <- the only code in A that touches Postgres, SELECT only
    main.py   <- the ASGI app, the JSON API, publishing

⚠️ Submission is REUSED, not reimplemented. `POST /submit` funnels into the same
extractor and publisher as the JSON `POST /pitches`, so the browser path and the
API path cannot drift into scoring things differently.

⚠️ Nothing here writes to the database. A pitch reaches Postgres only by being
published to Pub/Sub and picked up by Component B. ➡️ db.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown as markdown_lib
from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from components.intake import auth, db

log = logging.getLogger("gliq-intake")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Where bundled example pitches live, relative to the repo root on the VM.
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

#: Upload ceiling. nginx caps the body at 2M as well; this is the second line so
#: a direct request to uvicorn cannot bypass it.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

MARKDOWN_EXTENSIONS = ["tables", "sane_lists"]


def render_markdown(text: str | None) -> str:
    """Render a stored report to HTML, with raw HTML neutralised.

    🛑 Python-Markdown passes raw HTML STRAIGHT THROUGH by default. An earlier
    version of this function relied on "not enabling any HTML extension" as a
    safeguard, which is not how the library works — `<script>` in the source
    reached the page intact. Its old `safe_mode` was removed in 3.x, and the
    documented answer is to sanitise, which would mean another dependency.

    So the angle brackets are escaped BEFORE conversion. Markdown's own syntax
    uses `#`, `|`, `*`, `-` and never `<>`, so every structural element still
    renders — only embedded HTML is defused.

    ⚠️ `&` is deliberately NOT escaped here: Python-Markdown already converts a
    bare `&` to `&amp;`, and pre-escaping would double it into a visible
    `&amp;amp;` in titles like "Skinny & Franko".

    This is the SECOND line of defence. The first is `reporting.render.escape_md`,
    which strips these characters when the report is written. Both exist because
    the report mixes system text with a producer's own words.
    """
    if not text:
        return ""
    defused = text.replace("<", "&lt;").replace(">", "&gt;")
    return markdown_lib.markdown(defused, extensions=MARKDOWN_EXTENSIONS)


#: The worked example shown beside the submit form.
#:
#: ⚠️ It is a REAL FILE in samples/, not prose embedded in a template, and it
#: is verified by a test to extract to full coverage with no completeness cap.
#: An example that would itself be capped teaches the wrong shape, and an
#: example that drifts from what the extractor actually reads is worse than
#: none — producers would follow it and score lower for doing so.
EXAMPLE_PITCH = "minimal-pitch.md"


def available_samples() -> list[str]:
    """Bundled example pitches, by filename."""
    if not SAMPLES_DIR.is_dir():
        return []
    return sorted(p.name for p in SAMPLES_DIR.glob("*.md"))


def example_pitch_text() -> str:
    """The worked example, or empty if it is missing.

    Missing must degrade to hiding the panel, never to a broken page — this is
    guidance, and guidance is not worth a 500.
    """
    path = SAMPLES_DIR / EXAMPLE_PITCH
    try:
        return path.read_text()
    except OSError:
        log.warning("⚠️ example pitch %s is missing — the guidance panel is hidden", path)
        return ""


def page(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a template with the values every page needs.

    ⚠️ `status_code` is an explicit parameter, not part of `**context`. Left in
    the context it would be passed to Jinja as a template variable and the
    response would silently return 200 — a failed login answering 200 OK.
    """
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"user": auth.current_user(request), **context},
        status_code=status_code,
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    if auth.current_user(request):
        return RedirectResponse(url=auth.safe_next(next), status_code=303)
    return page(request, "login.html", next=auth.safe_next(next), error=None)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    """Check the credential and start a session.

    ⚠️ One failure message for every cause — wrong user, wrong password, or no
    password configured at all. Distinguishing them tells an attacker which half
    they got right, and tells a scanner that the instance is unconfigured.
    """
    config = request.app.state.intake.config
    ok = username == config.admin_user and auth.verify_password(
        password, config.admin_password_hash
    )
    if not ok:
        log.warning("🔒 failed login for %r from %s", username[:64], request.client.host)
        return page(
            request,
            "login.html",
            next=auth.safe_next(next),
            error="Incorrect username or password.",
            status_code=401,
        )

    auth.log_in(request, username)
    log.info("🔒 %s logged in from %s", username, request.client.host)
    return RedirectResponse(url=auth.safe_next(next), status_code=303)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    auth.log_out(request)
    return RedirectResponse(url=auth.LOGIN_PATH, status_code=303)


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    auth.require_session(request)
    pool = request.app.state.intake.pool

    recent: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    db_error = None
    try:
        recent = db.list_pitches(pool, limit=10)
        counts = db.status_counts(pool)
    except Exception as exc:  # noqa: BLE001 — the DB is not required to submit
        # ⚠️ Degrade, do not fail. Submitting a pitch does not touch Postgres,
        # so the form must still work when the review side is unavailable.
        db_error = str(exc)
        log.warning("⚠️ dashboard could not read pitches: %s", exc)

    return page(
        request,
        "index.html",
        recent=recent,
        counts=counts,
        samples=available_samples(),
        db_error=db_error,
        example=example_pitch_text(),
    )


@router.post("/submit")
async def submit(
    request: Request,
    document: UploadFile | None = None,
    sample: str = Form(""),
    title: str = Form(""),
):
    """Accept an uploaded document or a bundled sample, then publish it.

    ⚠️ Funnels into the same extractor and publisher as `POST /pitches`.
    """
    auth.require_session(request)
    intake = request.app.state.intake

    text, source, error = await _read_submission(document, sample)
    if error:
        return page(
            request,
            "index.html",
            recent=[],
            counts={},
            samples=available_samples(),
            db_error=None,
            error=error,
            example=example_pitch_text(),
            status_code=400,
        )

    profile = intake.extractor.extract(text)
    if title.strip():
        profile.title = title.strip()

    request_id = intake.publish_profile(profile)
    log.info(
        "✅ accepted %s via UI (%s) — %s (%s, %d tags)",
        request_id,
        source,
        profile.title or "untitled",
        profile.primary_genre or "no genre",
        len(profile.tags),
    )
    return RedirectResponse(url=f"/pitches/{request_id}", status_code=303)


async def _read_submission(
    document: UploadFile | None, sample: str
) -> tuple[str, str, str | None]:
    """Text, a label for the journal, and an error message if unusable."""
    if sample:
        # ⚠️ Resolve and confine to SAMPLES_DIR. `sample` is form input, so
        # accepting it as a path would be a directory traversal — `..%2f..%2f`
        # reading /etc/gliq/gliq.env is the exact attack.
        candidate = (SAMPLES_DIR / sample).resolve()
        if candidate.parent != SAMPLES_DIR.resolve() or not candidate.is_file():
            return "", "", "That sample does not exist."
        return candidate.read_text(), f"sample:{sample}", None

    if document is None or not document.filename:
        return "", "", "Choose a file to upload, or pick a sample."

    raw = await document.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return "", "", f"That file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024}MB."
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # ⚠️ A PDF or DOCX lands here. Say so plainly rather than extracting
        # gibberish from binary and returning a confident, meaningless grade.
        return "", "", "That file is not UTF-8 text. Upload a .md or .txt document."
    if not text.strip():
        return "", "", "That file is empty."
    return text, f"upload:{document.filename}", None


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------


@router.get("/pitches", response_class=HTMLResponse)
def pitch_list(request: Request) -> HTMLResponse:
    auth.require_session(request)
    try:
        rows = db.list_pitches(request.app.state.intake.pool)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ could not list pitches: %s", exc)
        return page(request, "pitches.html", rows=[], db_error=str(exc))
    return page(request, "pitches.html", rows=rows, db_error=None)


@router.get("/pitches/{pitch_id}", response_class=HTMLResponse)
def pitch_detail(request: Request, pitch_id: str) -> HTMLResponse:
    auth.require_session(request)

    parsed = db.parse_pitch_id(pitch_id)
    if parsed is None:
        return page(request, "not_found.html", pitch_id=pitch_id, status_code=404)

    try:
        row = db.get_pitch(request.app.state.intake.pool, parsed)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️ could not read pitch %s: %s", pitch_id, exc)
        return page(request, "pitches.html", rows=[], db_error=str(exc))

    if row is None:
        # ⚠️ Not necessarily missing — B writes the row, so a pitch published
        # seconds ago may not exist yet. Say "not scored yet", not "not found".
        return page(request, "pending.html", pitch_id=pitch_id, age=None)

    if db.is_pending(row):
        age = db.age_seconds(row, datetime.now(timezone.utc))
        return page(request, "pending.html", pitch_id=pitch_id, age=age, row=row)

    return page(
        request,
        "pitch.html",
        row=row,
        report_html=render_markdown(row.get("report_md")),
    )
