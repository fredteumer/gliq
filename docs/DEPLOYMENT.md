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
| `enableDatabase` | *(unset → off)* | 💰 Gates the whole Cloud SQL block. Unlike the rest of the stack it bills meaningfully by the hour, so it is opt-in and can be torn down alone: `pulumi config set enableDatabase false && pulumi up`. |
| `dbPassword` | *(required when enabled)* | 🔒 Set with `--secret`. Encrypted in state, and exported as a secret output so `env-from-stack.py` can put it in the components' `EnvironmentFile`. |
| `dbTier` | `db-f1-micro` | Cheapest Postgres tier; ample for a ~100k-row corpus. |
| `dbName` / `dbUser` | `greenlightiq` / `gliq` | |

### 💰 Turning the database on and off

The corpus is fully reproducible from the `data/` ETL and pitch records are disposable during development, so nothing in Postgres is precious. Backups are disabled and `deletionProtection` is off deliberately — both would otherwise obstruct exactly the teardown the credit budget depends on.

```bash
cd infra
pulumi config set enableDatabase true
pulumi config set --secret dbPassword '<pick one>'
pulumi up

# ...and between sessions:
pulumi config set enableDatabase false && pulumi up
```

⚠️ First creation is slow — the servicenetworking peering plus a fresh instance is typically **10–15 minutes**. It is not hung.

⚠️ Reaching it from a laptop needs `tailscale up --accept-routes`: the instance has **no public IP**, so the only path is the `10.10.0.0/24` route `gliq-scoring` advertises to the tailnet.

## Outline

1. **Provision** — `cd infra && pulumi up`. Creates Pub/Sub topics and subscriptions, three VMs, service accounts, firewall rules, the artifact bucket, and (in a later pass) Cloud SQL. ⛔ No Memorystore — caching is deliberately not implemented, ➡️ [ARCHITECTURE.md §7](./ARCHITECTURE.md).
2. **Sync config** — `python3 infra/env-from-stack.py` writes stack outputs into `.env`. Re-run after **every** `pulumi up`, or the components point at stale resource names.
2. **Join the tailnet** — each VM joins Tailscale; public SSH stays closed.
3. **Load the corpus** — `data/etl` fetches the Steam dataset and loads it into Cloud SQL. The dataset is not committed to this repository.
4. **Deploy the components** — install per-component dependencies and enable the systemd units in `infra/systemd`.
5. **Expose Component A** — nginx + certbot on the intake VM, serving `greenlightiq.fredt.io`. ➡️ the runbook below.
6. **Verify** — submit a sample pitch from `samples/` and follow it through `journalctl` on all three VMs.

## 🔒 Exposing Component A (step 5)

Component A is the only publicly reachable process. nginx terminates TLS and proxies to uvicorn on loopback; the login gate lives in the application.

⚠️ **Order matters.** Configure the credential *before* issuing the certificate. Issuing first leaves an unauthenticated public endpoint that queues work, and Certificate Transparency logs are scanned by bots within minutes of issuance.

### 5a — Set the login credential

```bash
python3 infra/scripts/hash-password.py          # prompts twice, echoes nothing
cd infra
pulumi config set --secret adminPasswordHash '<paste the hash>'
pulumi config set --secret sessionSecret "$(openssl rand -hex 32)"
pulumi up                                        # publishes the two stack outputs
cd .. && python3 infra/env-from-stack.py         # writes them into .env
./infra/scripts/deploy.sh intake --deps          # ships /etc/gliq/gliq.env
```

🔒 An unset `ADMIN_PASSWORD_HASH` **refuses every login** rather than allowing any, and Component A logs `🔒 ADMIN_PASSWORD_HASH is not set` at startup. An unconfigured instance is locked, not open.

### 5b — nginx and the certificate

DNS must already point at the reserved static IP (`pulumi stack output intakeStaticIp`). The `gliq-allow-https` firewall rule already opens **80 and 443** to the `gliq-public` tag, so certbot's HTTP-01 challenge needs no infrastructure change.

```bash
ssh root@gliq-intake
apt-get install -y nginx certbot python3-certbot-nginx

# from the repo, in another shell:
scp infra/scripts/nginx-gliq.conf root@gliq-intake:/etc/nginx/sites-available/gliq

ssh root@gliq-intake
ln -sf /etc/nginx/sites-available/gliq /etc/nginx/sites-enabled/gliq
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

certbot --nginx -d greenlightiq.fredt.io --non-interactive --agree-tos -m <you@example.com> --redirect
```

certbot rewrites the site file in place, adding the `listen 443` block and an HTTP→HTTPS redirect, and installs a renewal timer. Verify with `certbot renew --dry-run` and `systemctl is-active certbot.timer`.

### 5c — Verify the gate

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://greenlightiq.fredt.io/healthz   # 200, open by design
curl -s -o /dev/null -w '%{http_code}\n' https://greenlightiq.fredt.io/          # 303 -> /login
curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' \
     -H 'Content-Type: application/json' https://greenlightiq.fredt.io/pitches   # 303, NOT 422
```

⚠️ That last one is the meaningful check. A **422** means request-body validation ran before authentication and the endpoint is describing its schema to anonymous callers — the gate is middleware precisely so parsing never happens first.

### Scripted submission

Everything except `/healthz` requires a session, so scripted runs need a cookie jar:

```bash
curl -sc /tmp/gliq.jar -X POST https://greenlightiq.fredt.io/login \
     -d 'username=admin' -d 'password=<password>' -o /dev/null
curl -sb /tmp/gliq.jar -X POST https://greenlightiq.fredt.io/pitches \
     -H 'Content-Type: application/json' \
     -d "$(python3 -c 'import json;print(json.dumps({"document":open("samples/strong-pitch.md").read()}))')"
```

### ⚠️ What does not survive a rebuild

`/etc/nginx` and `/etc/letsencrypt` live outside `/opt/gliq`, so `deploy.sh` never touches them — but `node-startup.sh` does not provision nginx either, so **a recreated VM loses both** and 5b must be repeated.

Deliberately not automated: Let's Encrypt rate-limits to **5 certificates per domain per week**, and a boot script that re-issues on every recreation could lock the hostname out at the worst moment. Package install and site config could be moved into `node-startup.sh`; certificate issuance should stay a deliberate act.

## 🚀 Deploying component code

`infra/scripts/deploy.sh` is this project's CD. There is no hosted pipeline: the VMs are tailnet-only with public SSH closed, so a GitHub runner would need a Tailscale auth key of its own — deliberately avoided, since it would partly undo the no-public-ingress property the network design exists to provide.

```bash
./infra/scripts/deploy.sh all                # all three nodes
./infra/scripts/deploy.sh intake --deps      # one node, (re)install dependencies
./infra/scripts/deploy.sh scoring --force    # deploy a dirty tree
```

Once per run, before any node is touched: generates `/etc/gliq/gliq.env` from stack outputs, then applies `alembic upgrade head`.

Per node it: installs `rsync` if missing → syncs the tree to `/opt/gliq` → stamps `VERSION` with the commit SHA → installs the env file → optionally installs dependencies → `chown`s to the `gliq` service user → installs the unit file → restarts → prints the last 15 journal lines.

⚠️ Migrations run **once, from the deploy host — never inside the per-node loop.** Three nodes each running `alembic upgrade head` would race for the same `alembic_version` lock to do identical work. The schema is a property of the database, not of a node; the deploy host reaches Cloud SQL over the tailnet subnet route, while the nodes reach it directly on the VPC and never need Alembic at all (it is in the `etl` extra, not any component's).

| Flag | Effect |
| :--- | :--- |
| `--deps` | Create `/opt/gliq/.venv` if absent and `pip install -e .[<component>]`. **Off by default** — dependency installs are slow on an e2-small and rarely change. Required on a node's first deploy. |
| `--force` | Deploy despite a dirty working tree. `VERSION` is stamped `<sha>-dirty`. |

⚠️ **What lands on the VM is your working tree, not a pushed commit.** The clean-tree check is what keeps "what is deployed" answerable — `--force` trades that away deliberately. Take graded screenshots from a clean-tree deploy.

### Things that bite

- **`rsync: command not found`** — it is not in the Debian cloud image. `node-startup.sh` now installs it; `deploy.sh` also self-heals nodes bootstrapped before that change.
- **`Failed to load environment files`** — systemd treats a missing `EnvironmentFile` as a hard failure, surfacing as the unhelpful *"unavailable resources or another system error"*. `deploy.sh` generates `/etc/gliq/gliq.env` from Pulumi stack outputs on every deploy, so this means the local `pulumi stack output` call failed — check `PULUMI_CONFIG_PASSPHRASE`.
- **SSH hangs with no output** — the tailnet's SSH policy is in **check mode**, waiting on an interactive browser link. One check authenticates the whole session, not one host. To remove it, set the `ssh` rule's `action` to `accept` in the Tailscale admin console's Access Controls — that is tailnet-wide policy, not per-node config, so there is nothing to change on the boxes or in `node-startup.sh`.
- **`--accept-routes`** is unrelated to deploys (those use `100.x` tailnet addresses directly). It is needed on the *client* to reach the `10.10.0.0/24` subnet — i.e. Cloud SQL and Memorystore private IPs — via the route `gliq-scoring` advertises.

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
