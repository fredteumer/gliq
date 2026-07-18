# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Cursor, Copilot, Codex, etc.) working in this repository. It is the **single source of truth**. `CLAUDE.md` and `CODEX.md` are minimal redirects to this file.

## Project Overview

**GreenlightIQ** (`gliq`) is a distributed, cloud-hosted decision-support system for a video game publisher's acquisitions team. A producer submits a game design document; the system extracts the pitch's defining attributes, benchmarks them against a market dataset of released Steam titles, and returns a letter grade, an investment/de-risk recommendation, and an evidence report of comparable titles.

It is built as three independent processes communicating over managed Google Cloud services.

This repository is also the deliverable for the **Johns Hopkins University AI Engineering Certificate — Cloud-Based System Design Individual Project**. The proposal was approved by instructors on 2026-07-18. The constraints below are graded requirements, not preferences.

## 🛑 HARD CONSTRAINTS — read before proposing any architecture

These come directly from the assignment brief and **override any default best practice you would otherwise suggest**:

- ❌ **NO CONTAINERS.** No Docker, no Docker Compose, no Kubernetes, no service mesh. This is explicitly prohibited. Do not suggest containerizing anything, even for local development convenience.
- ❌ **NO SERVERLESS FUNCTIONS.** No Cloud Functions, no Cloud Run, no Lambda.
- ✅ **Managed cloud services only** for infrastructure. Do not install or self-host a database, cache, or message broker — not on a VM, not locally, not for testing.
- ✅ **VMs and object storage are permitted** compute/storage primitives. Nothing outside that list.
- ✅ **At least 3 distinct processes/executables**, deployed as real OS processes on VMs.
- ✅ **At least 3 of 4** infrastructure categories: messaging, queuing, caching, databases.

> ⚠️ If a task seems to call for a container or a locally-installed service, it is the wrong approach for this repo. Flag it rather than working around it.

## Architecture

Three processes, one per VM, coordinated through managed GCP services.

| Component | Process | Role |
| :--- | :--- | :--- |
| **A** | `gliq-intake` | Accepts a design doc over HTTPS, uses an LLM to extract a structured `pitch_profile`, publishes a scoring request |
| **B** | `gliq-scoring` | Consumes scoring requests, matches the profile against the Steam comps corpus, computes a `fitment_result`, publishes a completion event |
| **C** | `gliq-report` | Consumes completion events, renders the recommendation and evidence report, persists and notifies |

### Cloud services

| Category | Service | Role |
| :--- | :--- | :--- |
| **Messaging** | Pub/Sub | Topic fan-out. `scoring.completed` events from B to C |
| **Database** | Cloud SQL (PostgreSQL) | Steam comps corpus, pitch profiles, fitment results, audit trail |
| **Caching** | Memorystore (Redis) | Comp-set lookups by genre/tag cluster; hot-query cache in front of Cloud SQL |
| *Queuing (optional 4th)* | Cloud Tasks | A→B work queue. Only if it does not require exposing B publicly |
| *Storage (optional)* | Cloud Storage | Rendered report artifacts, raw dataset staging |

The requirement is 3 of 4 — Pub/Sub, Cloud SQL, and Memorystore satisfy it. Cloud Tasks is a stretch goal, not a dependency.

### Networking

- **Only Component A is publicly reachable.** Static external IP, nginx, Let's Encrypt via certbot, served at `greenlightiq.fredt.io`.
- **B and C have no inbound endpoints.** They use Pub/Sub *pull* subscriptions, so they need no public IP and no load balancer.
- **Admin access is via Tailscale.** All three VMs join the tailnet. Public SSH is closed. No bastion host.
- `fredt.io` DNS is not on Cloudflare (the apex points at GitHub Pages) — do not propose Cloudflare Tunnel, it would require a nameserver migration.

### Infrastructure as Code

**Pulumi** manages all GCP resources. Every provisioned resource must be declared in the Pulumi stack so teardown is a single command — this matters because the project runs on finite trial credits ($300, expiring 2026-09-30).

## Repository Layout

```
components/{intake,scoring,reporting}/   # the three deployable processes
shared/                                  # pydantic schemas: pitch_profile, fitment_result
data/                                    # Kaggle -> Cloud SQL ETL, SteamSpy enrichment client
infra/                                   # Pulumi stack, systemd units, provisioning scripts
samples/                                 # example pitch documents (strong and weak)
tests/
docs/                                    # ARCHITECTURE, DEPLOYMENT, SCREENSHOTS, AI_USAGE_LOG
```

## Conventions

### Language & style

- **Python 3.11+.** Each component is independently invocable and independently testable.
- Components exchange **schema-validated JSON** (pydantic models in `shared/`). Never pass ad-hoc dicts across a component boundary.
- Component B's scoring is **rule-based and deterministic** — no LLM in the scoring path. This keeps results reproducible and re-gradable.
- Component A's LLM provider is **switchable** via config: `anthropic`, `gemini`, or `fixture`. The `fixture` provider returns a canned `pitch_profile` so B and C are fully developable and testable with no API key and no network.

### Data

- ⚠️ **Do not commit the Steam dataset.** The Kaggle dataset and the SteamSpy API carry their own licensing terms, separate from this repo's AGPLv3. Commit the **ETL script**, and have setup fetch the data.
- Sales figures are **estimates**, not audited revenue — Steam does not publish unit sales. Every report must disclose the estimation method (review-count/Boxleiter multiplier, SteamSpy owner bands) as an assumption.

### Secrets

- `.env` is gitignored; `.env.example` is committed.
- No credentials in source, in Pulumi state, or in committed config. Verify before every commit.

### Documentation

- **Documentation is graded, and heavily.** The assignment requires enough screenshots to prove the system works. Keep `docs/SCREENSHOTS.md` current as features land rather than reconstructing evidence at the end.
- **Markdown prose is not hard-wrapped.** Write each sentence or bullet on one flowing line; break only where it is semantically meaningful.
- Use the [EMOJIDICT](#emojidict) below consistently in status updates, summaries, and docs.

### Licensing

- **AGPLv3.** Chosen deliberately: the project is a network service, and AGPL §13 closes the SaaS loophole that GPLv3 leaves open, preserving a future dual-licensing/commercial option.
- ⚠️ Copyright must stay solely with the author for that option to remain viable. A **CLA is required before accepting any external contribution** — retrofitting one later means chasing down every past contributor.

## ⚠️ Git

- **DO NOT HANDLE GIT OPERATIONS.** That is the user's responsibility.
- **NEVER COMMIT OR PUSH** without explicit permission.
- Stop at natural checkpoints, summarize what changed, and ask.

## Working Agreement

- On **"GO!"**, execute the request.
- Prefer flagging a constraint conflict over silently working around it.
- Generated artifacts (reports, `__pycache__/`, `*.zip`, Pulumi outputs) belong in `.gitignore`.

## EMOJIDICT

| Emoji | Meaning |
| :---: | :--- |
| ✅ | Done / passing / success |
| ❌ | Failed / error / broken |
| ⚠️ | Warning / caution / needs attention |
| 🚧 | Work in progress / under construction |
| 🛑 | Stop / blocked / do not proceed |
| ➡️ | Go to / see / next step / reference |
| 💡 | Idea / suggestion / tip |
| 📝 | Note / documentation / write-up |
| 🐛 | Bug / defect |
| 🔧 | Fix / configuration / maintenance |
| 🚀 | Deploy / launch / ship / performance |
| 🧪 | Test / experiment |
| 🔍 | Investigate / review / search |
| 📦 | Package / dependency / build artifact |
| 🔒 | Security / secrets / auth |
| ❓ | Question / needs clarification |
| 🎯 | Goal / objective / target |
| 🧹 | Cleanup / refactor |
| 🎓 | Assignment / coursework / JHU deliverable |
| ⭐ | Personal flare / highlight |
