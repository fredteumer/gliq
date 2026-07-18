"""Component A — gliq-intake.

Public HTTPS entry point. Accepts a game design document, uses an LLM to
extract a structured PitchProfile, and publishes a ScoringRequested message
for Component B.

This is the only publicly reachable component. Runs behind nginx + certbot at
greenlightiq.fredt.io, as the systemd unit `gliq-intake`.
"""

from __future__ import annotations

# TODO: FastAPI app with POST /pitches accepting a design doc
# TODO: LLM extraction via a provider adapter (anthropic | gemini | fixture)
# TODO: publish ScoringRequested to PUBSUB_TOPIC_SCORING_REQUESTED
# TODO: GET /healthz for deployment verification


def main() -> None:
    raise NotImplementedError("Component A not yet implemented")


if __name__ == "__main__":
    main()
