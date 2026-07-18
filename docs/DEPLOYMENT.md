# Deployment

> 🚧 Written as the Pulumi stack and systemd units land.

## Prerequisites

- GCP project with billing enabled
- `pulumi` and `gcloud` authenticated
- Tailscale account (admin access to the VMs)
- DNS control for `fredt.io` (A record for `greenlightiq`)

## Outline

1. **Provision** — `cd infra/pulumi && pulumi up`. Creates Pub/Sub topics and subscriptions, Cloud SQL (PostgreSQL), Memorystore (Redis), three VMs, service accounts, and firewall rules.
2. **Join the tailnet** — each VM joins Tailscale; public SSH stays closed.
3. **Load the corpus** — `data/etl` fetches the Steam dataset and loads it into Cloud SQL. The dataset is not committed to this repository.
4. **Deploy the components** — install per-component dependencies and enable the systemd units in `infra/systemd`.
5. **Expose Component A** — nginx + certbot on the intake VM, serving `greenlightiq.fredt.io`.
6. **Verify** — submit a sample pitch from `samples/` and follow it through `journalctl` on all three VMs.

## ⚠️ Teardown

`pulumi destroy` removes everything. Run it once evidence is captured.

Cloud SQL, Memorystore, and Pub/Sub beyond its free allowance **bill continuously whether or not they receive traffic**, and none of the three is covered by GCP's Always Free tier. The project runs on trial credits that expire **2026-09-30**.
