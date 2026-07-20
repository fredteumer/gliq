"""Persist the raw document and the LLM analyst's opinion

Revision ID: 0005
Revises: 0004
Created: 2026-07-20

Why:
    Two columns for Component C's LLM analyst — a subjective second opinion
    shown alongside the deterministic recommendation.

    `document TEXT` — the raw submitted pitch, as markdown.

      The deterministic pipeline never needed the prose: Component A extracts a
      PitchProfile and the rest of the system scores that. The LLM analyst does
      need it — its whole value is reading the argument the tag-based extractor
      cannot (negation, nuance, the team, the ask). Rather than re-forward the
      document on the B->C message (which B would carry without using), Component
      B persists it here. That keeps scoring deterministic — B writes the column
      but never reads it — and makes the raw input REFERENCEABLE: the analyst can
      be re-run against a stored pitch, the UI can show the original, and a
      future aggregate score has the input on hand.

      ⚠️ Component B writes it, not Component A. A stays out of the write path
      (it publishes and does not persist), so the document lands when B first
      processes the pitch, consistent with the existing "a pitch lost between A
      and B leaves no trace" behaviour.

    `advisory JSONB` — shared.schemas.AdvisoryOpinion.

      A separate column from `recommendation`, deliberately. The two are
      different KINDS of thing from different sources: `recommendation` is the
      deterministic decision, `advisory` is an LLM's subjective read. Keeping
      them apart is what lets a human's read be pooled with both later without
      one overwriting another — the aggregate-scoring step this is groundwork
      for. Merging the LLM opinion into `recommendation` would also blur the one
      invariant that matters: the grade is deterministic, the opinion is not.

    ⚠️ The analyst does NOT feed the grade. `fitment` and `recommendation` are
    unchanged; `advisory` sits beside them. ➡️ components/reporting/advisor.py.

    Both columns are nullable: a pitch scored before this change, or one whose
    analyst is disabled (the `fixture`/degraded paths), simply has NULL here.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nullable, no default: document is populated by Component B when it scores
    # the pitch; advisory by Component C when it reports. NULL means "not yet"
    # for either, which is exactly what the pipeline status already conveys.
    op.execute("ALTER TABLE pitches ADD COLUMN document TEXT")
    op.execute("ALTER TABLE pitches ADD COLUMN advisory JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE pitches DROP COLUMN advisory")
    op.execute("ALTER TABLE pitches DROP COLUMN document")
