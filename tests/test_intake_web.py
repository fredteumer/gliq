"""Behaviours the review UI commits to.

Most of these are about the gate rather than the pages. Component A is the only
publicly reachable process in the system; it queues work, and in Part 2 it will
spend LLM credits per submission. So the tests that matter are the ones that
fail loudly if a route stops requiring a session, if a misconfigured instance
starts accepting logins, or if form input reaches the filesystem.

⚠️ No database and no Pub/Sub. Both are stubbed, so the suite runs offline like
the rest of the project's tests.
"""

import importlib.util
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASSWORD = "test-password-1234"
USERNAME = "admin"

#: The hash generator is an OPS script that never ships to the VM, while
#: `auth.verify_password` runs in production. They are separate implementations
#: by necessity, so the suite loads the real generator and pins the two
#: together — a drift between them would present as "the correct password is
#: rejected", with nothing in the logs to explain it.
_spec = importlib.util.spec_from_file_location(
    "hash_password", os.path.join(REPO_ROOT, "infra", "scripts", "hash-password.py")
)
hash_password = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hash_password)

os.environ["ADMIN_PASSWORD_HASH"] = hash_password.hash_password(PASSWORD)
os.environ["ADMIN_USER"] = USERNAME
os.environ["SESSION_SECRET"] = "0" * 64
os.environ["LLM_PROVIDER"] = "deterministic"

from components.intake import auth, db, web  # noqa: E402

PITCH_ID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

REPORTED_ROW = {
    "pitch_id": PITCH_ID,
    "title": "Hollow Reef",
    "grade": "D",
    "status": "reported",
    "tier": "de_risk",
    "error": None,
    "profile": {},
    "fitment": {},
    "recommendation": {"tier": "de_risk"},
    "report_md": "# Hollow Reef\n\n| Signal | Score |\n| :--- | ---: |\n| Hit rate | 56.0 |\n",
    "submitted_at": NOW,
    "scored_at": NOW,
    "reported_at": NOW,
}

PENDING_ROW = {**REPORTED_ROW, "status": "scored", "report_md": None, "reported_at": None}


@contextmanager
def client(row=REPORTED_ROW, rows=None, db_raises=None):
    """A TestClient with Pub/Sub and Postgres stubbed.

    `base_url` is https because the session cookie is set `https_only=True` —
    over http the cookie is dropped and every login appears to silently fail,
    which is the single most confusing way this can break.
    """
    published: list[bytes] = []
    publisher = MagicMock()
    publisher.topic_path.return_value = "projects/p/topics/t"
    publisher.publish.side_effect = lambda topic, data: (
        published.append(data),
        MagicMock(result=lambda timeout=None: "msg"),
    )[1]

    def _fail(*_a, **_k):
        raise db_raises

    with (
        patch("google.cloud.pubsub_v1.PublisherClient", return_value=publisher),
        patch.object(db, "make_pool", return_value=MagicMock()),
        patch.object(db, "list_pitches", _fail if db_raises else (lambda *_a, **_k: rows or [])),
        patch.object(db, "status_counts", _fail if db_raises else (lambda *_a, **_k: {})),
        patch.object(db, "get_pitch", _fail if db_raises else (lambda *_a, **_k: row)),
    ):
        from fastapi.testclient import TestClient

        from components.intake.main import app

        with TestClient(app, base_url="https://testserver") as c:
            c.published = published
            yield c


def log_in(c) -> None:
    response = c.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


# --------------------------------------------------------------- credentials


def test_the_app_verifies_hashes_the_ops_script_generates():
    """Two implementations, one format. Drift = the right password stops working."""
    assert auth.verify_password(PASSWORD, hash_password.hash_password(PASSWORD))
    assert not auth.verify_password("wrong", hash_password.hash_password(PASSWORD))


def test_an_unconfigured_instance_is_locked_not_open():
    """🔒 The failure mode of a misconfiguration must be a locked door.

    An empty hash means nobody set a password — that has to reject every
    attempt, not accept any.
    """
    for password, stored in [
        ("anything", ""),
        ("", ""),
        ("anything", "garbage"),
        ("anything", "bcrypt$1$2$3$4$5"),
    ]:
        assert not auth.verify_password(password, stored), (password, stored)


def test_next_cannot_redirect_off_site():
    """⚠️ `//evil.com` matters as much as `https://evil.com`.

    A protocol-relative URL is a fully working off-site redirect and is the
    case that gets forgotten.
    """
    cases = [
        ("/pitches", "/pitches"),
        ("/pitches?x=1", "/pitches?x=1"),
        ("//evil.com", "/"),
        ("https://evil.com", "/"),
        ("javascript:alert(1)", "/"),
        (None, "/"),
        ("", "/"),
    ]
    for raw, expected in cases:
        assert auth.safe_next(raw) == expected, raw


# ---------------------------------------------------------------- the gate


def test_every_route_except_healthz_requires_a_session():
    """The gate is the point of this feature.

    ⚠️ `POST /pitches` is included deliberately. It is a public endpoint that
    queues work and will spend LLM credits in Part 2 — leaving the JSON API
    open would make the login form decorative.
    """
    with client() as c:
        for method, path in [
            ("get", "/"),
            ("get", "/pitches"),
            ("get", f"/pitches/{PITCH_ID}"),
            ("post", "/submit"),
            ("post", "/pitches"),
        ]:
            response = getattr(c, method)(path, follow_redirects=False)
            assert response.status_code == 303, (method, path)
            assert response.headers["location"].startswith("/login"), (method, path)

        assert c.get("/healthz").status_code == 200
        assert c.published == []


def test_a_rejected_login_does_not_say_which_half_was_wrong():
    """One message for every cause.

    Distinguishing "no such user" from "wrong password" from "no password
    configured" tells an attacker which half they got right, and tells a
    scanner that the instance is unconfigured.
    """
    with client() as c:
        for username, password in [(USERNAME, "wrong"), ("nobody", PASSWORD), ("", "")]:
            response = c.post(
                "/login", data={"username": username, "password": password}, follow_redirects=False
            )
            assert response.status_code == 401
            assert "Incorrect username or password" in response.text


def test_logging_in_grants_access_and_logging_out_revokes_it():
    with client(rows=[REPORTED_ROW]) as c:
        assert c.get("/", follow_redirects=False).status_code == 303
        log_in(c)
        assert c.get("/").status_code == 200
        assert c.get("/pitches").status_code == 200

        c.get("/logout")
        assert c.get("/", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ submit


def test_a_bundled_sample_is_published_for_scoring():
    with client() as c:
        log_in(c)
        response = c.post("/submit", data={"sample": "strong-pitch.md"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/pitches/")
        assert len(c.published) == 1

        from shared.schemas import ScoringRequested

        published = ScoringRequested.model_validate_json(c.published[0])
        assert published.profile.title == "Hollow Reef"
        assert published.profile.tags


def test_the_sample_picker_cannot_read_arbitrary_files():
    """⚠️ `sample` is form input. Treating it as a path is a directory traversal.

    `../.env` on the VM is the deployed secrets file, so this is the difference
    between a demo feature and a credential disclosure.
    """
    with client() as c:
        log_in(c)
        for attack in ["../.env", "../../etc/passwd", "/etc/passwd", "..%2f.env"]:
            response = c.post("/submit", data={"sample": attack}, follow_redirects=False)
            assert response.status_code == 400, attack
        assert c.published == []


def test_a_submission_with_neither_file_nor_sample_is_refused():
    with client() as c:
        log_in(c)
        assert c.post("/submit", data={}, follow_redirects=False).status_code == 400
        assert c.published == []


def test_a_non_utf8_upload_is_refused_rather_than_scored_as_gibberish():
    """A PDF or DOCX lands here.

    Extracting from decoded binary would produce a confident, meaningless
    grade — worse than a clear refusal.
    """
    with client() as c:
        log_in(c)
        response = c.post(
            "/submit",
            files={"document": ("pitch.pdf", b"%PDF-1.7\x00\x80\xff binary", "application/pdf")},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "not UTF-8" in response.text
        assert c.published == []


# ------------------------------------------------------------------ review


def test_a_finished_report_renders_as_html():
    with client(row=REPORTED_ROW) as c:
        log_in(c)
        response = c.get(f"/pitches/{PITCH_ID}")
        assert response.status_code == 200
        # Markdown became HTML rather than being shown as source.
        assert "<h1>Hollow Reef</h1>" in response.text
        assert "<table>" in response.text
        assert "| Signal |" not in response.text


def test_a_pitch_still_scoring_shows_progress_not_an_error():
    """⚠️ The normal path passes through here.

    A pitch sits in `requested`/`scored` for a few seconds while B and C work.
    Rendering that as a failure would make the working system look broken.
    """
    with client(row=PENDING_ROW) as c:
        log_in(c)
        response = c.get(f"/pitches/{PITCH_ID}")
        assert response.status_code == 200
        assert "Scoring in progress" in response.text


def test_a_pitch_that_does_not_exist_yet_is_pending_not_missing():
    """B writes the row, so a just-published pitch has no row for a moment."""
    with client(row=None) as c:
        log_in(c)
        assert "Scoring in progress" in c.get(f"/pitches/{PITCH_ID}").text


def test_a_malformed_pitch_id_is_a_404_not_a_500():
    """`pitch_id` is a UUID column — unvalidated input raises inside psycopg."""
    with client() as c:
        log_in(c)
        assert c.get("/pitches/not-a-uuid").status_code == 404


def test_the_database_being_down_does_not_stop_submissions():
    """⚠️ Degrade, do not fail.

    Scoring runs through Pub/Sub and never touches Postgres from Component A,
    so an outage must cost the review pages and nothing else.
    """
    with client(db_raises=RuntimeError("connection refused")) as c:
        log_in(c)
        dashboard = c.get("/")
        assert dashboard.status_code == 200
        assert "unreachable" in dashboard.text

        response = c.post("/submit", data={"sample": "weak-pitch.md"}, follow_redirects=False)
        assert response.status_code == 303
        assert len(c.published) == 1


# ------------------------------------------------------------------- markup


def test_report_rendering_does_not_emit_raw_html():
    """Second line of defence.

    Component C escapes untrusted fields when it writes the report; keeping
    raw-HTML extensions off means a stored `<script>` still could not execute.
    """
    rendered = web.render_markdown("# Title\n\n<script>alert(1)</script>\n")
    assert "<script>" not in rendered


def test_rendering_an_absent_report_is_empty_not_an_error():
    assert web.render_markdown(None) == ""
    assert web.render_markdown("") == ""


def test_pending_age_is_reported_from_submission_time():
    row = {"submitted_at": NOW - timedelta(seconds=42)}
    assert db.age_seconds(row, NOW) == pytest.approx(42.0)
    assert db.age_seconds({}, NOW) is None
