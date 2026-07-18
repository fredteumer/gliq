# Architecture

> 🚧 Expanded during implementation. See the [README](../README.md) for the current overview and diagram.

## Contents

1. **System overview** — the three components and the business process they execute
2. **Component responsibilities** — inputs, outputs, failure modes, why each is a separate process
3. **Cloud service selection** — why Pub/Sub, Cloud SQL, and Memorystore, and what each earns its place doing
4. **Message contracts** — `ScoringRequested`, `ScoringCompleted`, and the schemas in [`shared/schemas.py`](../shared/schemas.py)
5. **Scoring model** — sub-scores, weights, grade banding, and the estimation assumptions
6. **Networking & security** — public surface, Tailscale admin plane, service accounts, least privilege
7. **Assignment constraints** — how the design satisfies them without containers or functions

## Constraint note

This system is deliberately built **without containers, Kubernetes, or serverless functions**, which is unusual for a modern distributed application. That is an explicit requirement of the assignment brief, not an oversight. Processes are deployed as systemd units directly on VMs, and every stateful concern is delegated to a managed cloud service rather than self-hosted.
