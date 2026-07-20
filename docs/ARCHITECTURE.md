# Architecture

> 🚧 Expanded during implementation. See the [README](../README.md) for the current overview and diagram.
>
> **Status:** sections 3–5 are effectively covered elsewhere — cloud service selection in the README and `infra/core/*.ts` module docstrings, message contracts in [`shared/schemas.py`](../shared/schemas.py), and the scoring model in [SCORING.md](./SCORING.md). Section 7 is written below. Sections 1, 2, and 6 still need writing here, and they are graded.

## Contents

1. **System overview** — the three components and the business process they execute
2. **Component responsibilities** — inputs, outputs, failure modes, why each is a separate process
3. **Cloud service selection** — why Pub/Sub and Cloud SQL, and what each earns its place doing
4. **Message contracts** — `ScoringRequested`, `ScoringCompleted`, and the schemas in [`shared/schemas.py`](../shared/schemas.py)
5. **Scoring model** — ✅ written up separately and in full: ➡️ [SCORING.md](./SCORING.md). Sub-scores, weights, grade banding, estimation assumptions, and the calibration methodology behind every constant.
6. **Networking & security** — public surface, Tailscale admin plane, service accounts, least privilege
7. **Assignment constraints** — ✅ written below

## Constraint note

This system is deliberately built **without containers, Kubernetes, or serverless functions**, which is unusual for a modern distributed application. That is an explicit requirement of the assignment brief, not an oversight. Processes are deployed as systemd units directly on VMs, and every stateful concern is delegated to a managed cloud service rather than self-hosted.

## 7. Assignment constraints

| Requirement | How this design satisfies it |
| :--- | :--- |
| No containers / Kubernetes / service mesh | Three systemd units on three Compute Engine VMs. No Docker anywhere, including in development. |
| No serverless functions | No Cloud Functions, no Cloud Run. Component A is a long-lived uvicorn process; B and C are long-lived pull subscribers. |
| Managed services only for infrastructure | Nothing stateful is self-hosted. Postgres is Cloud SQL; the message broker is Pub/Sub. No database, cache, or broker is installed on a VM or locally, including for tests — the test suite exercises pure functions and never needs one. |
| VMs and object storage as the only primitives | Compute Engine plus a Cloud Storage bucket. Nothing outside that list. |
| ≥ 3 distinct processes | `gliq-intake`, `gliq-scoring`, `gliq-report` — separate executables, separate VMs, communicating only through managed services. |
| ≥ 3 of 4 infrastructure categories | **Messaging, queuing, and databases.** See below. |

### Infrastructure categories: messaging, queuing, databases

The brief asks for at least three of *messaging, queuing, caching, databases*. This system implements **messaging**, **queuing**, and **databases**, and deliberately does **not** implement caching.

**Databases — Cloud SQL (PostgreSQL).** The Steam comparables corpus (82,952 titles), pitch profiles, fitment results, recommendations, and rendered reports. Every component boundary that needs durable state crosses it.

**Messaging and queuing — Cloud Pub/Sub, used in two architecturally distinct ways.** The two categories describe different communication patterns, and this system genuinely exhibits both:

*Queuing* — the `scoring-requested` topic is a work queue between A and B. Its configuration is the queue, not the topic:

| Property | Setting | Queue semantic it provides |
| :--- | :--- | :--- |
| `ackDeadlineSeconds` | 60 | Visibility timeout — work not acknowledged in time is redelivered |
| `retryPolicy` | 10s → 600s backoff | Failed work is retried with exponential backoff |
| `deadLetterPolicy` | 5 attempts → `gliq-dead-letter` | Poison-message isolation |
| `messageRetentionDuration` | 7 days | Durable backlog surviving consumer downtime |
| Pull subscription, `max_messages=1` | — | Competing-consumer work distribution; B pulls when it has capacity |

Component B acknowledges a message only after the pitch is scored and persisted, and explicitly negative-acknowledges on a transient failure so the work is redelivered. That is work-queue behaviour — at-least-once delivery of *tasks*, with retry and dead-lettering — not broadcast.

*Messaging* — the `scoring-completed` topic is event notification. B publishes a fact ("this pitch was scored") without knowing or caring who consumes it. C subscribes today; a notification service or analytics consumer could subscribe tomorrow with no change to B. That is publish/subscribe fan-out, and it is a different pattern from the queue above regardless of the two sharing a provider.

⚠️ **This is a deliberate position, so the counter-argument is stated plainly:** both patterns are served by one managed service. A reading of the requirement that counts *distinct services* rather than *distinct capabilities* would score this as two categories, not three. The alternative — Cloud Tasks for queuing — was evaluated and rejected on architectural grounds, not convenience: Cloud Tasks delivers over HTTP and would require Component B to expose a publicly routable inbound endpoint. That would break the property this system's network design rests on, that **only Component A is publicly reachable** (§6). Weakening the security posture to add a second queueing product would make the architecture worse in order to make the checklist tidier.

⛔ **Caching is not implemented, and this is a design decision rather than an omission.** Memorystore appears in early planning notes and in the private-IP range allocated in `infra/core/networking.ts`; no instance was ever provisioned. The workload does not justify one. Component B's candidate query is the only plausible cache target, and this system's throughput is a handful of pitches — a Redis instance would be introduced solely to be pointed at in a screenshot, would cost real budget against finite trial credits, and would add a failure mode to a pipeline that currently has none. Introducing infrastructure a system does not need, to satisfy a checkbox, is the kind of decision this document should record honestly rather than dress up.

💡 If throughput ever justified it, the insertion point is already isolated: `components/scoring/corpus.py` is the only code that touches Postgres, and `fetch_candidates` is a single function keyed on genre and tag cluster.
