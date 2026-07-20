# Deployment

> 🚧 Written as the Pulumi stack and systemd units land.

## Prerequisites

- GCP project with billing enabled
- `pulumi` and `gcloud` authenticated
- Tailscale account (admin access to the VMs)
- DNS control for `fredt.io` (A record for `greenlightiq`)

## 🔒 Required stack config

`Pulumi.dev.yaml` is **gitignored** — it carries `--secret` values encrypted with the stack passphrase, and a weak passphrase in a public AGPL repo is a published secret. A fresh clone must therefore recreate stack config by hand:

```bash
cd infra
pulumi stack init dev
pulumi config set gcp:project  <project-id>
pulumi config set gcp:region   us-central1
pulumi config set gcp:zone     us-central1-a

# Tailscale key from https://login.tailscale.com/admin/settings/keys
# Must be REUSABLE — three VMs register with it. Ephemeral is recommended so
# nodes deregister themselves when a VM is destroyed.
pulumi config set --secret tailscaleAuthKey  tskey-auth-...
```

Optional, with defaults:

| Key | Default | Notes |
| :--- | :--- | :--- |
| `machineType` | `e2-small` | Applies to all three VMs |
| `adminUser` | `admin` | Local account created on every VM for administration. ⚠️ Tailscale SSH maps to the **connecting client's** username, so `ssh <host>` alone looks for a local account named after *you*. Either connect as `ssh admin@<host>`, add a `Host gliq-*` → `User admin` block to `~/.ssh/config`, or set this key to your own username. |
| `tailscaleTag` | *(unset)* | e.g. `tag:gliq`. ⚠️ An **untagged** key mints nodes that authenticate as the key's owner; under Tailscale's default allow-all ACL such a node can reach every device on the tailnet. Setting a tag scopes them to a machine identity instead. `tailscale up` fails if the key is not authorised for the tag, so mint the key with it. |
| `topicScoringRequested` etc. | see `index.ts` | Pub/Sub resource names |

## Outline

1. **Provision** — `cd infra && pulumi up`. Creates Pub/Sub topics and subscriptions, three VMs, service accounts, firewall rules, the artifact bucket, and (in a later pass) Cloud SQL and Memorystore.
2. **Sync config** — `python3 infra/env-from-stack.py` writes stack outputs into `.env`. Re-run after **every** `pulumi up`, or the components point at stale resource names.
2. **Join the tailnet** — each VM joins Tailscale; public SSH stays closed.
3. **Load the corpus** — `data/etl` fetches the Steam dataset and loads it into Cloud SQL. The dataset is not committed to this repository.
4. **Deploy the components** — install per-component dependencies and enable the systemd units in `infra/systemd`.
5. **Expose Component A** — nginx + certbot on the intake VM, serving `greenlightiq.fredt.io`.
6. **Verify** — submit a sample pitch from `samples/` and follow it through `journalctl` on all three VMs.

## 🔧 Changing the VM bootstrap

The startup script lives in `metadata["startup-script"]`, **not** the `metadataStartupScript` convenience field — the latter is ForceNew, so every edit would destroy and recreate all three VMs.

The tradeoff is that edits no longer self-apply. `pulumi up` writes the new metadata but executes nothing; GCE only runs the script on boot. So it is always two steps:

```bash
cd infra && pulumi up
for vm in gliq-intake gliq-scoring gliq-report; do
    ssh root@$vm 'google_metadata_script_runner startup'
done
```

`google_metadata_script_runner startup` re-runs it without a reboot. The script is idempotent (guarded `useradd`, `install -d`, `grep` before appending to `.bashrc`), so repeat runs are safe.

Verify by reading the boot log rather than assuming — every phase is marked, and a failure shows exactly which one:

```bash
ssh root@gliq-intake 'tail -40 /var/log/gliq-startup.log'
# or, before the node is reachable at all:
gcloud compute instances get-serial-port-output gliq-intake --zone=us-central1-a
```

⚠️ **Connectivity probes:** the readiness loop uses `curl -f`, which exits non-zero on **any** HTTP status ≥ 400. Probing a URL that 404s on `/` therefore never succeeds regardless of network health — that mistake silently failed every node's first boot while reporting "network not ready". Probe something that returns 200.

## ⚠️ Teardown

`pulumi destroy` removes everything. Run it once evidence is captured.

Cloud SQL, Memorystore, and Pub/Sub beyond its free allowance **bill continuously whether or not they receive traffic**, and none of the three is covered by GCP's Always Free tier. The project runs on trial credits that expire **2026-09-30**.
