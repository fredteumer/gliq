"""Rendered reports live in Postgres, not Cloud Storage

Revision ID: 0004
Revises: 0003
Created: 2026-07-20

Why:
    Component C renders an evidence report. The original design wrote it to the
    artifacts bucket as an object and had Component A serve it back — the IAM in
    infra/core/storage.ts still reflects that, granting C objectAdmin and A
    objectViewer.

    Storing the rendered document in Postgres instead, for three reasons:

      * A thin review UI is coming. Reading a column is what that UI wants; a
        bucket object means it either proxies GCS or the report becomes a second
        source of truth living somewhere the pitch row does not.
      * Component A is deliberately DB-free — it publishes and does not persist,
        which keeps psycopg and a database route off the one publicly-reachable
        VM. Serving reports from a bucket would have been fine, but so is
        deferring A's read path entirely until the UI lands and can say what it
        actually needs.
      * The `reporting` extra in pyproject.toml never installed
        google-cloud-storage, so the bucket path was not installable on C's VM
        as configured. That is a symptom of the design never having been
        exercised, not a coincidence worth working around.

    ⚠️ The report is stored as MARKDOWN, not HTML. Content and presentation stay
    separate: the UI renders Markdown to HTML at display time, so a styling
    change does not require regenerating stored artifacts.

    `report_md` is a separate column rather than a key inside `recommendation`
    JSONB because the two have different lifetimes. `recommendation` is the
    structured decision — tier, rationale, de-risk actions — and survives a
    template change; `report_md` is prose derived from it and can always be
    re-rendered from the structured data. Merging them would mean a template
    edit dirties the decision record.

    ⛔ Consequence: the artifacts bucket goes unused. Cloud Storage is marked
    *optional* in AGENTS.md and counts toward none of the graded infrastructure
    categories (messaging, queuing, caching, databases), so nothing is lost
    against the brief. The bucket and its IAM stay provisioned rather than being
    torn out, because Part 2 accepts image uploads and will want somewhere to
    put them.

    Also removed in this change, though it needs no DDL: `Recommendation
    .report_uri` in shared/schemas.py. It pointed at the bucket object, is
    referenced nowhere in the codebase, and under this design would be
    permanently None. A UI derives the report's location from `pitch_id`.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nullable with no default: a row is created by B at scoring time and only
    # gains a report once C has consumed the completion event. NULL here means
    # "not reported yet", which is exactly what status='scored' already says.
    op.execute("ALTER TABLE pitches ADD COLUMN report_md TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE pitches DROP COLUMN report_md")
