"""Boxleiter becomes the primary units estimate.

Revision ID: 0003
Revises: 0002
Created: 2026-07-20

Why:
    0002 stored SteamSpy owner bands as the primary estimate and Boxleiter as a
    cross-check. Loading a 500-row sample showed that to be backwards.

    The lowest owner band is '0 - 20000', so its LOWER BOUND IS ZERO — every
    title in it reads as zero units sold. That included Shadow of the Tomb
    Raider with 17,861 reviews. 74% of the sample landed in that band. Taking
    the lower bound was meant to be conservative; it is simply wrong.

    The two methods also disagreed by 2x on hit count (132 vs 63 of 500) — and
    hit rate is THE core signal in docs/SCORING.md, so that gap is not
    tolerable.

    Boxleiter (review_count x multiplier) wins because niche_hit_rate is a
    THRESHOLD comparison within a comp set:

      * it is monotonic in review count and degrades smoothly, so no title
        falls off a cliff for sitting near a band boundary;
      * its error is a systematic multiplier, shifting every title the same
        direction — and since hit rate is relative within a niche, a consistent
        multiplicative bias largely cancels. A discontinuous one does not.

    The owner band is kept as a cross-check: it depends on SteamSpy's sampling
    rather than on review-leaving behaviour, so sharp disagreement between the
    two is a signal worth disclosing in a report rather than averaging away.

    ⚠️ Both remain ESTIMATES, and neither is revenue. See docs/SCORING.md.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # estimated_units is what scoring reads; it now holds the Boxleiter figure.
    # The old column is renamed to say what it actually contains rather than
    # being reused under a name that would now be a lie.
    op.execute(
        "ALTER TABLE steam_titles RENAME COLUMN estimated_units_boxleiter "
        "TO estimated_units_owner_band"
    )
    # The 500-row sample loaded under the old semantics. Clearing rather than
    # migrating in place: the ETL is cheap to re-run and reproducible, and a
    # half-converted corpus is worse than an empty one.
    op.execute("TRUNCATE steam_titles")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE steam_titles RENAME COLUMN estimated_units_owner_band "
        "TO estimated_units_boxleiter"
    )
    op.execute("TRUNCATE steam_titles")
