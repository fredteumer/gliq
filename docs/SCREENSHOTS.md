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
