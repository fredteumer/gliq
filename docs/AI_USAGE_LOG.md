# AI Usage Log

Chronological record of AI assistance on GreenlightIQ. Kept honest — real interactions, logged as they happen.

## 2026-07-18 — Scoping and scaffold

**Tool:** Claude Code (Opus 4.8)

- Reviewed the updated assignment brief and rubric; identified that the build constraints (managed cloud services, no containers/functions) diverged substantially from the approved proposal's offline CLI design.
- Compared licensing options for a network service with possible future monetization. Selected **AGPLv3** over MIT and GPLv3 — GPLv3's copyleft triggers on distribution, which a SaaS competitor never does.
- Compared AWS and GCP on free-tier terms and on how cleanly each separates the messaging and queuing rubric categories. Selected GCP.
- Settled the architecture: three VMs, Pub/Sub + Cloud SQL + Memorystore (3 of 4 required technology components), Pub/Sub *pull* subscriptions so Components B and C need no public endpoints, Tailscale for admin access, Pulumi for IaC.
- Rejected Cloudflare Tunnel (would require migrating `fredt.io` nameservers away from the current registrar; apex serves GitHub Pages) and Tailscale Funnel (no custom domain support).
- Authored `AGENTS.md`, `README.md`, and the repository scaffold including `shared/schemas.py` message contracts.
