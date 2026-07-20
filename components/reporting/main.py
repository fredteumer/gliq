"""Component C — gliq-report.

Pulls `ScoringCompleted` from Pub/Sub, turns the fitment result into an
investment decision, renders the evidence report, and persists both.

No inbound endpoint. Uses a PULL subscription, so this process needs no public
IP, no load balancer, and no open port — the VM sits on the tailnet with no
ingress at all.

Runs as the systemd unit `gliq-report`. See docs/DEPLOYMENT.md.

## Layering

    main.py       <- this file: transport, persistence, lifecycle
    render.py     <- pure; Markdown from the three models
    recommend.py  <- pure; tier, rationale, de-risk actions
    templates/    <- report.md.j2

Mirrors Component B's split for the same reason: the decision and the document
are both reachable and testable without Pub/Sub or Postgres.

## This is the terminal stage

C publishes nothing. There is no `notify` step beyond this: writing
`status='reported'` is what makes a pitch visible as complete to anything
reading the row, and the journal line is the operator-facing signal. Email,
webhooks and a notification topic do not exist anywhere in this project, so a
real one would be net-new infrastructure rather than a call to make here.

⚠️ The rendered report is stored in Postgres (`pitches.report_md`), not written
to the artifacts bucket. ➡️ migration 0004.
"""

from __future__ import annotations

import logging
import signal
import sys
from concurrent.futures import TimeoutError as FuturesTimeout

import psycopg
from google.cloud import pubsub_v1
from pydantic import ValidationError

from components.reporting.advisor import get_advisor
from components.reporting.recommend import recommend
from components.reporting.render import render_report
from shared.config import Config
from shared.schemas import ScoringCompleted

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",  # systemd already stamps the time
)
log = logging.getLogger("gliq-report")

#: ⚠️ An UPDATE, deliberately NOT the upsert Component B uses.
#:
#: B's write is the first thing to touch a row, so an upsert is right for it. By
#: the time C runs, the row already holds `profile` and `fitment`. An upsert here
#: would have to restate those columns to avoid nulling them, and would silently
#: resurrect a deleted pitch as a half-row. Updating only what C owns means C
#: cannot corrupt what B wrote, whatever order things arrive in.
REPORT_SQL = """
    UPDATE pitches
       SET status = 'reported',
           recommendation = %(recommendation)s,
           advisory = %(advisory)s,
           report_md = %(report_md)s,
           reported_at = now(),
           error = NULL
     WHERE pitch_id = %(pitch_id)s
"""

#: The raw pitch document, which Component B persisted. Read back so the LLM
#: analyst can weigh the prose the tag extractor never saw. ➡️ migration 0005.
DOCUMENT_SQL = "SELECT document FROM pitches WHERE pitch_id = %(pitch_id)s"

#: ⚠️ Also an UPDATE, touching ONLY status and error.
#:
#: B's FAILURE_SQL is an upsert that writes `profile` and omits `fitment`;
#: reusing it here would erase a perfectly good score because the REPORT failed
#: to render. A scoring result is not the report stage's to destroy.
FAILURE_SQL = """
    UPDATE pitches
       SET status = 'failed',
           error = %(error)s
     WHERE pitch_id = %(pitch_id)s
"""


class Reporter:
    """Holds the long-lived connection a message handler needs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.conn = psycopg.connect(config.dsn, autocommit=True)
        # Built once at startup. An unknown provider is fatal here (a deploy
        # error worth failing on); a missing KEY is not — the advisor degrades
        # to None at call time, so C still starts and still reports. ➡️ advisor.py
        self.advisor = get_advisor(config)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass

    def _document_for(self, pitch_id: str) -> str | None:
        """The raw pitch document B persisted, or None if it was never sent.

        B commits the row (with the document) before it publishes the completion
        event, so by the time C runs the document is already there — unless the
        pitch predates the feature, in which case it is simply NULL.
        """
        row = self.conn.execute(DOCUMENT_SQL, {"pitch_id": pitch_id}).fetchone()
        return row[0] if row else None

    def handle(self, message: pubsub_v1.subscriber.message.Message) -> None:
        """Report on one scored pitch.

        ⚠️ ack/nack policy, matching Component B:
          * malformed payload  -> ACK. Redelivery fails identically forever and
            would block the subscription; the dead-letter topic exists for this.
          * render or DB error -> NACK. Probably transient (dropped connection,
            restart), so let Pub/Sub redeliver.
          * row not found      -> NACK. B writes the row before it publishes, so
            a miss means something is genuinely wrong rather than merely slow.
            Dead-lettering after 5 attempts is louder than acking into silence.
        """
        try:
            completed = ScoringCompleted.model_validate_json(message.data)
        except ValidationError as exc:
            log.error("❌ undecodable ScoringCompleted, acking to dead-letter: %s", exc)
            message.ack()
            return

        pitch_id = str(completed.pitch_id)
        try:
            log.info(
                "📥 consumed %s — %s (%s %.1f)",
                pitch_id,
                completed.profile.title or "untitled",
                completed.result.grade,
                completed.result.score,
            )

            recommendation = recommend(completed.profile, completed.result)

            # ⚠️ The analyst is an independent SECOND OPINION, never the grade.
            # It reads the raw document B persisted; if there is no document or
            # the model is unavailable, `advisory` is None and the deterministic
            # report is rendered regardless — the analyst is decision support,
            # not a dependency. ➡️ advisor.py
            document = self._document_for(pitch_id)
            advisory = self.advisor.opine(document, completed.profile, completed.result) if document else None

            report_md = render_report(
                pitch_id, completed.profile, completed.result, recommendation, advisory
            )

            cursor = self.conn.execute(
                REPORT_SQL,
                {
                    "pitch_id": pitch_id,
                    "recommendation": recommendation.model_dump_json(),
                    "advisory": advisory.model_dump_json() if advisory else None,
                    "report_md": report_md,
                },
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    f"no pitches row for {pitch_id} — Component B should have written "
                    "one before publishing"
                )

            if advisory is not None:
                analyst = f"analyst {advisory.model} → {advisory.grade}/{advisory.stance.value}"
            elif document is None:
                analyst = "analyst skipped (no document)"
            else:
                analyst = "analyst unavailable"
            log.info(
                "📝 rendered %s — %s, %d de-risk action(s), %d chars, %s",
                pitch_id,
                recommendation.tier.value,
                len(recommendation.de_risk_actions),
                len(report_md),
                analyst,
            )
            if completed.result.insufficient_information:
                log.warning(
                    "⚠️ %s reported as INSUFFICIENT INFORMATION, not as a low grade", pitch_id
                )
            message.ack()

        except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
            log.exception("❌ reporting %s failed: %s", pitch_id, exc)
            try:
                self.conn.execute(FAILURE_SQL, {"pitch_id": pitch_id, "error": str(exc)[:2000]})
            except Exception:  # noqa: BLE001
                log.exception("❌ could not even record the failure for %s", pitch_id)
            message.nack()


def main() -> int:
    config = Config.from_env()
    log.info("🚀 Component C starting")
    log.info("   project=%s subscription=%s", config.gcp_project, config.sub_scoring_completed)

    try:
        reporter = Reporter(config)
    except Exception as exc:  # noqa: BLE001
        # Fail loudly and exit: systemd restarts, and StartLimitBurst stops the
        # flapping if the cause is permanent (missing DB, bad credentials).
        log.error("❌ startup failed: %s", exc)
        return 1

    subscriber = pubsub_v1.SubscriberClient()
    path = subscriber.subscription_path(config.gcp_project, config.sub_scoring_completed)

    # One message at a time. NOT for B's reason — rendering a string is cheap,
    # where scoring pulls CANDIDATE_LIMIT rows into memory — but because the
    # report is the artefact a human reads, and a serial stream keeps the
    # journal a legible record of what happened in what order.
    flow = pubsub_v1.types.FlowControl(max_messages=1)
    streaming = subscriber.subscribe(path, callback=reporter.handle, flow_control=flow)
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
        reporter.close()
        subscriber.close()
        log.info("👋 stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
