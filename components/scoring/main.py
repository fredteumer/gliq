"""Component B — gliq-scoring.

Pulls `ScoringRequested` from Pub/Sub, scores the pitch against the Steam comps
corpus in Cloud SQL, persists the result, and publishes `ScoringCompleted` for
Component C.

No inbound endpoint. Uses a PULL subscription, so this process needs no public
IP, no load balancer, and no open port — which is what lets the VM sit on the
tailnet with no ingress at all.

Runs as the systemd unit `gliq-scoring`. See docs/DEPLOYMENT.md.

## Layering

    main.py    <- this file: transport, persistence, lifecycle
    corpus.py  <- the only code that talks to Postgres
    rules.py   <- pure functions; the implementation of docs/SCORING.md

Scoring is deliberately reachable without any of the transport above it, so it
can be tested and calibrated offline against the same code that runs here.

⚠️ Memorystore is NOT wired in yet. It is provisioned for later; nothing here
caches, and comp lookups hit Cloud SQL every time.
"""

from __future__ import annotations

import logging
import signal
import sys
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime, timezone

import psycopg
from google.cloud import pubsub_v1
from pydantic import ValidationError

from components.scoring import corpus, rules
from shared.config import Config
from shared.schemas import ScoringCompleted, ScoringRequested

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",  # systemd already stamps the time
)
log = logging.getLogger("gliq-scoring")

# ⚠️ `document` is persisted but NEVER read by scoring. B is the write path for
# the raw pitch text (Component A stays publish-only), and Component C's analyst
# reads it back from here. The scoring result is a pure function of the profile
# and the corpus — the document does not enter it. ➡️ migration 0005.
UPSERT_SQL = """
    INSERT INTO pitches (pitch_id, status, profile, document, fitment, title, grade, scored_at)
    VALUES (%(pitch_id)s, 'scored', %(profile)s, %(document)s, %(fitment)s,
            %(title)s, %(grade)s, now())
    ON CONFLICT (pitch_id) DO UPDATE SET
        status = 'scored',
        profile = EXCLUDED.profile,
        document = EXCLUDED.document,
        fitment = EXCLUDED.fitment,
        title = EXCLUDED.title,
        grade = EXCLUDED.grade,
        scored_at = now(),
        error = NULL
"""

FAILURE_SQL = """
    INSERT INTO pitches (pitch_id, status, profile, document, error)
    VALUES (%(pitch_id)s, 'failed', %(profile)s, %(document)s, %(error)s)
    ON CONFLICT (pitch_id) DO UPDATE SET
        status = 'failed', document = EXCLUDED.document, error = EXCLUDED.error
"""


class Scorer:
    """Holds the long-lived connections a message handler needs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.conn = psycopg.connect(config.dsn, autocommit=True)
        self.publisher = pubsub_v1.PublisherClient()
        self.topic = self.publisher.topic_path(config.gcp_project, config.topic_scoring_completed)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass

    def handle(self, message: pubsub_v1.subscriber.message.Message) -> None:
        """Score one request.

        ⚠️ ack/nack policy, which decides what gets retried:
          * malformed payload   -> ACK. Redelivery fails identically forever and
            would block the subscription; the dead-letter topic exists for this.
          * scoring or DB error -> NACK. Probably transient (dropped connection,
            restart), so let Pub/Sub redeliver.
        """
        try:
            request = ScoringRequested.model_validate_json(message.data)
        except ValidationError as exc:
            log.error("❌ undecodable ScoringRequested, acking to dead-letter: %s", exc)
            message.ack()
            return

        pitch_id = str(request.pitch_id)
        try:
            log.info("🔍 scoring %s (%s)", pitch_id, request.profile.title or "untitled")

            candidates = corpus.fetch_candidates(self.conn, request.profile)
            result = rules.score_pitch(request.profile, candidates, date.today())

            self.conn.execute(
                UPSERT_SQL,
                {
                    "pitch_id": pitch_id,
                    "profile": request.profile.model_dump_json(),
                    "document": request.document,
                    "fitment": result.model_dump_json(),
                    "title": request.profile.title,
                    "grade": result.grade,
                },
            )

            completed = ScoringCompleted(
                pitch_id=request.pitch_id,
                profile=request.profile,
                result=result,
                completed_at=datetime.now(timezone.utc),
            )
            future = self.publisher.publish(self.topic, completed.model_dump_json().encode())
            future.result(timeout=30)

            if result.insufficient_information:
                log.warning("⚠️ %s — F (insufficient information)", pitch_id)
            else:
                log.info(
                    "✅ %s — %s (%.1f) from %d comps%s",
                    pitch_id,
                    result.grade,
                    result.score,
                    result.comps_considered,
                    f", capped from {result.uncapped_score:.1f}"
                    if result.score < result.uncapped_score
                    else "",
                )
            message.ack()

        except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
            log.exception("❌ scoring %s failed: %s", pitch_id, exc)
            try:
                self.conn.execute(
                    FAILURE_SQL,
                    {
                        "pitch_id": pitch_id,
                        "profile": request.profile.model_dump_json(),
                        "document": request.document,
                        "error": str(exc)[:2000],
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("❌ could not even record the failure for %s", pitch_id)
            message.nack()


def main() -> int:
    config = Config.from_env()
    log.info("🚀 Component B starting")
    log.info("   project=%s subscription=%s", config.gcp_project, config.sub_scoring_requested)

    try:
        scorer = Scorer(config)
    except Exception as exc:  # noqa: BLE001
        # Fail loudly and exit: systemd restarts, and StartLimitBurst stops the
        # flapping if the cause is permanent (missing DB, bad credentials).
        log.error("❌ startup failed: %s", exc)
        return 1

    subscriber = pubsub_v1.SubscriberClient()
    path = subscriber.subscription_path(config.gcp_project, config.sub_scoring_requested)

    # One message at a time: scoring pulls up to CANDIDATE_LIMIT rows into
    # memory and the VM is an e2-small with 2GB. Throughput is not the
    # constraint here; staying alive is.
    flow = pubsub_v1.types.FlowControl(max_messages=1)
    streaming = subscriber.subscribe(path, callback=scorer.handle, flow_control=flow)
    log.info("✅ listening on %s", path)

    def shutdown(signum, _frame):
        log.info("🛑 signal %s — draining", signum)
        streaming.cancel()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        streaming.result()
    except FuturesTimeout:
        streaming.cancel()
    except KeyboardInterrupt:
        streaming.cancel()
    finally:
        scorer.close()
        subscriber.close()
        log.info("👋 stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
