"""Initial schema — comps corpus and pitch pipeline record.

Revision ID: 0001
Revises:
Created: 2026-07-20

Why:
    Two tables, both deliberately wide.

    The textbook design normalises genres and tags into join tables. That would
    be actively worse here: the corpus is written once by the ETL and never
    updated, and Component B's only question is "which titles match these
    tags?" — which a GIN index on a text[] answers in a single indexed scan.
    Normalising buys a multi-way join and pays for nothing.

    `pitches` holds one row per pitch for its whole journey: A writes `profile`,
    B writes `fitment`, C writes `recommendation`. That makes the audit trail
    free (a row IS the complete history), lets pydantic models serialise
    straight in and out with no ORM, and makes a stalled pitch visible in one
    query — still 'requested' means B never consumed it.

    ⚠️ The trade: no column-level typing or FK constraints on the JSONB
    payloads. pydantic is the only thing enforcing their shape. Acceptable
    because every component boundary already validates; it would not be on a
    multi-team system.

    Raw SQL rather than op.create_table(): the project uses psycopg directly
    with no ORM, and the array/GIN specifics read more clearly as the DDL they
    actually are.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------------
    # The comps corpus — one row per released Steam title
    # ---------------------------------------------------------------------
    # Loaded by the data/ ETL from the Kaggle dataset plus SteamSpy
    # enrichment. Not committed: the dataset carries its own licence terms,
    # separate from this project's AGPLv3.
    op.execute(
        """
        CREATE TABLE steam_titles (
            app_id          INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            release_date    DATE,
            price_usd       NUMERIC(10, 2),

            genres          TEXT[] NOT NULL DEFAULT '{}',
            tags            TEXT[] NOT NULL DEFAULT '{}',
            platforms       TEXT[] NOT NULL DEFAULT '{}',

            review_count    INTEGER,
            positive_ratio  NUMERIC(4, 3) CHECK (positive_ratio BETWEEN 0 AND 1),

            -- ⚠️ ESTIMATES, never audited sales — Steam does not publish unit
            -- sales. estimation_method must be carried into every report as a
            -- disclosed assumption (Boxleiter multiplier, SteamSpy owner bands).
            estimated_units    BIGINT,
            estimation_method  TEXT,

            ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Array containment ("titles whose tags overlap these") is the whole
    # workload. GIN answers && and @> without a sequential scan.
    op.execute("CREATE INDEX idx_steam_titles_tags   ON steam_titles USING GIN (tags)")
    op.execute("CREATE INDEX idx_steam_titles_genres ON steam_titles USING GIN (genres)")

    # Supporting the ranking half of scoring, once comps are selected.
    op.execute(
        "CREATE INDEX idx_steam_titles_reviews ON steam_titles (review_count DESC NULLS LAST)"
    )
    op.execute("CREATE INDEX idx_steam_titles_price ON steam_titles (price_usd)")

    # ---------------------------------------------------------------------
    # The pipeline record — one row per pitch, for its whole journey
    # ---------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE pitches (
            pitch_id        UUID PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'requested'
                            CHECK (status IN ('requested', 'scored', 'reported', 'failed')),

            profile         JSONB NOT NULL,   -- shared.schemas.PitchProfile
            fitment         JSONB,            -- shared.schemas.FitmentResult
            recommendation  JSONB,            -- shared.schemas.Recommendation

            -- Denormalised out of the JSONB purely so listing queries and the
            -- graded evidence screenshots do not need an extraction each time.
            title           TEXT,
            grade           TEXT,

            error           TEXT,             -- populated when status = 'failed'

            submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            scored_at       TIMESTAMPTZ,
            reported_at     TIMESTAMPTZ
        )
        """
    )

    op.execute("CREATE INDEX idx_pitches_status ON pitches (status)")
    op.execute("CREATE INDEX idx_pitches_submitted ON pitches (submitted_at DESC)")


def downgrade() -> None:
    # Indexes go with their tables.
    op.execute("DROP TABLE IF EXISTS pitches")
    op.execute("DROP TABLE IF EXISTS steam_titles")
