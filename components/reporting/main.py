"""Component C — gliq-report.

Pulls ScoringCompleted messages, maps the fitment score to an investment tier,
renders the evidence report, persists the decision to Cloud SQL, and notifies.

No inbound endpoint — Pub/Sub pull subscriber. Runs as the systemd unit
`gliq-report`.
"""

from __future__ import annotations

# TODO: Pub/Sub pull subscriber loop on PUBSUB_SUB_SCORING_COMPLETED
# TODO: score -> InvestmentTier mapping with rationale and de-risk actions
# TODO: Jinja2 report render (Markdown/HTML) incl. comps table + assumptions
# TODO: persist Recommendation to Cloud SQL; write artifact; notify


def main() -> None:
    raise NotImplementedError("Component C not yet implemented")


if __name__ == "__main__":
    main()
