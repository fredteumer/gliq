"""Component B — gliq-scoring.

Pulls ScoringRequested messages, matches the PitchProfile against the Steam
comparables corpus in Cloud SQL (fronted by the Memorystore cache), computes a
deterministic FitmentResult, and publishes ScoringCompleted for Component C.

No inbound endpoint — this is a Pub/Sub *pull* subscriber, so the VM needs no
public IP. Scoring is rule-based; no LLM is involved, so results are
reproducible. Runs as the systemd unit `gliq-scoring`.
"""

from __future__ import annotations

# TODO: Pub/Sub pull subscriber loop on PUBSUB_SUB_SCORING_REQUESTED
# TODO: comp-set lookup by genre/tag cluster, Redis-cached, Cloud SQL-backed
# TODO: sub-scores (saturation, hit rate, sales potential, price alignment)
# TODO: weighted score -> letter grade, assumptions recorded on the result
# TODO: publish ScoringCompleted


def main() -> None:
    raise NotImplementedError("Component B not yet implemented")


if __name__ == "__main__":
    main()
