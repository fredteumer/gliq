"""Read-only queries for Component A's web UI.

The only code in Component A that touches Postgres, kept apart for the same
reason `components/scoring/corpus.py` is: everything else in A stays testable
without a database.

## ⚠️ SELECT only, deliberately

Component A was built DB-free — it publishes to Pub/Sub and Component B's
`INSERT ... ON CONFLICT` creates the row. The review UI forced a database
dependency, but only for READING. There is no INSERT or UPDATE path in this
module and there should never be one: A is the publicly-reachable component,
and a write path here would let a bug or an exploit in the web layer corrupt
the pipeline's record of what was scored. Submission still travels by message,
never by direct write.

## Why a pool, when B and C hold a single connection

B and C are single-threaded consumers processing one message at a time, so one
long-lived connection is exactly right for them. A is a web server: FastAPI runs
sync endpoints in a threadpool, so requests genuinely overlap, and a psycopg
connection is not safe to share across threads. A small pool is the correct
shape here rather than an inconsistency with the other two.

⚠️ A database outage must DEGRADE the UI, not kill intake. The pool opens
without blocking startup, and every query raises a plain exception the web layer
turns into a message. Submitting a pitch does not touch this module at all, so
scoring keeps working while the review pages are unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

#: Small on purpose. This serves a handful of operators, and every connection
#: is a real Cloud SQL backend on a db-f1-micro.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4

#: Ceiling on the listing. The UI is a review tool, not an export.
DEFAULT_LIMIT = 200

#: `tier` is lifted out of the recommendation JSONB so the listing does not
#: have to parse it per row. `report_md` is deliberately NOT selected here — it
#: is kilobytes per row and the list view never renders it.
LIST_SQL = """
    SELECT pitch_id,
           title,
           grade,
           status,
           recommendation->>'tier' AS tier,
           submitted_at,
           scored_at,
           reported_at
      FROM pitches
     ORDER BY submitted_at DESC
     LIMIT %(limit)s
"""

GET_SQL = """
    SELECT pitch_id,
           title,
           grade,
           status,
           error,
           profile,
           fitment,
           recommendation,
           report_md,
           submitted_at,
           scored_at,
           reported_at
      FROM pitches
     WHERE pitch_id = %(pitch_id)s
"""

COUNTS_SQL = """
    SELECT status, count(*) AS n
      FROM pitches
     GROUP BY status
"""


def make_pool(dsn: str) -> ConnectionPool:
    """Build the pool without blocking startup.

    ⚠️ `open=False` then a non-waiting open: if Cloud SQL is unreachable at boot
    the process must still come up and serve `/healthz` and submissions. A
    connection error should surface on the page that needs the data, not as a
    unit that will not start.
    """
    pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=False)
    pool.open(wait=False)
    return pool


def parse_pitch_id(value: str) -> UUID | None:
    """Validate a pitch id from a URL path.

    ⚠️ Reject before it reaches the database. `pitch_id` is a UUID column, so a
    malformed value raises a psycopg error mid-request — this turns a 500 into a
    404, and keeps arbitrary user input away from the driver.
    """
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def list_pitches(pool: ConnectionPool, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Every pitch, newest first."""
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(LIST_SQL, {"limit": limit})
        return cur.fetchall()


def get_pitch(pool: ConnectionPool, pitch_id: UUID) -> dict[str, Any] | None:
    """One pitch with its report, or None if there is no such row."""
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(GET_SQL, {"pitch_id": str(pitch_id)})
        return cur.fetchone()


def status_counts(pool: ConnectionPool) -> dict[str, int]:
    """Pitches per status, for the dashboard."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(COUNTS_SQL)
        return {status: n for status, n in cur.fetchall()}


def is_pending(row: dict[str, Any]) -> bool:
    """True while a pitch has been accepted but has no report yet.

    ⚠️ The UI must distinguish this from a failure. A pitch sits in `requested`
    or `scored` for a few seconds while B and C work, and showing "no report" as
    an error would make the normal path look broken.
    """
    return row.get("status") in {"requested", "scored"}


def age_seconds(row: dict[str, Any], now: datetime) -> float | None:
    """How long a pitch has been in flight, for the pending page."""
    submitted = row.get("submitted_at")
    if submitted is None:
        return None
    return (now - submitted).total_seconds()
