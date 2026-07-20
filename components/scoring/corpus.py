"""Candidate lookup against the Steam comps corpus.

The only part of Component B that talks to Postgres. Kept apart from `rules.py`
so the scoring logic stays a pure function of its inputs — testable, and
calibratable offline against the same code that runs in production.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from shared.schemas import PitchProfile

log = logging.getLogger(__name__)

#: Hard ceiling on rows pulled back for scoring.
#:
#: ⚠️ A broad pitch can overlap tens of thousands of titles — a tag like "Indie"
#: alone matches most of the corpus. Scoring happens in Python, so an unbounded
#: fetch would pull the whole table over the wire for every message. Ordering by
#: review_count first means the cap keeps the titles most likely to be
#: comparable rather than an arbitrary slice.
#:
#: The bias this introduces is disclosed: a pitch whose true comps are all
#: obscure will see the popular end of its niche. That is the right failure
#: direction for an acquisitions read — the alternative is scoring against
#: titles nobody can verify — but it IS a bias, not a neutral truncation.
CANDIDATE_LIMIT = 20_000

CANDIDATE_SQL = """
    SELECT app_id, name, genres, tags, platforms, release_date, price_usd,
           review_count, positive_ratio, estimated_units, estimated_units_owner_band,
           estimation_method, coalesce(tag_votes, '{}'::jsonb) AS tag_votes
    FROM steam_titles
    WHERE tags && %(tags)s OR genres && %(genres)s
    ORDER BY review_count DESC NULLS LAST
    LIMIT %(limit)s
"""


def fetch_candidates(
    conn: psycopg.Connection, profile: PitchProfile, limit: int = CANDIDATE_LIMIT
) -> list[dict[str, Any]]:
    """Titles sharing at least one tag or genre with the pitch.

    A deliberately loose prefilter — it exists to let the GIN indexes on
    `tags` and `genres` do the cheap elimination, not to pick comparables.
    Selection is `rules.select_comps`, which applies the real similarity.

    Both arrays are searched with `&&` (overlap) rather than `@>` (contains):
    a pitch shares SOME tags with its comps, never all of them.
    """
    tags = list(profile.tags or [])
    genres = [g for g in ([profile.primary_genre] if profile.primary_genre else []) + list(profile.sub_genres) if g]

    if not tags and not genres:
        # rules.has_comp_basis() reports this as insufficient information; there
        # is nothing to query on, and `&& '{}'` would match every row.
        return []

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(CANDIDATE_SQL, {"tags": tags, "genres": genres, "limit": limit})
        rows = cur.fetchall()

    for row in rows:
        # psycopg returns Decimal for NUMERIC; the rules do float arithmetic.
        if row["price_usd"] is not None:
            row["price_usd"] = float(row["price_usd"])
        if row["positive_ratio"] is not None:
            row["positive_ratio"] = float(row["positive_ratio"])
        row["tag_votes"] = {k: int(v) for k, v in (row["tag_votes"] or {}).items()}

    if len(rows) >= limit:
        log.warning(
            "⚠️ candidate prefilter hit the %s-row cap for '%s' — comps are biased "
            "toward the popular end of the niche",
            limit,
            profile.title or "untitled",
        )
    return rows
