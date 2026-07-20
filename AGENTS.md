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
infra/                                   # Pulumi stack (TypeScript) at the root; systemd/ units alongside
samples/                                 # example pitch documents (strong and weak)
tests/
docs/                                    # ARCHITECTURE, DEPLOYMENT, SCREENSHOTS, AI_USAGE_LOG
```

## Conventions

### Language & style

- **Python 3.11+.** Each component is independently invocable and independently testable.
- Components exchange **schema-validated JSON** (pydantic models in `shared/`). Never pass ad-hoc dicts across a component boundary.
- **Deterministic first, LLM as a swappable upgrade.** Every stage where a model could help has a deterministic implementation and, optionally, an LLM implementation behind the same interface — both emitting the same pydantic model. The deterministic path is not a placeholder; it is the reproducible **control** the LLM path is measured against.
- **The scoring arithmetic itself stays deterministic in both parts.** That is the constraint that matters: a change in grade must be attributable to extraction or comp selection, never to model drift. ➡️ `docs/SCORING.md`
- Component A's LLM provider is **switchable** via config: `anthropic`, `gemini`, or `fixture`. The `fixture` provider returns a canned `pitch_profile` so B and C are fully developable and testable with no API key and no network.
- ⚠️ **Missing extracted fields must lower the grade, not raise an exception.** The design document is open-ended, so `PitchProfile` is almost entirely optional by design and decisiveness is enforced in scoring, not in validation. A required field there would mean a sparse pitch dies in Component A and never gets a report.

### Data

- ⚠️ **Do not commit the Steam dataset.** The Kaggle dataset and the SteamSpy API carry their own licensing terms, separate from this repo's AGPLv3. Commit the **ETL script**, and have setup fetch the data.
- Sales figures are **estimates** — Steam does not publish unit sales. Every report must disclose the estimation method (review-count/Boxleiter multiplier, SteamSpy owner bands) as an assumption.
- ⚠️ **Report units moved, never revenue.** Deriving dollars means multiplying an already-estimated unit count by list price, ignoring discounting, regional pricing, refunds and Steam's cut — stacking a second guess on the first to produce a figure that looks precise and is not. Units carry exactly one layer of estimation error. List price is used only to compare a pitch's asking price against comparable titles' asking prices (`price_alignment`), where nothing is inferred. ➡️ `docs/SCORING.md`

### Secrets

- `.env` is gitignored; `.env.example` is committed.
- No credentials in source, in Pulumi state, or in committed config. Verify before every commit.

### Documentation

- **Documentation is graded, and heavily.** The assignment requires enough screenshots to prove the system works. Keep `docs/SCREENSHOTS.md` current as features land rather than reconstructing evidence at the end.
- **Markdown prose is not hard-wrapped.** Write each sentence or bullet on one flowing line; break only where it is semantically meaningful.
- **Use emojis liberally**, per the [EMOJIDICT](#-emojidict) below — in docs, status updates, summaries, commit-adjacent notes, and in **script and CLI output**. A wall of undifferentiated prose is harder to scan than the same content with a ✅ / ⚠️ / 🛑 down the left edge, and the assignment is graded partly on how legibly the system explains itself.
- Emojis carry meaning, so **never improvise a synonym**. If you need one the table lacks, add a row to the EMOJIDICT rather than inventing a one-off — two symbols for one concept is worse than none.
- The same applies to tooling: a script that reports status should mark it (`✅ wrote 9 variables`, `⚠️ GCP_PROJECT_ID is also set outside the managed block`), not print bare sentences.

### Licensing

- **AGPLv3.** Chosen deliberately: the project is a network service, and AGPL §13 closes the SaaS loophole that GPLv3 leaves open, preserving a future dual-licensing/commercial option.
- ⚠️ Copyright must stay solely with the author for that option to remain viable. A **CLA is required before accepting any external contribution** — retrofitting one later means chasing down every past contributor.

## 🔄 Session continuity (WIP files)

We carry context across sessions with **WIP files at the repo root**, mirroring the `../manna` and `../grove` convention.

⚠️ One adaptation: `manna` is an umbrella directory holding subrepos, so its WIP files naturally sit outside any git tree. `gliq` is itself a single repo, so **WIP files here are gitignored** (`WIP*.md`). They are local working memory, not a deliverable — never commit them, and never let them substitute for documentation that belongs in `docs/`.

**At the start of any session: check for the most recent `WIP-YYYY-MM-DD.md` and read it first** — it is the handoff from the previous session.

- **`WIP-YYYY-MM-DD.md`** — dated session snapshot/handoff. One per session: scope, what got done (with file paths / commit refs), context & decisions, blockers, and a prioritized TODO for next time. These accumulate; don't delete old ones.
- **`WIP.md`** (undated, optional) — a rolling doc for a single active workstream once we commit to a specific feature/fix.

When picking work back up, start a **new** dated file rather than editing an old one; reference the prior file if continuing the same thread.

**Format (terse — bullets, not prose).** End each session by pruning the day's file down to exactly this shape:

```
CURRENT TASK:
<one- or two-line summary of the active thread>

TODO:
- x - brief summary
- y - brief summary

DONE IN SESSION:
- a ✅ - very brief summary
```

A WIP is a **handoff, not a journal**. Scratch notes during a session are fine; before closing out, collapse to the above. Move durable architecture and decisions into `AGENTS.md` or `docs/` rather than letting them accrete in WIP files — a decision recorded only in a gitignored file is a decision that is lost.

## 🎯 Plan before multi-step work

**Anything that is not a single obvious edit gets a plan first.** Use the agent's plan mode where it exists (Claude Code: plan mode / `ExitPlanMode`); otherwise write the plan into the response before touching a file.

Plan when the work: touches more than one file or component, provisions or changes cloud resources, changes a schema in `shared/`, alters deployment or systemd, or is described in more than one sentence by the user. Skip the plan for a typo, a one-line fix, or a read-only question.

A plan states 🎯 the goal, the ordered steps with the files each touches, ⚠️ anything that could conflict with the [hard constraints](#-hard-constraints--read-before-proposing-any-architecture) or spend credits, and what "done" looks like. **Get agreement on the plan before executing it** — a wrong plan is cheap, a wrong `pulumi up` is not.

For a plan spanning more than one session, land it in the WIP file's `TODO:` block so the next session inherits it.

## ⚠️ Git

- **DO NOT HANDLE GIT OPERATIONS.** That is the user's responsibility.
- **NEVER COMMIT OR PUSH** without explicit permission.
- Stop at natural checkpoints, summarize what changed, and ask.

## Working Agreement

- On **"GO!"**, execute the request.
- Prefer flagging a constraint conflict over silently working around it.
- Generated artifacts (reports, `__pycache__/`, `*.zip`, Pulumi outputs) belong in `.gitignore`.

## ✨ EMOJIDICT

This table is **authoritative**. Use it liberally and consistently; add a row when you introduce a new emoji rather than improvising a synonym for one already here.

> 📝 Sibling repo `ponder-the-orb` keeps an equivalent legend. The sets are deliberately close so moving between them is frictionless — the one divergence is in-progress, which is 🚧 here and 🛫 there.

**Status markers** — use these when writing any checklist or status table:

| Emoji | Meaning |
| :---: | :--- |
| ✅ | Done / passing / verified |
| 🚧 | In progress / under construction |
| ⚪ | TODO — not started |
| ⏸️ | Paused / parked / deferred |
| ⛔ | Not doing / out of scope (always give the reason) |
| ⏳ | Deferred-but-required — captured now, activated later |

**Everything else:**

| Emoji | Meaning |
| :---: | :--- |
| ❌ | Failed / error / broken |
| ⚠️ | Warning / caution / needs attention |
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
