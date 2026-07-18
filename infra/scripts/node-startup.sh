#!/bin/bash
#
# GreenlightIQ node bootstrap.
#
# Runs once on first boot of every component VM. Responsibilities:
#   1. join the tailnet (this is the ONLY admin path — public SSH is closed)
#   2. install the Python runtime the component needs
#   3. lay down /etc/gliq for the systemd EnvironmentFile
#
# The Tailscale auth key is pulled from Secret Manager using the VM's own
# service account, so it never appears in instance metadata. See core/secrets.ts.
#
# Placeholders substituted by core/compute.ts before upload:
#   __PROJECT__          GCP project id
#   __SECRET_NAME__      Secret Manager secret holding the auth key
#   __HOSTNAME__         tailnet hostname for this node
#   __EXTRA_TS_FLAGS__   per-node `tailscale up` flags (routes, tags)

set -euo pipefail

# Everything lands in one log so a failed boot is diagnosable after the fact.
exec > >(tee /var/log/gliq-startup.log)
exec 2>&1

echo "🚀 GreenlightIQ node bootstrap starting at $(date -Is)"

#-----------------------------------------------------------------------------
# 1. Wait for egress
#-----------------------------------------------------------------------------
# B and C have no external IP and reach the internet through Cloud NAT, which
# can take a moment longer than the boot sequence. Downloading anything before
# it is ready fails the whole script.

echo "⏳ Waiting for network..."
MAX_ATTEMPTS=30
ATTEMPT=0
until curl -fsS --max-time 5 https://packages.cloud.google.com >/dev/null 2>&1; do
    ATTEMPT=$((ATTEMPT + 1))
    if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
        echo "❌ Network not ready after ${MAX_ATTEMPTS} attempts — aborting"
        exit 1
    fi
    echo "   not ready, attempt ${ATTEMPT}/${MAX_ATTEMPTS}..."
    sleep 2
done
echo "✅ Network is up"

#-----------------------------------------------------------------------------
# 2. Base packages
#-----------------------------------------------------------------------------
# Debian 12 ships Python 3.11, which satisfies the project's >=3.11 floor.
# Pinning to the distro Python keeps the VMs reproducible.

echo "📦 Installing base packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates
echo "✅ Python: $(python3 --version)"

#-----------------------------------------------------------------------------
# 3. Tailscale
#-----------------------------------------------------------------------------

echo "📦 Installing Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh

# IP forwarding — only meaningful on the node advertising subnet routes, but
# harmless elsewhere and keeps every node identical.
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' >> /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf >/dev/null

echo "🔒 Fetching Tailscale auth key from Secret Manager..."
# --secret is read with the VM's attached service account, which holds
# secretAccessor on exactly this one secret and nothing else.
if ! AUTH_KEY="$(gcloud secrets versions access latest \
        --secret="__SECRET_NAME__" \
        --project="__PROJECT__" 2>/dev/null)"; then
    echo "❌ Could not read secret __SECRET_NAME__ — check the VM service account's IAM"
    exit 1
fi

if [ -z "${AUTH_KEY}" ]; then
    echo "❌ Secret __SECRET_NAME__ is empty"
    exit 1
fi

echo "🔗 Joining tailnet as __HOSTNAME__..."
# --ssh enables Tailscale SSH: admin access is authenticated by tailnet
# identity, so no public port 22 and no authorized_keys management.
tailscale up \
    --auth-key="${AUTH_KEY}" \
    --hostname="__HOSTNAME__" \
    --ssh \
    __EXTRA_TS_FLAGS__

unset AUTH_KEY

if tailscale status >/dev/null 2>&1; then
    echo "✅ Tailscale up"
    tailscale status || true
else
    echo "❌ Tailscale failed to start"
    exit 1
fi

#-----------------------------------------------------------------------------
# 4. Application layout
#-----------------------------------------------------------------------------
# /etc/gliq holds the systemd EnvironmentFile, generated from stack outputs by
#   python3 infra/env-from-stack.py --target /etc/gliq/gliq.env --no-secrets
# It is deliberately NOT written here: this script must contain no config that
# would drift from the Pulumi stack.

echo "📁 Preparing /opt/gliq and /etc/gliq..."
install -d -m 0755 -o root -g root /opt/gliq
install -d -m 0750 -o root -g root /etc/gliq

echo "🎯 Bootstrap complete at $(date -Is)"
