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
| 12 | Redis cache hit demonstrated — cold vs. warm scoring latency | Technology Component (caching) | ⬜ |
| 13 | Final rendered report: grade, tier, comps table, assumptions | End-to-end, Real-World Relevance | ⬜ |
| 14 | Second run with a contrasting pitch producing a different grade | End-to-end | ⬜ |

💡 Item 12 is worth deliberate effort. "We used a cache" is easy to claim and hard to prove — a timing comparison makes the caching component visibly real rather than decorative.

💡 Item 14 guards against a grader assuming the output is hardcoded. Two pitches, two clearly different verdicts.

## 📸 Ready to capture now (as of 2026-07-20)

All three components run end to end, so items **7, 8, 9, 11, 13 and 14 are reachable today** — still ⬜ above because the PNGs have not been taken. The commands below reproduce exactly what was observed, so capture is a matter of running them and screenshotting.

⚠️ Items 5, 6 and 12 remain blocked: nginx/TLS is not configured (so submission is loopback-only for now), and Memorystore is provisioned but unwired.

**Submit a pitch** — from the intake VM, since uvicorn binds `127.0.0.1:8000` and nginx is not up yet:

```bash
ssh root@gliq-intake
curl -s http://127.0.0.1:8000/healthz
cd /opt/gliq && python3 -c "import json,urllib.request; d=open('samples/strong-pitch.md').read(); \
  r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/pitches', \
  data=json.dumps({'document':d}).encode(), headers={'Content-Type':'application/json'})); \
  print(json.dumps(json.load(r), indent=2))"
```

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
