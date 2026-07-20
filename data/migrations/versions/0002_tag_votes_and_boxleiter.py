"""Tag vote counts and a second units estimate.

Revision ID: 0002
Revises: 0001
Created: 2026-07-20

Why:
    Two things the corpus turned out to carry that 0001 had nowhere to put.

    `tag_votes` — the source data gives Steam tags WITH vote counts
    ({'Strategy': 120, 'Fantasy': 76}), not a flat list. A game tagged Strategy
    by 120 people is more meaningfully a strategy game than one tagged by 3,
    and plain Jaccard over `tags` treats those identically. Kept alongside the
    text[] rather than replacing it: the array stays GIN-indexed for fast
    containment filtering, and the JSONB weights the candidates that survive.

    `estimated_units_boxleiter` — a second, independent estimate. The primary
    figure comes from SteamSpy owner bands, which are coarse at the low end
    ('0 - 20000' spans the entire 10,000-unit success threshold). The Boxleiter
    method (review_count x multiplier) has different failure modes, so where
    the two disagree sharply that is itself worth disclosing in a report.
    Storing both now is cheap; backfilling later means re-running the ETL.

    ⚠️ Neither is a sales figure. See docs/SCORING.md — the system reports
    units moved, never revenue.
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE steam_titles ADD COLUMN tag_votes JSONB")
    op.execute("ALTER TABLE steam_titles ADD COLUMN estimated_units_boxleiter BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE steam_titles DROP COLUMN estimated_units_boxleiter")
    op.execute("ALTER TABLE steam_titles DROP COLUMN tag_votes")
