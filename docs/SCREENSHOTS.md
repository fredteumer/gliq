# Evidence

Screenshots demonstrating that GreenlightIQ is deployed and functioning end to end.

> ⚠️ **This file is graded evidence, not an afterthought.** The instructor cannot run the system — these screenshots *are* the proof of the 30-point "end-to-end working" criterion plus the 10-point "Cloud Integration" criterion. Capture each item as it lands rather than reconstructing at the end.

Capture into `docs/evidence/` and embed with `<img src="evidence/NAME.png" width="450">` — width is required or images render at native size and overflow when converted to PDF.

## Checklist

| # | Evidence | Rubric criterion | Status |
| :--- | :--- | :--- | :---: |
| 1 | GCP console: all provisioned resources (Pub/Sub topics, Cloud SQL, Memorystore, VMs) | Cloud Integration, Technology Component | ⬜ |
| 2 | `pulumi up` output showing the stack created | Cloud Integration | ⬜ |
| 3 | Three VMs running in the GCP console | Distributed Application | ⬜ |
| 4 | `systemctl status` for each of `gliq-intake`, `gliq-scoring`, `gliq-report` | Distributed Application | ⬜ |
| 5 | `https://greenlightiq.fredt.io` serving over valid TLS | Cloud Integration | ⬜ |
| 6 | Pitch submitted through the public endpoint | End-to-end | ⬜ |
| 7 | `journalctl` on A showing extraction + publish to Pub/Sub | End-to-end, Technology Component | ⬜ |
| 8 | `journalctl` on B showing message pulled, comps queried, result published | End-to-end, Technology Component | ⬜ |
| 9 | `journalctl` on C showing event consumed and report rendered | End-to-end | ⬜ |
| 10 | Pub/Sub metrics in console — messages published/acked | Technology Component (messaging) | ⬜ |
| 11 | Cloud SQL query showing the comps corpus loaded, plus a persisted result row | Technology Component (database) | ⬜ |
| 12 | Pub/Sub **queue** semantics — dead-letter topic, retry policy, ack deadline in the console | Technology Component (queuing) | ⬜ |
| 13 | Final rendered report: grade, tier, comps table, assumptions | End-to-end, Real-World Relevance | ⬜ |
| 14 | Second run with a contrasting pitch producing a different grade | End-to-end | ⬜ |

💡 Item 12 carries weight, because queuing and messaging are both served by Pub/Sub here (➡️ [ARCHITECTURE.md §7](./ARCHITECTURE.md)). Screenshot the `scoring-requested` subscription's **dead-letter policy, retry policy and ack deadline** alongside the `gliq-dead-letter` topic — that is the evidence it is a work queue and not just a broadcast channel. ⛔ Caching is deliberately not implemented; do not present a cache screenshot.

💡 Item 14 guards against a grader assuming the output is hardcoded. Two pitches, two clearly different verdicts.

## 📸 Ready to capture now (as of 2026-07-20)

**Every item is now reachable.** The system is live at **https://greenlightiq.fredt.io** with TLS and a login gate, so items 5, 6 and 13 are captured in a browser rather than a terminal. Items remain ⬜ above only because the PNGs have not been taken.

**Items 1, 2, 3, 10, 12** are GCP console screenshots. **4, 7, 8, 9, 11** are terminal. **5, 6, 13, 14** are the browser UI.

**Item 5 — TLS.** Load `https://greenlightiq.fredt.io` and capture the padlock plus the certificate details (issuer *Let's Encrypt*, the `greenlightiq.fredt.io` subject). 💡 Capture the login screen for this one — it shows TLS *and* that the endpoint is gated.

**Item 6 — submission through the public endpoint.** Sign in, pick `strong-pitch.md` from the sample dropdown, submit. The ⏳ pending page auto-refreshes and resolves to the report within ~10s. 💡 Grab the pending page too — it is evidence the work is genuinely asynchronous rather than a synchronous call pretending to be a pipeline.

**Item 13 — the rendered report.** The pitch detail page: grade, investment tier, rationale, de-risk actions, the fitment breakdown ⚠️ with `differentiation` marked *unweighted*, the ranked comps table, and the assumptions block.

💡 The **All pitches** list is worth one frame on its own — four pitches, three distinct grades and tiers, one of them `Untitled pitch / F / pass`, which shows the insufficient-information path rendering differently rather than as a zeroed scorecard.

**Scripted alternative** (if a terminal capture is preferred) — everything except `/healthz` needs a session, so use a cookie jar. ➡️ [DEPLOYMENT.md](./DEPLOYMENT.md), "Scripted submission".

**Item 7 — A extracts and publishes:** `journalctl -u gliq-intake -n 20 --no-pager`

```
INFO ✅ extractor=deterministic vocabulary=452 tags from 82,952 titles
INFO ✅ accepted 82d5ff2b-… — Hollow Reef (Action, 8 tags) via deterministic
```

**Item 8 — B pulls, queries comps, publishes:** `journalctl -u gliq-scoring -n 20 --no-pager`

```
INFO 🔍 scoring 82d5ff2b-… (Hollow Reef)
INFO ✅ 82d5ff2b-… — D (46.7) from 50 comps
```

**Item 9 — C consumes the event and renders:** `journalctl -u gliq-report -n 20 --no-pager`

```
INFO 📥 consumed 82d5ff2b-… — Hollow Reef (D 46.7)
INFO 📝 rendered 82d5ff2b-… — de_risk, 1 de-risk action(s), 3326 chars
WARNING ⚠️ b5a047c3-… reported as INSUFFICIENT INFORMATION, not as a low grade
```

**Item 11 — corpus + persisted result**, against Cloud SQL over the tailnet:

```sql
SELECT count(*) FROM steam_titles;                      -- 82,952
SELECT title, status, grade, recommendation->>'tier', length(report_md)
  FROM pitches ORDER BY scored_at;
```

**Item 13 — the rendered report.** Stored as Markdown in `pitches.report_md`, not as a file:

```sql
SELECT report_md FROM pitches WHERE title = 'Hollow Reef';
```

Contains, in order: grade + investment tier, the rationale, de-risk actions, the fitment breakdown ⚠️ with `differentiation` marked **unweighted**, the ranked comps table, and the assumptions block. 💡 For the screenshot, pipe it through a Markdown viewer (`glow`, or paste into any renderer) — it is stored as content precisely so presentation is a display-time choice.

**Item 14 — contrasting verdicts.** Three documents, three distinct outcomes, which also demonstrates the *insufficient information* path as separate from a low grade:

| Document | Grade | Score | Comps | Tier | Note |
| :--- | :---: | ---: | ---: | :--- | :--- |
| `samples/strong-pitch.md` | D | 46.7 | 50 | `de_risk` | premium-priced metroidvania |
| `samples/weak-pitch.md` | B | 75.0 | 9 | `greenlight` | free-to-play battle royale |
| `"We want to make a game."` | F | 0.0 | 0 | `pass` | ⛔ insufficient information — no comp basis |

💡 The third row is the one worth showing a grader: it renders a visibly *different* report — "Not evaluated", no fitment table, no comps — rather than a zeroed-out scorecard. "We cannot tell" and "we evaluated it and it is bad" are different claims and the system does not conflate them.

⚠️ The strong/weak grades are **inverted relative to the sample names** — this is a known open issue, not a demo artifact. Do not present these two as evidence of judgement quality until the scoring recalibration lands; they are evidence that the *pipeline* works. ➡️ the recalibration TODO in the WIP file.
