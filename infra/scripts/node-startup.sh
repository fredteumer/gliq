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
#   __ADMIN_USER__       local account Tailscale SSH maps the admin identity to

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

# Probe the exact resource the next step needs, and one that returns HTTP 200.
# ⚠️ `curl -f` exits non-zero on any status >= 400, so a URL that 404s on `/`
# can never satisfy this loop however healthy the network is — that mistake
# failed every node's first boot.
PROBE_URL="https://tailscale.com/install.sh"

echo "⏳ Waiting for network (probing ${PROBE_URL})..."
MAX_ATTEMPTS=45
ATTEMPT=0
until curl -fsS --max-time 5 -o /dev/null "${PROBE_URL}"; do
    CURL_EXIT=$?
    ATTEMPT=$((ATTEMPT + 1))
    if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
        echo "❌ Network not ready after ${MAX_ATTEMPTS} attempts (curl exit ${CURL_EXIT}) — aborting"
        echo "   6=DNS 7=connection refused 22=HTTP>=400 28=timeout"
        exit 1
    fi
    echo "   not ready (curl exit ${CURL_EXIT}), attempt ${ATTEMPT}/${MAX_ATTEMPTS}..."
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
# Deliberately uses the metadata server + REST API rather than `gcloud`, so
# this does not depend on the Cloud SDK being present in the boot image. The
# token comes from the VM's attached service account, which holds
# secretAccessor on exactly this one secret and nothing else.
ACCESS_TOKEN="$(curl -fsS -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

if [ -z "${ACCESS_TOKEN}" ]; then
    echo "❌ Could not obtain an access token from the metadata server"
    exit 1
fi

SECRET_URL="https://secretmanager.googleapis.com/v1/projects/__PROJECT__/secrets/__SECRET_NAME__/versions/latest:access"

if ! SECRET_JSON="$(curl -fsS -H "Authorization: Bearer ${ACCESS_TOKEN}" "${SECRET_URL}")"; then
    echo "❌ Could not read secret __SECRET_NAME__ — check the VM service account's IAM"
    exit 1
fi

AUTH_KEY="$(printf '%s' "${SECRET_JSON}" \
    | python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode())')"
unset ACCESS_TOKEN SECRET_JSON

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
# 4. Users
#-----------------------------------------------------------------------------
# Tailscale SSH maps a tailnet identity onto a LOCAL UNIX ACCOUNT, defaulting to
# the client's own username. A stock Debian image has no such account, so SSH
# fails with:
#     tailscale: failed to look up local user "<you>"
# even though the node is perfectly reachable.
#
# The admin account is generic (`admin`) rather than personal, so connect with
# `ssh admin@<host>` or put this in your ~/.ssh/config:
#     Host gliq-*
#         User admin

echo "👤 Creating admin user __ADMIN_USER__..."
if ! id -u "__ADMIN_USER__" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "__ADMIN_USER__"
fi
usermod -aG sudo "__ADMIN_USER__"

# Passwordless sudo: the account has no password at all (authentication happens
# at the tailnet layer via Tailscale SSH), so a sudo password prompt would be
# unanswerable rather than secure.
cat > /etc/sudoers.d/90-gliq-admin <<EOF
__ADMIN_USER__ ALL=(ALL) NOPASSWD:ALL
EOF
chmod 0440 /etc/sudoers.d/90-gliq-admin
visudo -c -f /etc/sudoers.d/90-gliq-admin >/dev/null

# Unprivileged service account for the component processes. No login shell —
# it exists to own /opt/gliq and to be the systemd unit's User=.
echo "👤 Creating service user gliq..."
if ! id -u gliq >/dev/null 2>&1; then
    useradd --system --home-dir /opt/gliq --shell /usr/sbin/nologin gliq
fi

echo "✅ Users ready: $(id -un __ADMIN_USER__), $(id -un gliq)"

#-----------------------------------------------------------------------------
# 5. Application layout
#-----------------------------------------------------------------------------
# /etc/gliq holds the systemd EnvironmentFile, generated from stack outputs by
#   python3 infra/env-from-stack.py --target /etc/gliq/gliq.env --no-secrets
# It is deliberately NOT written here: this script must contain no config that
# would drift from the Pulumi stack.

echo "📁 Preparing /opt/gliq and /etc/gliq..."
# Owned by the service user: the component runs as `gliq`, not root.
install -d -m 0755 -o gliq -g gliq /opt/gliq
# Env file holds credentials — readable by the service user, nobody else.
install -d -m 0750 -o root -g gliq /etc/gliq

#-----------------------------------------------------------------------------
# 6. Shell prompt
#-----------------------------------------------------------------------------
# Matches Fred's local prompt so a VM shell reads the same as a laptop shell —
# which matters most when several terminals are open at once and it needs to be
# obvious at a glance which host a command is about to run on.

echo "🎨 Installing shell prompt..."

# Quoted heredoc: everything below must reach the file LITERALLY. In particular
# $(date ...) has to survive as-is so it re-evaluates on every prompt render
# rather than freezing the boot time into the prompt.
cat > /etc/gliq-prompt.sh <<'PROMPT_EOF'
# GreenlightIQ shell prompt — installed by infra/scripts/node-startup.sh
# set custom prompt
# datetime, user@host, full path
PS1='\n\[\e[0;37m\]┌─[\[\e[1;33m\]$(date "+%Y-%m-%d %H:%M:%S")\[\e[0;37m\]]'
PS1+=' \[\e[0;37m\][\[\e[1;32m\]\u\[\e[1;34m\]@\[\e[1;32m\]\h\[\e[0;37m\]]'
PS1+=' \[\e[0;37m\][\[\e[1;33m\]\w\[\e[0;37m\]]\n'
PS1+='\[\e[0;37m\]└─\[\e[1;31m\]$ \[\e[0m\]'
PROMPT_EOF
chmod 0644 /etc/gliq-prompt.sh

# Sourced from ~/.bashrc rather than /etc/profile.d: Debian's default .bashrc
# assigns PS1 itself, and for an interactive SSH shell it runs *after*
# profile.d — so a prompt set there would be silently overwritten. Appending
# here means ours is the last assignment to win.
SOURCE_LINE='[ -r /etc/gliq-prompt.sh ] && . /etc/gliq-prompt.sh'
for rc in /root/.bashrc "/home/__ADMIN_USER__/.bashrc" /etc/skel/.bashrc; do
    [ -e "${rc}" ] || continue
    # Guarded so re-running the bootstrap doesn't stack duplicate lines.
    if ! grep -qF '/etc/gliq-prompt.sh' "${rc}"; then
        printf '\n# GreenlightIQ prompt\n%s\n' "${SOURCE_LINE}" >> "${rc}"
        echo "   added to ${rc}"
    else
        echo "   already present in ${rc}"
    fi
done

echo "✅ Prompt installed"

echo "🎯 Bootstrap complete at $(date -Is)"
